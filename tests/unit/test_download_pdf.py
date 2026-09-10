from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_fetch import cli
from ._paper_fetch_support import build_envelope, sample_article

from dataclasses import replace
import pymupdf
from paper_fetch.providers.base import (
    ProviderContent,
    ProviderFailure,
    ProviderFetchResult,
)
from paper_fetch.runtime import RuntimeContext
from paper_fetch.workflow.pdf_download import save_requested_pdf
from paper_fetch.workflow.fulltext import (
    _try_official_provider,
    _ProviderAttemptOutputs,
)
from ._paper_fetch_support import FixtureHtmlTransport, http_response


class DownloadPdfCliTests(unittest.TestCase):
    def test_requested_pdf_cannot_succeed_with_only_markdown(self):
        # An HTML/Markdown success must not masquerade as a successful PDF download.
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                cli, "fetch_paper", return_value=build_envelope(sample_article())
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                code = cli.main(
                    [
                        "fetch",
                        "--query",
                        "10.1016/test",
                        "--download-pdf",
                        "--output-dir",
                        directory,
                        "--progress",
                        "none",
                        "--manifest",
                        str(Path(directory) / "result.json"),
                    ]
                )
            except SystemExit as exc:
                code = exc.code
            report = Path(directory) / "result.json"
            self.assertTrue(
                report.exists(),
                "PDF failure must have a result record, not an unknown CLI flag",
            )
            record = json.loads(report.read_text(encoding="utf-8"))
            self.assertNotEqual(code, 0)
            self.assertIn("PDF", json.dumps(record))


DOI = "10.1038/example"
TITLE = "Seasonal energy allocation in marine fish"
PDF_URL = "https://www.nature.com/articles/example.pdf"
LANDING_URL = "https://www.nature.com/articles/example"


def make_pdf(doi=DOI):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), TITLE + "\nDOI: " + doi)
        doc.set_metadata({"title": TITLE, "subject": "DOI: " + doi})
        return doc.tobytes()


def html_content():
    return ProviderContent(
        route_kind="html",
        source_url=LANDING_URL,
        content_type="text/html",
        body=(
            f'<html><head><meta name="citation_pdf_url" '
            f'content="{PDF_URL}"></head><body>{TITLE}</body></html>'
        ).encode(),
    )


class PdfArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.pdf = make_pdf()
        self.transport = FixtureHtmlTransport(
            {PDF_URL: http_response(PDF_URL, self.pdf, "application/pdf")}
        )
        self.context = RuntimeContext(
            env={},
            transport=self.transport,
            download_dir=self.output,
            artifact_mode="markdown-assets",
        )
        self.addCleanup(self.context.close)
        self.metadata = {
            "doi": DOI,
            "title": TITLE,
            "authors": ["Alice Example"],
            "published": "2020",
        }

    def save(self, content=None, **kwargs):
        return save_requested_pdf(
            "springer",
            DOI,
            self.metadata,
            content if content is not None else html_content(),
            client=object(),
            context=self.context,
            **kwargs,
        )

    def test_html_success_still_downloads_and_saves_original_pdf_bytes(self):
        target, source = self.save()
        self.assertEqual(target.read_bytes(), self.pdf)
        self.assertEqual(target.suffix, ".pdf")
        self.assertEqual(source, PDF_URL)
        with pymupdf.open(target) as document:
            self.assertEqual(document.page_count, 1)
        self.assertEqual(len(list(self.output.glob("*.pdf"))), 1)

    def test_existing_pdf_payload_is_saved_without_another_http_request(self):
        content = replace(
            html_content(),
            body=self.pdf,
            content_type="application/pdf",
            route_kind="pdf_fallback",
            source_url=PDF_URL,
        )
        target, _ = self.save(content)
        self.assertEqual(target.read_bytes(), self.pdf)
        self.assertEqual(self.transport.calls, [])

    def test_html_response_cannot_be_saved_as_pdf(self):
        self.transport.responses[PDF_URL] = http_response(
            PDF_URL, b"<html>Login required</html>", "text/html"
        )
        with self.assertRaises(ProviderFailure):
            self.save()
        self.assertEqual(list(self.output.glob("*.pdf")), [])

    def test_wrong_paper_pdf_is_rejected(self):
        self.transport.responses[PDF_URL] = http_response(
            PDF_URL, make_pdf("10.1038/another"), "application/pdf"
        )
        with self.assertRaises(ProviderFailure):
            self.save()
        self.assertEqual(list(self.output.glob("*.pdf")), [])

    def test_different_existing_pdf_requires_explicit_overwrite(self):
        target, _ = self.save()
        target.write_bytes(b"previous local file")
        with self.assertRaises(FileExistsError):
            self.save()
        self.assertEqual(target.read_bytes(), b"previous local file")
        self.save(overwrite=True)
        self.assertEqual(target.read_bytes(), self.pdf)

    def test_identical_pdf_is_idempotent(self):
        target, _ = self.save()
        before = target.stat().st_mtime_ns
        self.save()
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_arxiv_version_uses_official_pdf_url(self):
        doi = "10.48550/arXiv.1706.03762v7"
        url = "https://arxiv.org/pdf/1706.03762v7"
        body = make_pdf(doi)
        self.transport.responses[url] = http_response(url, body, "application/pdf")
        target, source = save_requested_pdf(
            "arxiv",
            doi,
            {**self.metadata, "doi": doi},
            None,
            client=object(),
            context=self.context,
        )
        self.assertEqual(source, url)
        self.assertEqual(target.read_bytes(), body)

    def test_cli_flag_runs_provider_pdf_archive_and_reports_path(self):
        article = sample_article(DOI)
        article.source = "springer_html"
        article.metadata.title = TITLE

        class HtmlProvider:
            def fetch_result(self, *args, **kwargs):
                return ProviderFetchResult(
                    provider="springer", article=article, content=html_content()
                )

        def fetch(query, *, strategy, context, **kwargs):
            context.transport = self.transport
            result = _try_official_provider(
                doi=DOI,
                metadata=self.metadata,
                provider_name="springer",
                strategy=strategy,
                artifact_store=context.artifact_store,
                context=context,
                clients={"springer": HtmlProvider()},
                outputs=_ProviderAttemptOutputs(),
            )
            self.assertIsNotNone(result)
            return build_envelope(result)

        report = self.output / "manifest.json"
        with (
            mock.patch.object(cli, "fetch_paper", side_effect=fetch),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(
                [
                    "fetch",
                    "--query",
                    DOI,
                    "--download-pdf",
                    "--output-dir",
                    str(self.output),
                    "--manifest",
                    str(report),
                    "--asset-profile",
                    "none",
                    "--progress",
                    "none",
                ]
            )
        self.assertEqual(code, 0)
        record = json.loads(report.read_text(encoding="utf-8"))
        pdfs = [item for item in record["output_artifacts"] if item["kind"] == "pdf"]
        self.assertEqual(len(pdfs), 1)
        self.assertEqual(Path(pdfs[0]["path"]).read_bytes(), self.pdf)
        self.assertTrue(pdfs[0]["sha256"])

    def test_batch_records_pdf_success_and_pdf_failure_separately(self):
        article = sample_article(DOI)
        article.source = "springer_html"
        article.metadata.title = TITLE

        class HtmlProvider:
            def fetch_result(self, *args, **kwargs):
                return ProviderFetchResult(
                    provider="springer", article=article, content=html_content()
                )

        def fetch(query, *, strategy, context, **kwargs):
            if query.endswith("/missing"):
                return build_envelope(sample_article(query))
            context.transport = self.transport
            result = _try_official_provider(
                doi=DOI,
                metadata=self.metadata,
                provider_name="springer",
                strategy=strategy,
                artifact_store=context.artifact_store,
                context=context,
                clients={"springer": HtmlProvider()},
                outputs=_ProviderAttemptOutputs(),
            )
            self.assertIsNotNone(result)
            return build_envelope(result)

        queries = self.output / "queries.txt"
        queries.write_text(DOI + "\n10.1038/missing\n", encoding="utf-8")
        with (
            mock.patch.object(cli, "fetch_paper", side_effect=fetch),
            mock.patch.object(
                cli,
                "_resolve_cli_batch_item_lane",
                side_effect=lambda item, **kwargs: item,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(
                [
                    "fetch",
                    "--query-file",
                    str(queries),
                    "--download-pdf",
                    "--output-dir",
                    str(self.output),
                    "--asset-profile",
                    "none",
                    "--batch-concurrency",
                    "1",
                    "--progress",
                    "none",
                ]
            )
        self.assertNotEqual(code, 0)
        records = [
            json.loads(line)
            for line in (self.output / "batch-results.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(records), 2)
        self.assertTrue(
            any(item["kind"] == "pdf" for item in records[0]["output_artifacts"])
        )
        self.assertFalse(
            any(item["kind"] == "pdf" for item in records[1]["output_artifacts"])
        )
        self.assertIn("PDF requested", json.dumps(records[1]))
