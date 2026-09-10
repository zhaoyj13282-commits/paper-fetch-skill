import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pymupdf
import pytest


def subject():
    assert importlib.util.find_spec("paper_fetch.scihub") is not None, (
        "Sci-Hub adapter is missing"
    )
    from paper_fetch import scihub

    return scihub


DOI = "10.1038/171737a0"
BASE = "https://sci-hub.example"


def make_pdf(doi=DOI):
    with pymupdf.open() as doc:
        doc.new_page().insert_text(
            (72, 72), "Molecular structure of nucleic acids\nDOI: " + doi
        )
        return doc.tobytes()


@pytest.mark.parametrize(
    "html,expected",
    [
        (
            '<iframe id="pdf" src="//cdn.example/a.pdf#view=FitH"></iframe>',
            "https://cdn.example/a.pdf",
        ),
        ('<embed src="/paper/a.pdf#navpanes=0">', BASE + "/paper/a.pdf"),
        ('<object data="a.pdf"></object>', BASE + "/a.pdf"),
        (
            "<button onclick=\"location.href='/paper/a.pdf'\">save</button>",
            BASE + "/paper/a.pdf",
        ),
    ],
)
def test_pdf_link_formats(html, expected):
    assert expected in subject().pdf_links(html, BASE + "/lookup")


def test_altcha_solution_matches_challenge_and_limits_work():
    m = subject()
    d = {
        "algorithm": "SHA-256",
        "salt": "sample-salt",
        "challenge": hashlib.sha256(b"sample-salt42").hexdigest(),
        "maxNumber": 100,
        "signature": "opaque-server-signature",
    }
    payload = json.loads(base64.b64decode(m.solve_altcha(d)))
    assert payload["number"] == 42
    assert payload["signature"] == d["signature"]
    with pytest.raises(m.SciHubError):
        m.solve_altcha({**d, "maxNumber": 10**12})


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, url, *, data=None):
        self.calls.append((url, data))
        return self.responses[url].pop(0)


def response(url, body):
    return {"url": url, "body": body, "status_code": 200}


def test_download_validates_and_keeps_original_bytes(tmp_path):
    m = subject()
    lookup = BASE + "/" + DOI
    pdf_url = BASE + "/paper.pdf"
    raw = make_pdf()
    s = Session(
        {
            lookup: [response(lookup, b'<iframe src="/paper.pdf"></iframe>')],
            pdf_url: [response(pdf_url, raw)],
        }
    )
    result = m.download(DOI, base_urls=[BASE], output_dir=tmp_path, session=s)
    assert Path(result["path"]).read_bytes() == raw
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["identity"]["status"] == "match"
    assert result["source"] == "scihub"


def test_wrong_paper_cannot_be_saved(tmp_path):
    m = subject()
    lookup = BASE + "/" + DOI
    s = Session({lookup: [response(lookup, make_pdf("10.1038/unrelated"))]})
    with pytest.raises(m.SciHubError):
        m.download(DOI, base_urls=[BASE], output_dir=tmp_path, session=s)
    assert not list(tmp_path.glob("*.pdf"))


def test_ambiguous_title_stops_before_source_request(monkeypatch, tmp_path):
    from paper_fetch.resolve.query import ResolvedQuery

    m = subject()
    monkeypatch.setattr(
        m,
        "resolve_paper",
        lambda q: ResolvedQuery(query=q, query_kind="title", candidates=[{"doi": DOI}]),
    )
    s = Session({})
    with pytest.raises(m.SciHubError):
        m.download("ambiguous title", base_urls=[BASE], output_dir=tmp_path, session=s)
    assert s.calls == []


def test_altcha_roundtrip_reloads_with_same_session(tmp_path):
    m = subject()
    lookup = BASE + "/" + DOI
    html = b'<altcha-widget challengeurl="/captcha/challenge/123"></altcha-widget><script>fetch("/captcha/solution/123", {method:"POST"})</script>'
    d = {
        "algorithm": "SHA-256",
        "salt": "x",
        "challenge": hashlib.sha256(b"x2").hexdigest(),
        "maxNumber": 10,
        "signature": "signed",
    }
    s = Session(
        {
            lookup: [response(lookup, html), response(lookup, make_pdf())],
            BASE + "/captcha/challenge/123": [response("", json.dumps(d).encode())],
            BASE + "/captcha/solution/123": [response("", b'{"success":true}')],
        }
    )
    assert (
        m.download(DOI, base_urls=[BASE], output_dir=tmp_path, session=s)["identity"][
            "status"
        ]
        == "match"
    )
    assert json.loads(s.calls[2][1])["captcha"]
    assert s.calls[-1][0] == lookup


def test_batch_continues_and_returns_failure(monkeypatch, tmp_path, capsys):
    from paper_fetch import cli

    m = subject()
    source = tmp_path / "queries.txt"
    source.write_text("first\nsecond\n", encoding="utf-8")
    seen = []

    def fetch(query, **kwargs):
        seen.append(query)
        if query == "first":
            raise m.SciHubError("not_found", "No PDF")
        return {"source": "scihub", "doi": DOI, "path": "sample.pdf"}

    monkeypatch.setattr(m, "download", fetch)
    assert (
        cli.main(["scihub", "--query-file", str(source), "--output-dir", str(tmp_path)])
        == 1
    )
    assert seen == ["first", "second"]
    rows = [
        json.loads(x)
        for x in (tmp_path / "scihub-results.jsonl").read_text().splitlines()
    ]
    assert [x["status"] for x in rows] == ["failed", "downloaded"]


def test_parser_discards_unsafe_links():
    m = subject()
    html = '<iframe src="http://127.0.0.1/admin"></iframe><embed src="javascript:alert(1)"><object data="file:///secret.pdf"></object>'
    assert m.pdf_links(html, BASE + "/lookup") == []


def test_altcha_cannot_submit_to_external_endpoint(tmp_path):
    m = subject()
    lookup = BASE + "/" + DOI
    body = b'<altcha-widget challengeurl="https://other.example/challenge"></altcha-widget><script>fetch("/captcha/solution/1")</script>'
    s = Session({lookup: [response(lookup, body)]})
    with pytest.raises(m.SciHubError):
        m.download(DOI, base_urls=[BASE], output_dir=tmp_path, session=s)
    assert len(s.calls) == 1


def test_invalid_cli_input_is_a_usage_error(tmp_path):
    from paper_fetch import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["scihub", "--query-file", str(tmp_path / "missing.txt")])
    assert exc.value.code == 2


def test_title_resolution_is_reused(monkeypatch, tmp_path):
    from paper_fetch.resolve.query import ResolvedQuery

    m = subject()
    monkeypatch.setattr(
        m,
        "resolve_paper",
        lambda q: ResolvedQuery(
            query=q,
            query_kind="title",
            doi=DOI,
            title="Molecular structure of nucleic acids",
            confidence=1.0,
        ),
    )
    lookup = BASE + "/" + DOI
    s = Session({lookup: [response(lookup, make_pdf())]})
    result = m.download(
        "Molecular structure of nucleic acids",
        base_urls=[BASE],
        output_dir=tmp_path,
        session=s,
    )
    assert result["doi"] == DOI
