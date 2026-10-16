"""Unit tests for `app.scheduling.jobs` (Epic 16 infra).

The BDD suite (`tests/features/scheduled_jobs.feature`) pins the registry's
own mechanics (due/skip/blocked/failed) using synthetic jobs. This file
covers the three REAL jobs' own wiring logic — the part `app.scheduling.jobs`
adds on top of the already-tested `app.ingest.*` / `app.corrections.*`
modules: precondition checks, the per-state/office loop and its
one-state-failing tolerance, and the house-rollcalls source-error mapping.
None of this re-tests `fetch_candidates`, `candidacies_from_results`,
`transform_roll_calls` etc. themselves — those have their own unit suites.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.bootstrap import Bootstrapper
from app.ingest.candidacies import ALL_STATES, INGESTED_OFFICES, CandidacySourceError
from app.ingest.house_rollcalls import HouseRollCallSourceError
from app.scheduling import jobs as jobs_module
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError
from app.zerodb.testing import FakeZeroDBTransport


@pytest.fixture
def client() -> ZeroDBClient:
    client = ZeroDBClient(transport=FakeZeroDBTransport(), project_id="proj-test")
    Bootstrapper(client, pace=lambda: None).run()
    return client


# --- default_jobs() composition ---------------------------------------------


def test_default_jobs_registers_exactly_the_three_documented_jobs() -> None:
    names = [job.name for job in jobs_module.default_jobs()]
    assert names == ["sweep_correction_emails", "load_candidacies", "load_house_rollcalls"]


def test_sweep_correction_emails_has_no_precondition() -> None:
    """Unlike the other two, the sweep needs no external API key — only
    ZeroDB credentials, which every job already requires to run at all.
    """
    job = next(j for j in jobs_module.default_jobs() if j.name == "sweep_correction_emails")
    assert job.precondition is None
    assert job.interval == timedelta(days=1)


def test_load_candidacies_is_blocked_without_fec_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEC_API_KEY", raising=False)
    job = next(j for j in jobs_module.default_jobs() if j.name == "load_candidacies")
    assert job.interval == timedelta(days=7)
    assert job.precondition is not None
    assert job.precondition() == "FEC_API_KEY is not set"


def test_load_candidacies_precondition_passes_once_the_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEC_API_KEY", "a-real-key")
    job = next(j for j in jobs_module.default_jobs() if j.name == "load_candidacies")
    assert job.precondition() is None


def test_load_house_rollcalls_is_blocked_without_congress_gov_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONGRESS_GOV_API_KEY", raising=False)
    job = next(j for j in jobs_module.default_jobs() if j.name == "load_house_rollcalls")
    assert job.interval == timedelta(hours=1)
    assert job.precondition is not None
    assert job.precondition() == "CONGRESS_GOV_API_KEY is not set"


# --- run_sweep_correction_emails: exercised for real against the fake ------


def test_run_sweep_correction_emails_runs_clean_against_an_empty_project(
    client: ZeroDBClient,
) -> None:
    # No ZeroDBError raised over an empty table is the behaviour the registry
    # needs to record this as RAN.
    jobs_module.run_sweep_correction_emails(client)


# --- run_load_candidacies: the loop and its fault tolerance ----------------


def test_run_load_candidacies_tolerates_one_states_source_error(
    monkeypatch: pytest.MonkeyPatch, client: ZeroDBClient
) -> None:
    """Mirrors `scripts/load_candidacies.py`'s own tolerance: one state/office
    failing to fetch must not abort the run over the other 55 states.
    """
    monkeypatch.setenv("FEC_API_KEY", "a-real-key")
    # 56 states x 2 offices through the real 1.1s/call pacer would make this
    # test take minutes; the pacing itself is `app.ingest.candidacies`'s own
    # tested concern (see `tests/test_candidacies_unit.py`), not this job
    # wrapper's, so it is a no-op here exactly like the bootstrap suite's own
    # pacer tests inject a no-op pace callback.
    monkeypatch.setattr(jobs_module.time, "sleep", lambda _seconds: None)

    calls: list[tuple[str, str]] = []

    def fake_fetch_candidates(*, election_year: int, office: str, state: str, api_key: str):
        calls.append((state, office))
        if state == ALL_STATES[0]:
            raise CandidacySourceError("simulated transient FEC failure")
        return []

    monkeypatch.setattr(jobs_module, "fetch_candidates", fake_fetch_candidates)

    # Does not raise even though the first state/office pair always fails.
    jobs_module.run_load_candidacies(client)

    # Every state/office pair was attempted exactly once — the first state's
    # failure did not stop the loop from reaching the rest.
    assert len(calls) == len(ALL_STATES) * len(INGESTED_OFFICES)
    assert calls[0] == (ALL_STATES[0], INGESTED_OFFICES[0])
    assert (ALL_STATES[-1], INGESTED_OFFICES[-1]) in calls


def test_run_load_candidacies_propagates_a_zerodb_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ZeroDB write failure is a real failure for the registry to report,
    unlike a single state's fetch failure. An unbootstrapped client (no
    tables at all) is the simplest way to force a genuine `ZeroDBError` out
    of `ElectionSeeder`'s own first write, before any fetch is even attempted.
    """
    monkeypatch.setenv("FEC_API_KEY", "a-real-key")
    bare_client = ZeroDBClient(transport=FakeZeroDBTransport(), project_id="proj-test")

    with pytest.raises(ZeroDBError):
        jobs_module.run_load_candidacies(bare_client)


# --- run_load_house_rollcalls: source-error mapping -------------------------


def test_run_load_house_rollcalls_maps_a_source_error_to_zerodb_error(
    monkeypatch: pytest.MonkeyPatch, client: ZeroDBClient
) -> None:
    """Unlike load_candidacies, there is only one source here, so any source
    failure must fail the whole job rather than being tolerated as a skip.
    """
    monkeypatch.setenv("CONGRESS_GOV_API_KEY", "a-real-key")

    def fake_fetch_roll_call_numbers(*, congress: int, session: int, api_key: str):
        raise HouseRollCallSourceError("simulated Congress.gov outage")

    monkeypatch.setattr(jobs_module, "fetch_roll_call_numbers", fake_fetch_roll_call_numbers)

    with pytest.raises(ZeroDBError, match="could not load the House roll-call source"):
        jobs_module.run_load_house_rollcalls(client)


def test_run_load_house_rollcalls_loads_the_most_recent_sample(
    monkeypatch: pytest.MonkeyPatch, client: ZeroDBClient
) -> None:
    monkeypatch.setenv("CONGRESS_GOV_API_KEY", "a-real-key")
    monkeypatch.setattr(jobs_module.time, "sleep", lambda _seconds: None)

    # Fewer numbers than HOUSE_ROLLCALLS_SAMPLE_SIZE, so every one is loaded
    # and the "most recent N" slicing is exercised without needing >20 fixtures.
    numbers = [10, 11, 12]
    fetched: list[int] = []

    def fake_fetch_roll_call_numbers(*, congress: int, session: int, api_key: str):
        assert congress == jobs_module.HOUSE_ROLLCALLS_CONGRESS
        assert session == jobs_module.HOUSE_ROLLCALLS_SESSION
        return numbers

    def fake_fetch_roll_call_item(
        *, congress: int, session: int, roll_call_number: int, api_key: str
    ):
        fetched.append(roll_call_number)
        return {
            "rollCallNumber": roll_call_number,
            "congress": congress,
            "sessionNumber": session,
            "legislationType": None,
            "legislationNumber": None,
            "voteType": "YEA-AND-NAY",
            "result": "Passed",
            "startDate": "2026-01-01T00:00:00Z",
            "voteQuestion": "On Passage",
            "totals": {"yea": 1, "nay": 0, "present": 0, "notVoting": 0},
        }

    monkeypatch.setattr(jobs_module, "fetch_roll_call_numbers", fake_fetch_roll_call_numbers)
    monkeypatch.setattr(jobs_module, "fetch_roll_call_item", fake_fetch_roll_call_item)

    jobs_module.run_load_house_rollcalls(client)

    # Fetched in descending order (most recent first), matching the CLI
    # script's own `sorted(numbers, reverse=True)` behaviour.
    assert fetched == sorted(numbers, reverse=True)
