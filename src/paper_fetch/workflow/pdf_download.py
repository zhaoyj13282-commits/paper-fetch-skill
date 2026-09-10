"""Archive an original PDF alongside a provider's HTML/XML full text."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from pathlib import Path
from tempfile import TemporaryDirectory

from ..arxiv_id import arxiv_id_from_doi, canonical_arxiv_pdf_url
from ..config import build_publisher_user_agent
from ..http import redact_url_for_diagnostics
from ..provider_catalog import provider_has_browser_route, provider_pdf_path_templates
from ..providers._pdf_candidates import (
    _append_provider_doi_pdf_candidates,
    _append_provider_source_path_pdf_candidates,
    extract_pdf_candidate_urls_from_html,
    extract_pdf_url_from_metadata_links,
)
from ..providers._pdf_common import (
    PdfFetchFailure,
    default_pdf_headers,
    pdf_fetch_result_from_bytes,
)
from ..providers._pdf_fallback import (
    PdfRequestContext,
    fetch_pdf_over_http,
    fetch_pdf_with_browser,
)
from ..providers.base import ProviderContent, ProviderFailure
from ..providers.browser_runtime.api import load_runtime_config, ensure_runtime_ready
from ..runtime import RuntimeContext
from ..utils import build_output_path, _extract_year


def pdf_candidates(
    provider: str,
    doi: str,
    metadata: Mapping[str, Any],
    content: ProviderContent | None,
    client: object,
) -> list[str]:
    """Reuse provider discovery and catalog templates for this same paper."""
    candidates: list[str] = []
    link = extract_pdf_url_from_metadata_links(metadata)
    if link:
        candidates.append(link)
    arxiv_id = arxiv_id_from_doi(doi) if provider == "arxiv" else None
    if arxiv_id:
        candidates.append(canonical_arxiv_pdf_url(arxiv_id))
    if content is not None and "html" in content.content_type.lower():
        candidates.extend(
            extract_pdf_candidate_urls_from_html(
                content.body.decode("utf-8", errors="replace"), content.source_url
            )
        )
    discover = getattr(client, "pdf_candidates", None)
    if callable(discover):
        candidates.extend(discover(doi, metadata))
    if provider == "copernicus":
        from ..providers.copernicus import _candidate_urls

        candidates.extend(
            _candidate_urls(doi, templates=provider_pdf_path_templates(provider))
        )
    for source_url in (
        content.source_url if content else None,
        metadata.get("landing_page_url"),
    ):
        _append_provider_source_path_pdf_candidates(
            candidates, provider, source_url, doi=doi
        )
    _append_provider_doi_pdf_candidates(candidates, provider, doi)
    return list(dict.fromkeys(candidates))


def save_requested_pdf(
    provider: str,
    doi: str,
    metadata: Mapping[str, Any],
    content: ProviderContent | None,
    *,
    client: object,
    context: RuntimeContext,
    overwrite: bool = False,
) -> tuple[Path, str]:
    """Validate and atomically save PDF bytes; never render HTML into a PDF."""
    store = context.artifact_store
    if store is None or store.download_dir is None or store.artifact_mode == "none":
        raise ProviderFailure(
            "not_supported", "PDF download requires an artifact output directory."
        )
    context.raise_if_cancelled()
    merged = (
        {**metadata, **dict(content.merged_metadata or {})}
        if content
        else dict(metadata)
    )
    identity = {"doi": doi, "title": metadata.get("title")}
    try:
        # API publishers own credential/header handling; reuse their official PDF lane.
        if content is None or content.route_kind != "pdf_fallback":
            if provider == "elsevier":
                from ..providers.elsevier import ElsevierClient

                content = (
                    cast(ElsevierClient, client)
                    ._fetch_official_pdf_payload(
                        doi, asset_profile="none", context=context
                    )
                    .content
                )
            elif provider == "plos":
                from ..providers.plos import PlosClient

                content = (
                    cast(PlosClient, client)
                    ._fetch_pdf_payload(
                        doi,
                        merged,
                        xml_failure_message="Original PDF explicitly requested.",
                        context=context,
                    )
                    .content
                )
        if content is not None and content.route_kind == "pdf_fallback":
            result = pdf_fetch_result_from_bytes(
                artifact_dir=None,
                source_url=content.source_url,
                final_url=content.source_url,
                pdf_bytes=content.body,
                allow_pdf_only=True,
                expected_identity=identity,
            )
        else:
            candidates = pdf_candidates(provider, doi, merged, content, client)
            if not candidates:
                raise ProviderFailure(
                    "no_result", "No original PDF URL found for this paper."
                )
            source_url = (
                content.source_url
                if content
                else str(merged.get("landing_page_url") or "")
            )
            seed = content.browser_context_seed if content else {}
            cookies = seed.get("cookies") or seed.get("browser_cookies")
            request = PdfRequestContext(
                expected_identity=identity, runtime=context, provider_name=provider
            )
            assert context.transport is not None
            try:
                result = fetch_pdf_over_http(
                    context.transport,
                    candidates,
                    headers=default_pdf_headers(
                        build_publisher_user_agent(context.env or {}),
                        referer=source_url,
                    ),
                    browser_cookies=cookies,
                    allow_pdf_only=True,
                    request=request,
                )
            except PdfFetchFailure:
                if not provider_has_browser_route(provider):
                    raise
                config = load_runtime_config(
                    context.env or {}, provider=provider, doi=doi
                )
                ensure_runtime_ready(config)
                with TemporaryDirectory(
                    prefix="paper_fetch_requested_pdf_"
                ) as temporary:
                    result = fetch_pdf_with_browser(
                        candidates,
                        artifact_dir=Path(temporary),
                        browser_config=config,
                        browser_cookies=cookies,
                        referer=source_url,
                        seed_urls=[source_url] if source_url else None,
                        allow_pdf_only=True,
                        request=request,
                    )
    except PdfFetchFailure as exc:
        raise ProviderFailure(
            "no_result",
            f"Original PDF download failed: {exc.kind}: {exc.message}",
        ) from exc
    context.raise_if_cancelled()
    target = build_output_path(
        store.download_dir,
        doi,
        merged.get("title"),
        "application/pdf",
        result.final_url,
        authors=merged.get("authors"),
        year=_extract_year(merged.get("published")),
    )
    assert target is not None
    store.write_bytes_file(
        target, result.pdf_bytes, overwrite=overwrite, commit_guard=context.commit_guard
    )
    return target, redact_url_for_diagnostics(result.final_url or result.source_url)
