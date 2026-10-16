"""Unit tests for the campaign-site archival pipeline (story 9.1).

`tests/features/archive_campaign_sites.feature` covers the acceptance
criterion end to end through `CampaignSiteLoader` with injected fakes. These
tests cover what Gherkin reads badly: the pure helpers (`extract_text`,
`render_capture_pdf`, `sha256_hex`) in isolation, and the three
network-reaching functions (`fetch_campaign_page`, `submit_to_wayback`,
`check_wayback_available`) driven through a stub transport rather than the
real network — the same pattern `tests/test_candidacies_unit.py` uses for
`fetch_candidates`, and for the same reason: no live call may be required for
CI to pass (CLAUDE.md).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.ingest.campaign_sites import (
    CampaignSiteError,
    _default_wayback_submitter,  # noqa: PLC2701 - the one internal seam worth pinning
    check_wayback_available,
    extract_text,
    fetch_campaign_page,
    render_capture_pdf,
    sha256_hex,
    submit_to_wayback,
)

# --- extract_text ------------------------------------------------------------


def test_extract_text_keeps_the_readable_content() -> None:
    html = "<html><body><h1>Hello</h1><p>World</p></body></html>"
    assert extract_text(html) == "Hello\nWorld"


def test_extract_text_strips_script_and_style_content() -> None:
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><script>alert('hi')</script><p>Real text</p></body></html>"
    )
    text = extract_text(html)
    assert "Real text" in text
    assert "color" not in text
    assert "alert" not in text


def test_extract_text_of_an_empty_document_is_empty() -> None:
    assert extract_text("<html><body></body></html>") == ""


def test_extract_text_closes_an_unbalanced_skip_tag_safely() -> None:
    """A malformed `</style>` with no matching open must not go negative.

    `_TextExtractor._skip_depth` is guarded at 0 specifically so a stray
    closing tag cannot make later ordinary text look skipped.
    """
    html = "</style><p>Still visible</p>"
    assert "Still visible" in extract_text(html)


# --- sha256_hex ---------------------------------------------------------------


def test_sha256_hex_of_text_and_its_utf8_bytes_agree() -> None:
    text = "hello world"
    assert sha256_hex(text) == sha256_hex(text.encode("utf-8"))


def test_sha256_hex_is_a_64_char_hex_digest() -> None:
    digest = sha256_hex("anything")
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex


def test_sha256_hex_differs_for_different_content() -> None:
    assert sha256_hex("a") != sha256_hex("b")


# --- render_capture_pdf --------------------------------------------------------


def test_render_capture_pdf_produces_a_real_pdf_with_content() -> None:
    pdf_bytes = render_capture_pdf(
        url="https://example.test/",
        candidacy_id="us-2026-11-03-general:...:person:fec-H6PA02205",
        captured_at="2026-10-16T22:00:00+00:00",
        html_sha256="a" * 64,
        text="I will lower prescription drug costs.",
    )
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 100


def test_render_capture_pdf_handles_empty_text_without_crashing() -> None:
    pdf_bytes = render_capture_pdf(
        url="https://example.test/",
        candidacy_id="person:fec-H6PA02205",
        captured_at="2026-10-16T22:00:00+00:00",
        html_sha256="b" * 64,
        text="",
    )
    assert pdf_bytes.startswith(b"%PDF")


def test_render_capture_pdf_escapes_markup_in_the_source_text() -> None:
    """Text containing `<`/`&` must not break reportlab's mini-markup parser."""
    pdf_bytes = render_capture_pdf(
        url="https://example.test/?a=1&b=2",
        candidacy_id="person:fec-H6PA02205",
        captured_at="2026-10-16T22:00:00+00:00",
        html_sha256="c" * 64,
        text="Costs < savings & more <b>bold claims</b>",
    )
    assert pdf_bytes.startswith(b"%PDF")


# --- fetch_campaign_page: stub transport, no real network ---------------------


class _StubTransport(httpx.BaseTransport):
    """Answers every request with one canned response, recording what was asked."""

    def __init__(
        self, status_code: int = 200, text: str = "", content_type: str = "text/html"
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content_type = content_type
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.status_code,
            text=self.text,
            headers={"content-type": self.content_type},
            request=request,
        )


def _patch_get(monkeypatch: pytest.MonkeyPatch, stub: _StubTransport, *, module: str) -> None:
    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=stub) as client:
            return client.get(url, params=kwargs.get("params"), headers=kwargs.get("headers"))

    monkeypatch.setattr(f"{module}.httpx.get", fake_get)


def test_fetch_campaign_page_returns_the_body_and_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubTransport(text="<html>hi</html>", content_type="text/html; charset=utf-8")
    _patch_get(monkeypatch, stub, module="app.ingest.campaign_sites")

    page = fetch_campaign_page("https://example.test/")

    assert page.html == "<html>hi</html>"
    assert page.content_type == "text/html; charset=utf-8"
    assert page.url == "https://example.test/"


def test_fetch_campaign_page_sends_a_browser_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirmed necessary live: two of three real sites 403 without one.

    See `DEFAULT_USER_AGENT`'s docstring for the full live finding.
    """
    stub = _StubTransport()
    _patch_get(monkeypatch, stub, module="app.ingest.campaign_sites")

    fetch_campaign_page("https://example.test/")

    sent = stub.requests[0].headers.get("user-agent", "")
    assert "python-httpx" not in sent
    assert "Mozilla" in sent


def test_fetch_campaign_page_raises_on_a_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubTransport(status_code=404)
    _patch_get(monkeypatch, stub, module="app.ingest.campaign_sites")

    with pytest.raises(CampaignSiteError, match="404"):
        fetch_campaign_page("https://example.test/missing")


def test_fetch_campaign_page_reports_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.get", boom)

    with pytest.raises(CampaignSiteError, match="could not reach"):
        fetch_campaign_page("https://example.test/")


# --- submit_to_wayback ---------------------------------------------------------


def test_submit_to_wayback_without_credentials_makes_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise AssertionError("should not have made a request with no credentials")

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.post", boom)

    outcome = submit_to_wayback("https://example.test/")

    assert outcome.ok is False
    assert "credentials" in (outcome.detail or "")


def test_submit_to_wayback_reports_the_real_live_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the exact unauthenticated shape confirmed live during this story."""

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": "You need to be logged in to use Save Page Now."},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.post", fake_post)

    outcome = submit_to_wayback(
        "https://example.test/", access_key="bad-key", secret_key="bad-secret"
    )

    assert outcome.ok is False
    assert "401" in (outcome.detail or "")


def test_submit_to_wayback_succeeds_on_the_documented_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "url": "https://example.test/",
                "job_id": "spn2-123",
                "status": "success",
                "timestamp": "20261016221413",
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.post", fake_post)

    outcome = submit_to_wayback("https://example.test/", access_key="k", secret_key="s")

    assert outcome.ok is True
    assert (
        outcome.snapshot_url == "https://web.archive.org/web/20261016221413/https://example.test/"
    )


def test_submit_to_wayback_reports_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.post", boom)

    outcome = submit_to_wayback("https://example.test/", access_key="k", secret_key="s")

    assert outcome.ok is False
    assert "failed" in (outcome.detail or "")


def test_submit_to_wayback_reports_a_non_json_success_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, text="not json", request=httpx.Request("POST", url))

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.post", fake_post)

    outcome = submit_to_wayback("https://example.test/", access_key="k", secret_key="s")

    assert outcome.ok is False
    assert "non-JSON" in (outcome.detail or "")


def test_submit_to_wayback_reports_a_success_body_with_no_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"status": "success"}, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.post", fake_post)

    outcome = submit_to_wayback("https://example.test/", access_key="k", secret_key="s")

    assert outcome.ok is False
    assert "timestamp" in (outcome.detail or "")


# --- check_wayback_available --------------------------------------------------


def test_check_wayback_available_reports_an_existing_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "url": "https://www.congress.gov/",
                "archived_snapshots": {
                    "closest": {
                        "status": "200",
                        "available": True,
                        "url": "http://web.archive.org/web/20261011211739/https://www.congress.gov/",
                        "timestamp": "20261011211739",
                    }
                },
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.get", fake_get)

    result = check_wayback_available("https://www.congress.gov/")

    assert result == "http://web.archive.org/web/20261011211739/https://www.congress.gov/"


def test_check_wayback_available_reports_none_for_no_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"url": "https://never-archived.example/", "archived_snapshots": {}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.get", fake_get)

    assert check_wayback_available("https://never-archived.example/") is None


def test_check_wayback_available_reports_none_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.get", boom)

    assert check_wayback_available("https://example.test/") is None


def test_check_wayback_available_reports_none_on_a_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("GET", url))

    monkeypatch.setattr("app.ingest.campaign_sites.httpx.get", fake_get)

    assert check_wayback_available("https://example.test/") is None


# --- _default_wayback_submitter: reads credentials from the environment ------


def test_default_wayback_submitter_reads_credentials_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAYBACK_ACCESS_KEY", "env-access")
    monkeypatch.setenv("WAYBACK_SECRET_KEY", "env-secret")
    seen: dict[str, Any] = {}

    def fake_submit(url: str, *, access_key: str | None, secret_key: str | None) -> Any:
        seen["access_key"] = access_key
        seen["secret_key"] = secret_key
        return "sentinel"

    monkeypatch.setattr("app.ingest.campaign_sites.submit_to_wayback", fake_submit)

    result = _default_wayback_submitter("https://example.test/")

    assert result == "sentinel"
    assert seen == {"access_key": "env-access", "secret_key": "env-secret"}


def test_default_wayback_submitter_with_no_env_vars_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAYBACK_ACCESS_KEY", raising=False)
    monkeypatch.delenv("WAYBACK_SECRET_KEY", raising=False)

    outcome = _default_wayback_submitter("https://example.test/")

    assert outcome.ok is False
