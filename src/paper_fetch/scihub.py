"""Explicit Sci-Hub PDF acquisition with an isolated, in-memory session."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .artifacts import ArtifactStore
from .http import (
    DEFAULT_SAFE_REMOTE_URL_POLICY,
    RequestFailure,
    redact_url_for_diagnostics,
)
from .pdf_limits import pdf_max_bytes
from .providers._pdf_candidates import extract_pdf_candidate_urls_from_html
from .providers._pdf_common import PdfFetchFailure, pdf_fetch_result_from_bytes
from .publisher_identity import extract_doi
from .resolve.query import ResolvedQuery
from .service import resolve_paper
from .utils import build_output_path

DEFAULT_BASE_URLS = ("https://sci-hub.ru",)


class SciHubError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def checked_url(url: str, *, previous: str | None = None, dns: bool = False) -> str:
    try:
        return DEFAULT_SAFE_REMOTE_URL_POLICY.validate(
            url, previous_url=previous, resolve_dns=dns
        ).url
    except (RequestFailure, ValueError) as exc:
        raise SciHubError("unsafe_url", "Source returned an unsafe URL.") from exc


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl, previous=req.full_url, dns=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SciHubSession:
    """No publisher profile, saved credentials, or global cookie store is loaded."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            _Redirects(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, url: str, *, data: bytes | None = None) -> dict[str, Any]:
        url = checked_url(url, dns=True)
        headers = {
            "User-Agent": "paper-fetch/6.2 (Sci-Hub PDF client)",
            "Accept": "*/*",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as result:
                limit = pdf_max_bytes()
                length = result.headers.get("Content-Length")
                if length and int(length) > limit:
                    raise SciHubError(
                        "too_large", "Response exceeds the configured PDF size limit."
                    )
                body = result.read(limit + 1)
                if len(body) > limit:
                    raise SciHubError(
                        "too_large", "Response exceeds the configured PDF size limit."
                    )
                return {"body": body, "url": result.url, "status_code": result.status}
        except urllib.error.HTTPError as exc:
            code = "challenge" if exc.code in {403, 429} else "http_error"
            raise SciHubError(code, f"Source returned HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise SciHubError(
                "network_error",
                "Source connection failed; check the mirror, network or certificate.",
            ) from exc


def pdf_links(html: str, source_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = extract_pdf_candidate_urls_from_html(html, source_url)
    for node in soup.select("iframe[src], embed[src], object[data]"):
        candidates.append(str(node.get("src") or node.get("data")))
    for node in soup.select("[onclick]"):
        # Extract a literal location assignment; never execute downloaded JavaScript.
        match = re.search(
            r'(?:window\.)?location(?:\.href)?\s*=\s*[\'"]([^\'"]+)[\'"]',
            str(node.get("onclick")),
        )
        if match:
            candidates.append(match.group(1))
    result: list[str] = []
    for raw in candidates:
        try:
            url = checked_url(
                urllib.parse.urljoin(source_url, raw), previous=source_url
            )
        except SciHubError:
            continue
        if url not in result:
            result.append(url)
    return result[:8]


def solve_altcha(challenge: dict[str, Any]) -> str:
    """Bounded ALTCHA v1 SHA-256 proof of work, as used by the current source."""
    maximum = challenge.get("maxNumber", challenge.get("maxnumber", 1000000))
    if (
        challenge.get("algorithm") != "SHA-256"
        or type(maximum) is not int
        or not 0 <= maximum <= 5000000
    ):
        raise SciHubError("challenge", "Unsupported or excessive ALTCHA challenge.")
    salt, target, signature = (
        challenge.get(k) for k in ("salt", "challenge", "signature")
    )
    if not all(
        isinstance(x, str) and 0 < len(x) <= 4096 for x in (salt, target, signature)
    ):
        raise SciHubError("challenge", "Malformed ALTCHA challenge.")
    started = time.monotonic()
    for number in range(maximum + 1):
        if number % 4096 == 0 and time.monotonic() - started > 20:
            break
        if hashlib.sha256((str(salt) + str(number)).encode()).hexdigest() == target:
            payload = {
                "algorithm": "SHA-256",
                "challenge": target,
                "salt": salt,
                "signature": signature,
                "number": number,
                "took": int((time.monotonic() - started) * 1000),
            }
            return base64.b64encode(json.dumps(payload).encode()).decode("ascii")
    raise SciHubError(
        "challenge", "ALTCHA challenge could not be solved within the work limit."
    )


def _unlock(session: Any, response: dict[str, Any]) -> dict[str, Any]:
    body, source = response["body"], response["url"]
    if body.startswith(b"%PDF-"):
        return response
    soup = BeautifulSoup(body[:2000000], "html.parser")
    widget = soup.select_one("altcha-widget[challengeurl]")
    if widget is None:
        return response
    challenge_path = str(widget.get("challengeurl"))
    script_text = "\n".join(
        node.get_text() for node in soup.select("script:not([src])")
    )
    match = re.search(r'fetch\(\s*[\'"](/captcha/solution/[0-9]+)[\'"]', script_text)
    if not re.fullmatch(r"/captcha/challenge/[0-9]+", challenge_path) or match is None:
        raise SciHubError("challenge", "Unsupported Sci-Hub verification page.")
    challenge_url = urllib.parse.urljoin(source, challenge_path)
    solution_url = urllib.parse.urljoin(source, match.group(1))
    try:
        challenge = json.loads(session.request(challenge_url)["body"])
        payload = solve_altcha(challenge)
        submitted = session.request(
            solution_url, data=json.dumps({"captcha": payload}).encode()
        )
        if json.loads(submitted["body"]).get("success") is not True:
            raise SciHubError("challenge", "Source rejected the ALTCHA solution.")
    except (ValueError, TypeError, AttributeError) as exc:
        raise SciHubError("challenge", "Invalid ALTCHA server response.") from exc
    return session.request(source)


def _identity(query: str) -> ResolvedQuery:
    doi = extract_doi(query)
    if doi:
        return ResolvedQuery(query=query, query_kind="doi", doi=doi, confidence=1.0)
    resolved = resolve_paper(query)
    if not resolved.doi or resolved.candidates:
        raise SciHubError(
            "ambiguous_query",
            "The reference did not resolve to one DOI; provide its DOI or exact title.",
        )
    return resolved


def download(
    query: str,
    *,
    base_urls: list[str] | tuple[str, ...],
    output_dir: Path,
    session: Any = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    resolved = _identity(query)
    doi = str(resolved.doi)
    client = session if session is not None else SciHubSession()
    last_error = SciHubError("not_found", "No verified PDF was returned.")
    attempts: list[dict[str, str]] = []
    for base in base_urls:
        base = checked_url(base).rstrip("/")
        lookup = base + "/" + urllib.parse.quote(doi, safe="/")
        try:
            page = _unlock(client, client.request(lookup))
            queue = [page] if page["body"].startswith(b"%PDF-") else []
            candidates = (
                []
                if queue
                else pdf_links(
                    page["body"].decode("utf-8", errors="replace"), page["url"]
                )
            )
            if not queue and not candidates:
                raise SciHubError(
                    "not_found",
                    "Source returned no PDF link (paper absent or verification required).",
                )
            for candidate in [None] if queue else candidates:
                try:
                    if queue:
                        response = queue[0]
                    else:
                        assert candidate is not None
                        response = client.request(candidate)
                    final_url = str(response["url"])
                    result = pdf_fetch_result_from_bytes(
                        artifact_dir=None,
                        source_url=lookup,
                        final_url=final_url,
                        pdf_bytes=response["body"],
                        allow_pdf_only=True,
                        expected_identity={"doi": doi, "title": resolved.title},
                    )
                    identity = result.diagnostics.get("identity", {})
                    if identity.get("status") != "match":
                        raise SciHubError(
                            "identity_unverified",
                            "PDF identity could not be verified against the requested paper.",
                        )
                    target = build_output_path(
                        output_dir, doi, resolved.title, "application/pdf", final_url
                    )
                    if target is None:
                        raise SciHubError(
                            "output_error", "Could not build a PDF output path."
                        )
                    ArtifactStore.from_download_dir(output_dir).write_bytes_file(
                        target, result.pdf_bytes, overwrite=overwrite
                    )
                    return {
                        "source": "scihub",
                        "doi": doi,
                        "title": resolved.title,
                        "path": str(target.resolve()),
                        "sha256": hashlib.sha256(result.pdf_bytes).hexdigest(),
                        "bytes": len(result.pdf_bytes),
                        "pages": result.diagnostics.get("pdf_pages"),
                        "identity": identity,
                        "source_url": redact_url_for_diagnostics(final_url),
                        "attempts": attempts,
                    }
                except PdfFetchFailure as exc:
                    last_error = SciHubError(exc.kind, exc.message)
                except SciHubError as exc:
                    last_error = exc
            raise last_error
        except SciHubError as exc:
            last_error = exc
            attempts.append(
                {"mirror": redact_url_for_diagnostics(base), "code": exc.code}
            )
    raise SciHubError(last_error.code, str(last_error))


def register_subcommand(subparsers) -> None:
    parser = subparsers.add_parser(
        "scihub", help="Explicitly download verified PDFs from Sci-Hub."
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--query", help="DOI, exact title, or reference containing a DOI."
    )
    inputs.add_argument(
        "--query-file", type=Path, help="UTF-8 file, one reference or title per line."
    )
    parser.add_argument(
        "--base-url",
        action="append",
        help="Mirror URL; repeat to try alternatives in order.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path.home() / "Downloads" / "paper-fetch"
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--overwrite", action="store_true")
    parser.set_defaults(_command_handler=run_namespace, _command_parser=parser)


def run_namespace(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        args._command_parser.error("--timeout must be positive")
    bases = args.base_url or list(DEFAULT_BASE_URLS)
    try:
        for base in bases:
            checked_url(base)
        queries = (
            [args.query]
            if args.query
            else [
                line.strip()
                for line in args.query_file.read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
        )
    except (SciHubError, OSError, UnicodeError) as exc:
        args._command_parser.error(
            str(exc) if isinstance(exc, SciHubError) else "Cannot read the query file."
        )
    if not queries:
        args._command_parser.error("The query file is empty")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = args.output_dir / "scihub-results.jsonl"
    failed = False
    for query in queries:
        try:
            result = download(
                query,
                base_urls=bases,
                output_dir=args.output_dir,
                session=SciHubSession(args.timeout),
                overwrite=args.overwrite,
            )
            record = {"query": query, "status": "downloaded", **result}
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            failed = True
            record = {
                "query": query,
                "status": "failed",
                "source": "scihub",
                "code": exc.code
                if isinstance(exc, SciHubError)
                else type(exc).__name__,
                "message": str(exc)
                if isinstance(exc, SciHubError)
                else "Resolution, network, or output failed.",
            }
        text = json.dumps(record, ensure_ascii=False)
        with manifest.open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")
        print(text, flush=True)
    return 1 if failed else 0
