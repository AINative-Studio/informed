"""Unit tests for `app.scheduling.jobs` (Epic 16 infra).

The BDD suite (`tests/features/scheduled_jobs.feature`) pins the registry's
own mechanics (due/skip/blocked/failed) using synthetic jobs. This file
covers the five REAL jobs' own wiring logic — the part `app.scheduling.jobs`
adds on top of the already-tested `app.ingest.*` / `app.corrections.*` /
`app.integrity.*` modules: precondition checks, the per-state/office loop
and its one-state-failing tolerance, the house-rollcalls source-error
mapping, and (stories 16.2/16.4) turning an integrity violation into a job
failure and reporting a drift repair's result. None of this re-tests
`fetch_candidates`, `candidacies_from_results`, `transform_roll_calls`,
`run_all_checks` or `repair_member_votes_drift` themselves — those have their
own unit/BDD suites.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.bootstrap import Bootstrapper
from app.ingest.candidacies import ALL_STATES, INGESTED_OFFICES, CandidacySourceError
from app.ingest.house_rollcalls import HouseRollCallSourceError
from app.ingest.senate_rollcalls import SenateRollCallSourceError
from app.repositories import Repository
from app.repositories.models import MemberVote, Person, RollCall
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


def test_default_jobs_registers_exactly_the_documented_jobs() -> None:
    names = [job.name for job in jobs_module.default_jobs()]
    assert names == [
        "sweep_correction_emails",
        "load_candidacies",
        "load_house_rollcalls",
        # Story 8.1. Hourly, like load_house_rollcalls, but with NO
        # precondition: the senate.gov LIS feed is public and keyless
        # (confirmed live during the story), so there is no credential to
        # block on.
        "load_senate_rollcalls",
        "run_nightly_integrity_checks",
        "repair_member_votes_denormalization",
        # Story 12.5. Daily, beside the integrity checks it mirrors in shape:
        # fails the job (not a request) if any live issue_topics/stances row
        # lacks an approved-or-grandfathered panel decision at its current
        # version.
        "run_panel_gate_check",
        # Story 13.5. Registered from day one rather than left to a human with
        # a script, which is the gap story 12.4's own module docstring
        # documented at length before this registry existed.
        "sweep_qa_session_memory",
        # Story 6.2. The consumer of datamodel.md's `rollcall.ingested` event.
        # Hourly, matching `load_house_rollcalls` — the ingest is its only
        # producer. A no-op until something emits that event; see
        # `run_recompute_candidate_positions`'s docstring for that gap.
        "recompute_candidate_positions",
    ]


def test_the_panel_gate_check_runs_daily_with_no_precondition() -> None:
    """No precondition, unlike `load_candidacies`/`load_house_rollcalls`: the
    panel gate needs only the ZeroDB credentials every job already has via
    `ZeroDBClient.from_env()`, not a separate third-party API key.
    """
    job = next(j for j in jobs_module.default_jobs() if j.name == "run_panel_gate_check")
    assert job.precondition is None
    assert job.interval == timedelta(days=1)


def test_the_qa_memory_sweep_runs_every_six_hours_with_no_precondition() -> None:
    """Every 6 hours, not daily: a daily sweep of a 1-day window lets a turn
    survive up to 48 hours, which would make datamodel.md's "Q&A memory | 1
    day" false of the database for an extra day. See
    `app.agent_memory.retention`'s module docstring for the full argument, and
    `SessionMemoryService.recall`'s read guard for why the overshoot is never
    voter-visible.
    """
    job = next(j for j in jobs_module.default_jobs() if j.name == "sweep_qa_session_memory")
    assert job.precondition is None
    assert job.interval == timedelta(hours=6)


def test_integrity_jobs_have_no_precondition() -> None:
    """Unlike the FEC/Congress.gov jobs, neither integrity job needs an
    external API key — every check and the repair read/write tables that
    already exist in any bootstrapped project, even when some are empty.
    """
    by_name = {job.name: job for job in jobs_module.default_jobs()}
    assert by_name["run_nightly_integrity_checks"].precondition is None
    assert by_name["run_nightly_integrity_checks"].interval == timedelta(days=1)
    assert by_name["repair_member_votes_denormalization"].precondition is None
    assert by_name["repair_member_votes_denormalization"].interval == timedelta(days=1)


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


# --- load_senate_rollcalls: no precondition (story 8.1) --------------------


def test_load_senate_rollcalls_has_no_precondition() -> None:
    """Unlike `load_house_rollcalls`, the senate.gov LIS feed needs no API
    key (confirmed live during story 8.1) -- there is no credential to check
    for, so this job has none, the same "always actually runs" posture the
    integrity jobs and the memory sweep already have.
    """
    job = next(j for j in jobs_module.default_jobs() if j.name == "load_senate_rollcalls")
    assert job.interval == timedelta(hours=1)
    assert job.precondition is None


# --- run_load_senate_rollcalls: source-error mapping and the happy path ----


def test_run_load_senate_rollcalls_maps_a_source_error_to_zerodb_error(
    monkeypatch: pytest.MonkeyPatch, client: ZeroDBClient
) -> None:
    """Mirrors `test_run_load_house_rollcalls_maps_a_source_error_to_zerodb_error`:
    only one source here, so any source failure must fail the whole job.
    """

    def fake_fetch_vote_numbers(*, congress: int, session: int):
        raise SenateRollCallSourceError("simulated senate.gov outage")

    monkeypatch.setattr(jobs_module, "fetch_vote_numbers", fake_fetch_vote_numbers)

    with pytest.raises(ZeroDBError, match="could not load the Senate roll-call source"):
        jobs_module.run_load_senate_rollcalls(client)


def test_run_load_senate_rollcalls_loads_roll_calls_and_member_votes(
    monkeypatch: pytest.MonkeyPatch, client: ZeroDBClient
) -> None:
    """Exercises the real wiring -- vote-number listing, item fetch, the
    bioguide crosswalk fetch, and both `roll_calls` and `member_votes`
    writes -- against mocked fetch functions, mirroring
    `test_run_load_house_rollcalls_loads_the_most_recent_sample`'s shape but
    additionally proving this story's wider `member_votes` scope works end
    to end through the job wrapper, not just through the loader directly
    (already covered by tests/features/senate_rollcalls.feature).
    """
    monkeypatch.setattr(jobs_module.time, "sleep", lambda _seconds: None)

    # Seed the one senator this fixture's single vote names, so the
    # member_votes write's `person_id -> people` reference resolves (the real
    # people rows come from story 2.2's already-live legislators ingest; this
    # test only needs to prove the job's own wiring, not re-prove that
    # ingest).
    Repository(client=client, model=Person).upsert(
        {"id": "person:B001261", "canonical_name": "Senator Test"}
    )

    numbers = [254]
    fetched: list[int] = []
    vote_xml = """<?xml version="1.0" encoding="UTF-8"?><roll_call_vote>
  <congress>119</congress>
  <session>2</session>
  <vote_number>254</vote_number>
  <vote_date>September 30, 2026,  12:51 PM</vote_date>
  <question>On Cloture on the Motion to Proceed</question>
  <vote_result>Cloture on the Motion to Proceed Rejected</vote_result>
  <document><document_type>H.R.</document_type></document>
  <amendment><amendment_number/></amendment>
  <count><yeas>1</yeas><nays>0</nays><present/><absent/></count>
  <members>
    <member>
      <last_name>Barrasso</last_name>
      <vote_cast>Yea</vote_cast>
      <lis_member_id>S317</lis_member_id>
    </member>
  </members>
</roll_call_vote>"""

    def fake_fetch_vote_numbers(*, congress: int, session: int):
        assert congress == jobs_module.SENATE_ROLLCALLS_CONGRESS
        assert session == jobs_module.SENATE_ROLLCALLS_SESSION
        return numbers

    def fake_fetch_roll_call_xml(*, congress: int, session: int, vote_number: int):
        fetched.append(vote_number)
        return vote_xml

    def fake_fetch_lis_to_bioguide_map():
        return {"S317": "B001261"}

    monkeypatch.setattr(jobs_module, "fetch_vote_numbers", fake_fetch_vote_numbers)
    monkeypatch.setattr(jobs_module, "fetch_roll_call_xml", fake_fetch_roll_call_xml)
    monkeypatch.setattr(jobs_module, "fetch_lis_to_bioguide_map", fake_fetch_lis_to_bioguide_map)

    jobs_module.run_load_senate_rollcalls(client)

    assert fetched == [254]

    roll_calls: Repository = Repository(client=client, model=RollCall)
    stored = roll_calls.get("us-senate:119-2:254")
    assert stored is not None
    assert stored.vote_type == "cloture"

    member_votes: Repository = Repository(client=client, model=MemberVote)
    member_row = member_votes.get("us-senate:119-2:254:B001261")
    assert member_row is not None
    assert member_row.position == "yea"
    assert member_row.person_id == "person:B001261"


# --- run_nightly_integrity_checks: violation becomes a job failure (16.2) --


def test_run_nightly_integrity_checks_is_clean_against_an_empty_bootstrapped_project(
    client: ZeroDBClient,
) -> None:
    """No violations possible over empty tables — this is check 2/3's
    documented vacuous-truth case, exercised here at the job level too.
    """
    jobs_module.run_nightly_integrity_checks(client)


def test_run_nightly_integrity_checks_raises_when_a_check_finds_a_violation(
    client: ZeroDBClient,
) -> None:
    """A dangling member_votes.roll_call_id (check 1), written directly to
    bypass the repository's own reference guard, must surface as a raised
    error here — that is what makes `ScheduledJobRunner` record this job as
    FAILED rather than RAN. The detailed per-check sabotage proofs live in
    `tests/features/integrity_checks.feature`; this test only pins the
    found-a-violation -> raises contract at the job boundary.
    """
    client.insert_row(
        "member_votes",
        {
            "id": "us-house:119-2:999:B000944",
            "roll_call_id": "us-house:119-2:999",
            "person_id": "person:B000944",
            "position": "yea",
            "voted_at": "2026-01-01T00:00:00Z",
            "vote_type": "final_passage",
            "bill_id": None,
            "topic_ids": [],
            "is_bundled": False,
        },
    )
    with pytest.raises(ZeroDBError, match="violation"):
        jobs_module.run_nightly_integrity_checks(client)


# --- run_panel_gate_check: unapproved wording becomes a job failure (12.5) -


def test_run_panel_gate_check_passes_once_the_corpus_is_grandfathered(
    client: ZeroDBClient,
) -> None:
    """The real, current shape of the live corpus: fully seeded and fully
    grandfathered, nothing yet panel-approved. Mirrors
    `scripts/check_panel_gate.py`'s own baseline fixture.
    """
    from app.panel_review.service import PanelReviewService
    from app.seed_stances import StanceSeeder
    from app.seed_taxonomy import TaxonomySeeder

    TaxonomySeeder(client, pace=lambda: None).run()
    StanceSeeder(client, pace=lambda: None).run()
    PanelReviewService(client).grandfather_existing_corpus()

    jobs_module.run_panel_gate_check(client)  # must not raise


def test_run_panel_gate_check_raises_on_a_stance_with_no_panel_review_at_all(
    client: ZeroDBClient,
) -> None:
    """A stance written directly to storage, bypassing `PanelReviewService`
    entirely, with no ledger entry of any kind — not even grandfathered —
    must surface as a raised error here, exactly as
    `run_nightly_integrity_checks` raises on a found violation. This is the
    job-boundary half of story 12.5's sabotage proof; the detailed gate
    scenarios live in `tests/features/panel_gate_in_release.feature`.
    """
    from app.panel_review.service import PanelReviewService
    from app.repositories import Repository
    from app.repositories.models import Stance
    from app.seed_stances import WORDING_VERSION, StanceSeeder
    from app.seed_taxonomy import TaxonomySeeder

    TaxonomySeeder(client, pace=lambda: None).run()
    StanceSeeder(client, pace=lambda: None).run()
    PanelReviewService(client).grandfather_existing_corpus()

    stances: Repository[Stance] = Repository(client=client, model=Stance)
    stances.upsert(
        {
            "id": "en-US:prescription-drug-pricing:v1:job-boundary-sabotage",
            "topic_id": "en-US:prescription-drug-pricing:v1",
            "label": "Job-boundary sabotage card: never reviewed by any panel",
            "description": "Written directly to storage to prove run_panel_gate_check "
            "raises when the gate finds an unreviewed row.",
            "direction_code": "0",
            "wording_version": WORDING_VERSION,
        }
    )

    with pytest.raises(ZeroDBError, match="unapproved"):
        jobs_module.run_panel_gate_check(client)


# --- repair_member_votes_denormalization: reports, idempotent (16.4) ------


def test_run_repair_member_votes_drift_is_a_noop_against_an_empty_project(
    client: ZeroDBClient,
) -> None:
    """Does not raise, and (vacuously) repairs nothing — mirrors
    `test_run_sweep_correction_emails_runs_clean_against_an_empty_project`'s
    own posture toward an empty table.
    """
    jobs_module.run_repair_member_votes_drift(client)
