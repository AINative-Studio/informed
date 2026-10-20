"""Regression tests for `house_bills.fetch_bill_subjects`'s page limit.

Found live during story 5.5, 2026-10-19. `fetch_bill_subjects` sent no `limit`,
the endpoint's default is 20, and the real count appears only in a `pagination`
object the function discarded -- so H.R. 1 of the 119th Congress, which carries
239 CRS legislative subject terms, came back with 19 and no error.

**Why that was a data-integrity bug and not a pagination nicety.** Story 5.5's
classifier applies prd.md's omnibus rule ("a vote on a 1,000-page bill is not a
vote for every clause") to the number of distinct taxonomy subtopics a bill's
subject list reaches. A 239-term reconciliation bill truncated to 19 terms
reaches 5 subtopics and is indistinguishable from an ordinary single-subject
bill, so the breadth rule did not fire and the omnibus received an **accepted**
`farm-subsidies-and-crop-insurance` topic tag. Truncation upstream produced a
confidently wrong classification downstream, which is the precise failure prd.md
makes unacceptable -- not a smaller or less complete one.

Confirmed against the real API before and after the fix:

    limit absent  ->  19 terms, pagination {'count': 240, 'next': ...}
    limit=250     -> 239 terms, pagination {'count': 240}

These tests pin the HTTP request the function makes and the refusal it raises
when one page cannot hold the answer, because neither is observable from the
captured `house_bills_sample.json` fixture -- that fixture holds a RESPONSE, and
the bug was in the REQUEST.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.ingest.house_bills import (
    SUBJECTS_PAGE_LIMIT,
    HouseBillSourceError,
    fetch_bill_subjects,
)


class _Response:
    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._body


def _capture(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> dict[str, Any]:
    """Patch `httpx.get` and record the params the function actually sent."""
    captured: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _Response:
        captured["url"] = url
        captured["params"] = kwargs.get("params", {})
        return _Response(body)

    monkeypatch.setattr(httpx, "get", fake_get)
    return captured


def test_the_subjects_fetch_asks_for_the_maximum_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The bug.** Without an explicit limit the endpoint returns 20 and stops.

    This asserts on the outgoing REQUEST, which is the only place the bug was
    ever visible: every captured response fixture in this suite was captured
    *after* whatever limit the capturing call happened to use.
    """
    captured = _capture(
        monkeypatch,
        {"subjects": {"policyArea": {"name": "Health"}, "legislativeSubjects": []}},
    )
    fetch_bill_subjects(congress=119, bill_type="hr", bill_number=1, api_key="unused")
    assert captured["params"].get("limit") == SUBJECTS_PAGE_LIMIT
    assert SUBJECTS_PAGE_LIMIT == 250, "250 is the endpoint's documented maximum"


def test_a_bill_whose_subjects_fit_in_one_page_returns_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary case: a count at or below the limit is returned as-is."""
    body = {
        "subjects": {
            "policyArea": {"name": "Public Lands and Natural Resources"},
            "legislativeSubjects": [{"name": "Land use and conservation"}],
        },
        "pagination": {"count": 8},
    }
    _capture(monkeypatch, body)
    got = fetch_bill_subjects(congress=119, bill_type="hr", bill_number=4219, api_key="unused")
    assert got == body


def test_a_count_exactly_at_the_limit_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary. An off-by-one here would refuse a bill that fits.

    Real and worth pinning: H.R. 1 reports `count: 240`, which is close enough
    to 250 that a `>=` instead of a `>` would have refused the very bill this
    fix exists to handle.
    """
    body = {
        "subjects": {"policyArea": {"name": "Health"}, "legislativeSubjects": []},
        "pagination": {"count": SUBJECTS_PAGE_LIMIT},
    }
    _capture(monkeypatch, body)
    assert (
        fetch_bill_subjects(congress=119, bill_type="hr", bill_number=1, api_key="unused") == body
    )


def test_more_subjects_than_one_page_is_refused_rather_than_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bill above the maximum RAISES, and the message says why.

    Deliberately a refusal rather than a silent short list or a best-effort
    partial read: a short subject list is the bug this whole module docstring is
    about, and returning one quietly would reintroduce it one bill higher up.
    No real bill observed in the 117th-119th Congresses exceeds 250 (the maximum
    seen is 240), so this guards a future bill and fails loudly with its real
    count when one arrives.
    """
    _capture(
        monkeypatch,
        {
            "subjects": {"policyArea": {"name": "Health"}, "legislativeSubjects": []},
            "pagination": {"count": SUBJECTS_PAGE_LIMIT + 1},
        },
    )
    with pytest.raises(HouseBillSourceError) as excinfo:
        fetch_bill_subjects(congress=119, bill_type="hr", bill_number=1, api_key="unused")
    message = str(excinfo.value)
    assert str(SUBJECTS_PAGE_LIMIT + 1) in message
    # The message has to explain the CONSEQUENCE, not just the condition: the
    # next person to see it needs to know a truncated list breaks the breadth
    # rule rather than merely being incomplete.
    assert "breadth" in message


def test_a_response_with_no_pagination_block_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent pagination must not be read as "too many".

    Observed live on bills with no legislative subjects at all. Treating a
    missing `pagination` as a failure would refuse every freshly-introduced
    bill, which is the opposite of the bug being fixed.
    """
    body = {"subjects": {"policyArea": {"name": "Health"}, "legislativeSubjects": []}}
    _capture(monkeypatch, body)
    assert (
        fetch_bill_subjects(congress=119, bill_type="hr", bill_number=1, api_key="unused") == body
    )


def test_the_api_key_is_sent_as_a_parameter_and_not_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key goes in the query string only, and nowhere else.

    Pinned because this story's live script runs with a real key sourced into
    the environment, and CLAUDE.md's rule is that it never reaches a command
    line or a log. A future refactor that moved it into the URL path would put
    it in every stored `source_url`.
    """
    captured = _capture(
        monkeypatch,
        {"subjects": {"policyArea": {"name": "Health"}, "legislativeSubjects": []}},
    )
    fetch_bill_subjects(congress=119, bill_type="hr", bill_number=1, api_key="not-a-real-key")
    assert captured["params"]["api_key"] == "not-a-real-key"
    assert "not-a-real-key" not in captured["url"]
