import importlib.util
from urllib.parse import parse_qs, urlsplit

import pymupdf
import pytest


def subject():
    assert importlib.util.find_spec("paper_fetch.institutional") is not None, (
        "Institutional download entry is missing"
    )
    from paper_fetch import institutional

    return institutional


URL = "https://www.sciencedirect.com/science/article/pii/S0044848621007377"
DOI = "10.1016/j.aquaculture.2021.737123"


def pdf(text):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), text)
        return doc.tobytes()


def test_pku_login_returns_to_requested_article():
    m = subject()
    pii, canonical = m.parse_article_url(URL + "?via=ihub")
    assert pii == "S0044848621007377"
    query = parse_qs(urlsplit(m.pku_login_url(canonical)).query)
    assert query["entityID"] == ["https://idp.pku.edu.cn/idp/shibboleth"]
    nested = parse_qs(urlsplit(query["appReturnURL"][0]).query)
    assert nested["targetURL"] == [URL]


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("www.sciencedirect.com", "www.sciencedirect.com.evil.test"),
        URL.replace("https:", "http:"),
        "https://www.sciencedirect.com/science/article/pii/../secret",
    ],
)
def test_rejects_non_article_and_lookalike_urls(url):
    with pytest.raises(ValueError):
        subject().parse_article_url(url)


def test_pdf_requires_requested_identity_and_rejects_html(tmp_path):
    m = subject()
    with pytest.raises(ValueError):
        m.save_pdf(
            b"<html>Login</html>", pii="S0044848621007377", doi=DOI, output_dir=tmp_path
        )
    with pytest.raises(ValueError):
        m.save_pdf(
            pdf("A different paper"),
            pii="S0044848621007377",
            doi=DOI,
            output_dir=tmp_path,
        )
    assert not list(tmp_path.glob("*.pdf"))


def test_pdf_saved_verbatim_with_digest_and_no_tokens(tmp_path):
    m = subject()
    data = pdf("DOI: " + DOI)
    result = m.save_pdf(data, pii="S0044848621007377", doi=DOI, output_dir=tmp_path)
    assert (tmp_path / "S0044848621007377.pdf").read_bytes() == data
    assert result["pages"] == 1
    assert result["identity_verified"] is True
    assert len(result["sha256"]) == 64
    with pytest.raises(FileExistsError):
        m.save_pdf(
            pdf("DOI: " + DOI + "\nChanged"),
            pii="S0044848621007377",
            doi=DOI,
            output_dir=tmp_path,
        )


def test_pdf_source_allowlist():
    m = subject()
    assert m.publisher_pdf_url(
        "https://pdf.sciencedirectassets.com/abc/file.pdf?token=secret"
    )
    assert not m.publisher_pdf_url("https://sciencedirectassets.com.evil.test/file.pdf")
    assert not m.publisher_pdf_url("http://www.sciencedirect.com/file.pdf")


def test_cli_does_not_print_browser_tokens(monkeypatch, capsys):
    m = subject()

    def fail(**kwargs):
        raise RuntimeError("https://idp.pku.edu.cn/login?ticket=private-token")

    monkeypatch.setattr(m, "download_article", fail)
    assert m.main(["--url", URL]) == 1
    output = capsys.readouterr()
    assert "private-token" not in output.out + output.err
    assert "RuntimeError" in output.err


def test_oversized_response_not_saved(monkeypatch, tmp_path):
    m = subject()
    monkeypatch.setattr(m, "pdf_max_bytes", lambda: 10)
    with pytest.raises(ValueError):
        m.save_pdf(
            pdf("DOI: " + DOI), pii="S0044848621007377", doi=DOI, output_dir=tmp_path
        )
    assert not list(tmp_path.glob("*.pdf"))


def test_download_uses_same_session_and_writes_verified_report(monkeypatch, tmp_path):
    import contextlib
    import json
    from types import SimpleNamespace

    m = subject()
    payload = pdf("DOI: " + DOI)
    events = {}
    visits = []

    class Locator:
        first = None

        def __init__(self, selector):
            self.selector = selector
            self.first = self

        def count(self):
            return 1

        def get_attribute(self, name):
            return DOI

        def inner_text(self, **kwargs):
            return "Access through Peking University"

        def evaluate_all(self, script):
            return [{"href": URL + "/pdfft?download=true", "text": "View PDF"}]

    class Page:
        url = "about:blank"

        def on(self, name, callback):
            pass

        def title(self):
            return "ScienceDirect"

        def set_default_timeout(self, value):
            pass

        def goto(self, url, **kwargs):
            visits.append(url)
            self.url = URL
            if "/pdfft" in url:
                events["response"](
                    SimpleNamespace(
                        url=url,
                        status=200,
                        headers={"content-type": "application/pdf"},
                        body=lambda: payload,
                    )
                )

        def locator(self, selector):
            return Locator(selector)

        def wait_for_timeout(self, value):
            pass

    class Context:
        def __init__(self):
            self.pages = []

        def on(self, name, callback):
            events[name] = callback

        def new_page(self):
            page = Page()
            self.pages.append(page)
            events["page"](page)
            return page

    opened = []

    @contextlib.contextmanager
    def session(profile, browser):
        opened.append((profile, browser))
        yield Context()

    monkeypatch.setattr(m, "browser_session", session)
    profile = tmp_path / "profile"
    output = tmp_path / "papers"
    result = m.download_article(
        url=URL, output_dir=output, profile_dir=profile, browser="msedge"
    )
    assert opened == [(profile, "msedge")]
    assert visits == [m.pku_login_url(URL), URL + "/pdfft?download=true"]
    assert result["institution_label_observed"] is True
    assert result["idp_page_observed"] is False  # Never infer an unseen login.
    assert (output / "S0044848621007377.pdf").read_bytes() == payload
    assert json.loads((output / "S0044848621007377.json").read_text()) == result


def test_cli_profiles_are_isolated_by_browser(monkeypatch, tmp_path):
    m = subject()
    calls = []
    monkeypatch.setattr(m, "user_data_path", lambda app: tmp_path)
    monkeypatch.setattr(m, "download_article", lambda **kw: calls.append(kw) or {})
    assert m.main(["--url", URL, "--browser", "msedge"]) == 0
    assert m.main(["--url", URL, "--browser", "camoufox"]) == 0
    assert calls[0]["profile_dir"] != calls[1]["profile_dir"]
    assert calls[0]["browser"] == "msedge"


def test_publisher_challenge_has_actionable_status():
    m = subject()
    assert (
        m.publisher_wait_message("请稍候…")
        == "Publisher verification is pending; complete any visible verification in the browser."
    )
    assert m.publisher_wait_message("Just a moment...")
    assert m.publisher_wait_message("ScienceDirect") is None
