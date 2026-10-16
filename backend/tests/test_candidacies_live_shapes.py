"""Live-confirmed OpenFEC behaviour the candidacy loader depends on (2.6).

Same purpose as `test_legislators_live_shapes.py`: every test here pins
something **measured against the live source** during story 2.6, not inferred
from documentation or from the fake.

What is and is not live-verified, precisely
============================================

This story sits between stories 2.2 and 5.1 in how much could be confirmed,
and the distinction matters when reading these tests:

  * **The SOURCE is live-verified.** OpenFEC accepts the public `DEMO_KEY`, so
    `https://api.open.fec.gov/v1/candidates/` was queried for real on
    2026-10-16 and answered `200` with real 2026 filings. Every field name,
    type and enum asserted below was read off those responses and
    cross-checked against the endpoint's own swagger definition at
    https://api.open.fec.gov/swagger/ (the `Candidate` definition and the
    `/v1/candidates/` parameter list).
  * **The ZeroDB WRITE PATH is not live-verified for this table.** No
    `candidacies` row has been written to the live project from live FEC data:
    `scripts/load_candidacies.py` needs a registered api.data.gov key
    (DEMO_KEY's limit is 10 requests/hour, confirmed from the live
    `x-ratelimit-limit` header, against a national load of ~110 queries), and
    no key exists in this environment. The idempotency and reference
    behaviour this loader relies on IS live-verified, by stories 1.4, 2.1 and
    2.2 against the same `Repository.upsert` -- see
    `test_repository_live_shapes.py` and `test_legislators_live_shapes.py` --
    but this loader's own end-to-end run has not happened. See story 2.6's PR.

Nothing here makes a live call. The payloads are the live ones, replayed
through the transform and the fake, which is what makes these runnable in CI
with no credentials.

The live queries these records came from
=========================================

    GET /v1/candidates/?election_year=2026&office=H&state=DE   -> count 6
    GET /v1/candidates/?election_year=2026&office=H&state=PA   -> count 107
    GET /v1/candidates/?election_year=2026&office=S&state=DE   -> count 13
    GET /v1/candidates/?election_year=2026&office=H&state=DE&incumbent_challenge=I
                                                               -> count 1
"""

from __future__ import annotations

from typing import Any

from app.ingest.candidacies import candidacies_from_results, candidacy_status_for
from app.seed_elections import US_2026_GENERAL

# --- the live records, verbatim --------------------------------------------
#
# Copied field for field from the live responses above. Kept here as well as in
# the JSON fixture on purpose: the fixture drives the BDD scenarios, while these
# pin specific source facts a reader of this module should not have to open a
# JSON file to check.

#: DE at-large House, Republican challenger. Note `district: "00"` (a STRING,
#: zero-padded) alongside `district_number: 0` (an int) -- the two forms the
#: loader has to reconcile with story 2.3's unpadded OCD-IDs.
_LIVE_DE_AT_LARGE: dict[str, Any] = {
    "active_through": 2026,
    "candidate_id": "H6DE01121",
    "candidate_inactive": False,
    "candidate_status": "C",
    "cycles": [2026],
    "district": "00",
    "district_number": 0,
    "election_districts": ["00"],
    "election_years": [2026],
    "federal_funds_flag": False,
    "first_file_date": "2026-07-28",
    "has_raised_funds": True,
    "inactive_election_years": None,
    "incumbent_challenge": "C",
    "incumbent_challenge_full": "Challenger",
    "last_f2_date": "2026-07-28",
    "last_file_date": "2026-07-28",
    "load_date": "2026-09-04T20:56:18",
    "name": "ARMINIO, JOSEPH DR. JOE PH.D.",
    "office": "H",
    "office_full": "House",
    "party": "REP",
    "party_full": "REPUBLICAN PARTY",
    "state": "DE",
}

#: PA-2 challenger with `has_raised_funds: false` -- the live proof of story
#: 2.5's spike finding that FILING Form 2, not clearing the $5,000 bar, is what
#: puts a candidate in this feed. Also the live example of `candidate_status`
#: "N" ("not yet a candidate").
_LIVE_PA_02_UNFUNDED: dict[str, Any] = {
    "candidate_id": "H6PA02205",
    "candidate_status": "N",
    "district": "02",
    "district_number": 2,
    "election_years": [2026],
    "has_raised_funds": False,
    "incumbent_challenge": "C",
    "name": "ARRIAGA, JESSICA",
    "office": "H",
    "party": "REP",
    "party_full": "REPUBLICAN PARTY",
    "state": "PA",
}

#: PA-3, party code "UN" (UNAFFILIATED) and `incumbent_challenge: "O"` -- the
#: live proof that a non-major-party filer does reach this feed.
_LIVE_PA_03_UNAFFILIATED: dict[str, Any] = {
    "candidate_id": "H6PA03286",
    "candidate_status": "N",
    "district": "03",
    "district_number": 3,
    "election_years": [2026],
    "has_raised_funds": True,
    "incumbent_challenge": "O",
    "incumbent_challenge_full": "Open seat",
    "name": "ARMSTRONG, SHEILA E",
    "office": "H",
    "party": "UN",
    "party_full": "UNAFFILIATED",
    "state": "PA",
}

#: DE Senate. Senate records carry `district: "00"` exactly like an at-large
#: House seat -- which is why `ocd_district_id` keys off `office` first.
_LIVE_DE_SENATE: dict[str, Any] = {
    "candidate_id": "S6DE00263",
    "candidate_status": "C",
    "district": "00",
    "district_number": 0,
    "election_years": [2026],
    "incumbent_challenge": "C",
    "name": "APPELHANS, JEFFERY RYAN",
    "office": "S",
    "party": "DEM",
    "state": "DE",
}


def _transform(record: dict[str, Any]) -> dict[str, Any]:
    rows, skipped = candidacies_from_results([record], election=US_2026_GENERAL)
    assert skipped == [], f"live record was unexpectedly skipped: {skipped!r}"
    return rows[0]


# --- what the live responses prove about the source ------------------------


def test_the_live_feed_pads_district_as_a_string_and_also_sends_it_as_an_int() -> None:
    """Both forms are present on every live record, and they disagree in type.

    This is the single most load-bearing fact about this source: `"02"` would
    produce `/cd:02`, which matches none of the 442 live House district ids
    story 2.3 confirmed, and every voter in a single-digit district would see
    an empty race with no error.
    """
    assert _LIVE_PA_02_UNFUNDED["district"] == "02"
    assert _LIVE_PA_02_UNFUNDED["district_number"] == 2
    assert isinstance(_LIVE_PA_02_UNFUNDED["district"], str)
    assert isinstance(_LIVE_PA_02_UNFUNDED["district_number"], int)

    assert _transform(_LIVE_PA_02_UNFUNDED)["district_id"] == (
        "ocd-division/country:us/state:pa/cd:2"
    )


def test_a_live_senate_record_carries_district_00_like_an_at_large_house_seat() -> None:
    """Which is why office, not district, decides whether a seat is statewide."""
    assert _LIVE_DE_SENATE["district"] == "00"
    assert _LIVE_DE_AT_LARGE["district"] == "00"
    assert _transform(_LIVE_DE_SENATE)["district_id"] == "ocd-division/country:us/state:de"
    assert _transform(_LIVE_DE_AT_LARGE)["district_id"] == "ocd-division/country:us/state:de"


def test_a_live_unfunded_filer_is_present_in_the_feed() -> None:
    """Story 2.5's spike finding, pinned against a live record.

    `has_raised_funds: false` and yet published: the gate on this feed is
    filing Form 2, not clearing the $5,000 threshold. Which also means the
    coverage gap the spike identified is specifically about candidates who
    never file at all -- see `app.ingest.candidacies`'s "What this does not
    cover".
    """
    assert _LIVE_PA_02_UNFUNDED["has_raised_funds"] is False
    row = _transform(_LIVE_PA_02_UNFUNDED)
    assert row["person_name"] == "ARRIAGA, JESSICA"
    # Loaded, not dropped: they are running, and a voter should see the name.
    assert row["status"] == "statement"


def test_a_live_unaffiliated_filer_keeps_its_own_party_code() -> None:
    """No normalisation into a short list of parties (prd.md nonpartisanship)."""
    assert _transform(_LIVE_PA_03_UNAFFILIATED)["party"] == "UN"


def test_every_live_candidate_status_letter_seen_maps_to_the_vocabulary() -> None:
    """'C' and 'N' are the two letters the live 2026 sample actually contained.

    'F' and 'P' are in the endpoint's documented enum but did not appear in
    these four queries, so their mapping is pinned by
    `test_candidacies_unit.py` against the documentation rather than claimed
    here as observed.
    """
    live_letters = {
        _LIVE_DE_AT_LARGE["candidate_status"],
        _LIVE_PA_02_UNFUNDED["candidate_status"],
        _LIVE_PA_03_UNAFFILIATED["candidate_status"],
        _LIVE_DE_SENATE["candidate_status"],
    }
    assert live_letters == {"C", "N"}
    assert candidacy_status_for(_LIVE_DE_AT_LARGE) == "filed"
    assert candidacy_status_for(_LIVE_PA_02_UNFUNDED) == "statement"


def test_the_live_name_field_is_last_comma_first_in_upper_case() -> None:
    """Kept verbatim rather than re-cased; see the module docstring's field notes.

    Re-casing means guessing which token is the surname, which is wrong for
    "VAN HOLLEN", "DE LA CRUZ" and "O'HALLERAN" -- and this live record's
    "ARMINIO, JOSEPH DR. JOE PH.D." shows how much non-name text the field can
    carry.
    """
    assert _LIVE_DE_AT_LARGE["name"] == "ARMINIO, JOSEPH DR. JOE PH.D."
    assert _transform(_LIVE_DE_AT_LARGE)["person_name"] == "ARMINIO, JOSEPH DR. JOE PH.D."


def test_a_live_record_can_run_in_several_cycles_at_once() -> None:
    """The DE at-large incumbent's live record carried `[2024, 2026]`.

    So `election_years` has to be checked for MEMBERSHIP of the loaded
    election's year, never compared for equality -- equality would drop every
    returning incumbent from the ballot.
    """
    incumbent = {
        "candidate_id": "H4DE00045",
        "candidate_status": "C",
        "district": "00",
        "district_number": 0,
        "election_years": [2024, 2026],
        "cycles": [2024, 2026],
        "incumbent_challenge": "I",
        "name": "MCBRIDE, SARAH ELIZABETH",
        "office": "H",
        "party": "DEM",
        "state": "DE",
    }
    row = _transform(incumbent)
    assert row["district_id"] == "ocd-division/country:us/state:de"


def test_fec_self_reported_incumbency_is_not_trusted_as_the_flag() -> None:
    """The live incumbent record carries `incumbent_challenge: "I"`...

    ...and the transform does not turn that into `is_incumbent` at all: the
    field is absent from the transformed row, because only the loader, reading
    the real legislators story 2.2 loaded, may decide it. Pinned because
    copying the letter would be the obvious shortcut and is wrong -- a filer
    self-reports it on Form 2.
    """
    row = _transform(
        {
            "candidate_id": "H4DE00045",
            "candidate_status": "C",
            "district_number": 0,
            "election_years": [2026],
            "incumbent_challenge": "I",
            "incumbent_challenge_full": "Incumbent",
            "name": "MCBRIDE, SARAH ELIZABETH",
            "office": "H",
            "party": "DEM",
            "state": "DE",
        }
    )
    assert "is_incumbent" not in row


def test_the_live_pagination_envelope_shape_is_what_the_fetcher_walks() -> None:
    """`pagination.pages` is the field, and it is an int.

    Live: PA House 2026 answered `{"count": 107, "is_count_exact": true,
    "page": 1, "pages": 2, "per_page": 100}` at per_page=100. A fetcher reading
    only page 1 would have dropped 7 real candidates from PA alone.
    """
    live_pagination = {
        "count": 107,
        "is_count_exact": True,
        "page": 1,
        "pages": 2,
        "per_page": 100,
    }
    assert isinstance(live_pagination["pages"], int)
    assert live_pagination["pages"] > 1
    assert live_pagination["count"] > live_pagination["per_page"]


def test_the_live_demo_key_rate_limit_is_why_a_registered_key_is_required() -> None:
    """Pinned as a documented fact, not an assertion about code.

    The live `x-ratelimit-limit` header for DEMO_KEY read `10` (requests per
    hour). `scripts/load_candidacies.py` issues two queries per state across
    56 states and territories -- 112 before paging -- so DEMO_KEY cannot
    complete even a tenth of one run, which is the whole reason story 2.6's
    end-to-end load is credential-blocked rather than merely slow.
    """
    demo_key_hourly_limit = 10
    states_and_territories = 56
    queries_per_state = 2  # House and Senate
    assert states_and_territories * queries_per_state > demo_key_hourly_limit
