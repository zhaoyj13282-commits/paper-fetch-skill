"""Single-article PKU/ScienceDirect institutional PDF download.

Run with python -m paper_fetch.institutional. Authentication is entered in the
headed browser; the persistent profile is private local state, never an artifact.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from platformdirs import user_data_path

from .artifacts import ArtifactStore
from .pdf_limits import pdf_max_bytes
from .providers.browser_runtime.camoufox_manager import CamoufoxPersistentContextManager

ARTICLE_ROOT = "https://www.sciencedirect.com/science/article/pii/"
PKU_IDP_HOSTS = frozenset({"idp.pku.edu.cn", "iaaa.pku.edu.cn"})


class InstitutionalDownloadError(Exception):
    """An error message safe to show without authentication URL details."""


def parse_article_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    match = re.fullmatch(r"/science/article/pii/(S[0-9A-Z]{16})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.sciencedirect.com", "sciencedirect.com"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or not match
    ):
        raise ValueError("Expected an HTTPS ScienceDirect article URL with a PII.")
    pii = match.group(1)
    return pii, ARTICLE_ROOT + pii


def pku_login_url(article_url: str) -> str:
    _, canonical = parse_article_url(article_url)
    router = "https://www.sciencedirect.com/user/router/shib?" + urlencode(
        {"targetURL": canonical}
    )
    return "https://auth.elsevier.com/ShibAuth/institutionLogin?" + urlencode(
        {
            "entityID": "https://idp.pku.edu.cn/idp/shibboleth",
            "appReturnURL": router,
        }
    )


def publisher_pdf_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in {None, 443}
        and (
            host == "www.sciencedirect.com"
            or host == "sciencedirect.com"
            or host == "sciencedirectassets.com"
            or host.endswith(".sciencedirectassets.com")
        )
    )


def publisher_wait_message(title: str) -> str | None:
    if title.strip().lower().rstrip(".\u2026") in {
        "just a moment",
        "\u8bf7\u7a0d\u5019",
    }:
        return "Publisher verification is pending; complete any visible verification in the browser."
    return None


def save_pdf(
    data: bytes, *, pii: str, doi: str, output_dir: Path, overwrite: bool = False
) -> dict[str, Any]:
    """Validate exact target evidence before publishing the unmodified bytes."""
    import pymupdf

    if not re.fullmatch(r"S[0-9A-Z]{16}", pii):
        raise ValueError("Invalid PII.")
    if not data.startswith(b"%PDF-") or len(data) > pdf_max_bytes():
        raise ValueError("Response is not a PDF within the configured size limit.")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            pages = len(document)
            if document.needs_pass or not 0 < pages <= 2000:
                raise ValueError("PDF must be readable and contain 1-2000 pages.")
            text = "\n".join(page.get_text() for page in document)
    except Exception as exc:
        raise ValueError("Response is not a readable non-empty PDF.") from exc
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    doi_match = bool(
        doi and re.search(re.escape(doi.lower()) + r"(?![a-z0-9./])", text.lower())
    )
    if pii.lower() not in compact and not doi_match:
        raise ValueError("PDF does not contain the requested PII or article-page DOI.")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (pii + ".pdf")
    store = ArtifactStore.from_download_dir(output_dir)
    store.write_bytes_file(path, data, overwrite=overwrite)
    return {
        "path": str(path.resolve()),
        "bytes": len(data),
        "pages": pages,
        "sha256": hashlib.sha256(data).hexdigest(),
        "identity_verified": True,
        "doi": doi or None,
        "pii": pii,
        "article_url": ARTICLE_ROOT + pii,
    }


@contextlib.contextmanager
def browser_session(profile_dir: Path, browser: str):
    """The standalone helper may use an explicitly selected installed browser."""
    if browser == "camoufox":
        manager = CamoufoxPersistentContextManager(
            user_data_dir=str(profile_dir.resolve()), headless=False
        )
        try:
            yield manager.new_context()
        finally:
            manager.close()
    elif browser in {"chrome", "msedge"}:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir.resolve()),
                channel=browser,
                headless=False,
                accept_downloads=True,
            )
            try:
                yield context
            finally:
                context.close()
    else:
        raise ValueError("Unsupported browser.")


def download_article(
    *,
    url: str,
    output_dir: Path,
    profile_dir: Path,
    timeout_seconds: int = 600,
    overwrite: bool = False,
    browser: str = "camoufox",
) -> dict[str, Any]:
    """Keep login and download inside the same native browser session."""
    pii, article_url = parse_article_url(url)
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending: list[bytes] = []
    doi = ""
    visited_idp = False
    institution_label_seen = False
    attempted: set[str] = set()
    last_host = ""
    last_failure = ""
    reported_waits: set[str] = set()

    def on_response(response: Any) -> None:
        try:
            if (
                publisher_pdf_url(response.url)
                and response.status == 200
                and "application/pdf"
                in response.headers.get("content-type", "").lower()
            ):
                size = int(response.headers.get("content-length", "0"))
                if size <= pdf_max_bytes():
                    body = response.body()
                    if len(body) <= pdf_max_bytes():
                        pending.append(body)
        except Exception:
            # A browser download event may own the body instead of the response.
            pass

    def on_download(download: Any) -> None:
        try:
            if not publisher_pdf_url(download.url):
                return
            path = download.path()
            if path and Path(path).stat().st_size <= pdf_max_bytes():
                pending.append(Path(path).read_bytes())
        except Exception:
            pass

    with browser_session(profile_dir, browser) as context:
        context.on("response", on_response)
        context.on("page", lambda page: page.on("download", on_download))
        page = context.new_page()
        page.set_default_timeout(3000)
        print(
            "Opening PKU institutional login. Enter credentials only in the browser.",
            flush=True,
        )
        try:
            page.goto(
                pku_login_url(article_url), wait_until="domcontentloaded", timeout=60000
            )
        except Exception:
            print("Navigation is still pending; waiting for the browser.", flush=True)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not context.pages:
                raise InstitutionalDownloadError(
                    "Browser closed before a verified PDF was downloaded."
                )
            for active in list(context.pages):
                host = urlsplit(active.url).hostname or ""
                visited_idp = visited_idp or host in PKU_IDP_HOSTS
                if host != last_host and host:
                    print("Browser host: " + host, flush=True)
                    last_host = host
                if host not in {"www.sciencedirect.com", "sciencedirect.com"}:
                    continue
                try:
                    waiting = publisher_wait_message(active.title())
                    if waiting:
                        last_failure = waiting
                        if waiting not in reported_waits:
                            print(waiting, flush=True)
                            reported_waits.add(waiting)
                        continue
                    if (
                        urlsplit(active.url).path.rstrip("/")
                        != "/science/article/pii/" + pii
                    ):
                        continue
                    meta = active.locator('meta[name="citation_doi"]')
                    if meta.count():
                        doi = (meta.first.get_attribute("content") or "").strip()
                    body_text = active.locator("body").inner_text(timeout=2000)
                    institution_label_seen = (
                        institution_label_seen
                        or "peking university" in body_text.lower()
                    )
                    links = active.locator("a[href]").evaluate_all(
                        "(nodes) => nodes.map(n => ({href:n.href, text:n.innerText}))"
                    )
                    for link in links:
                        href = link["href"]
                        if (
                            href in attempted
                            or not publisher_pdf_url(href)
                            or not re.search(r"pdfft|/pdf(?:[/?#]|$)", href, re.I)
                        ):
                            continue
                        attempted.add(href)
                        print("Opening the publisher PDF link.", flush=True)
                        pdf_page = context.new_page()
                        # PDF navigation normally becomes a download.
                        with contextlib.suppress(Exception):
                            pdf_page.goto(
                                href, wait_until="domcontentloaded", timeout=45000
                            )
                        break
                except Exception:
                    # Redirects can replace the document while it is being read.
                    continue
            while pending:
                try:
                    result = save_pdf(
                        pending.pop(0),
                        pii=pii,
                        doi=doi,
                        output_dir=output_dir,
                        overwrite=overwrite,
                    )
                except ValueError as exc:
                    last_failure = str(exc)
                    continue
                result.update(
                    {
                        "institution": "Peking University",
                        "idp_page_observed": visited_idp,
                        "institution_label_observed": institution_label_seen,
                    }
                )
                report = output_dir / (pii + ".json")
                ArtifactStore.from_download_dir(output_dir).write_bytes_file(
                    report,
                    (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(),
                    overwrite=True,
                )
                return result
            context.pages[0].wait_for_timeout(1000)
        raise InstitutionalDownloadError(
            last_failure
            or "Timed out before a verified PDF was downloaded. Complete login or check article entitlement."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download one ScienceDirect PDF using PKU institutional login."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--output-dir", type=Path, default=Path.home() / "Downloads" / "paper-fetch"
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--browser", choices=["camoufox", "chrome", "msedge"], default="camoufox"
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    try:
        parse_article_url(args.url)
        result = download_article(
            url=args.url,
            output_dir=args.output_dir,
            profile_dir=args.profile_dir
            or user_data_path("paper-fetch")
            / "institutional"
            / ("pku-sciencedirect-" + args.browser),
            timeout_seconds=args.timeout_seconds,
            overwrite=args.overwrite,
            browser=args.browser,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, InstitutionalDownloadError, FileExistsError) as exc:
        print("Download failed: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Download cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Playwright errors may embed URLs with authentication tokens.
        print(
            "Browser failed (" + type(exc).__name__ + "); retry the local login.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
