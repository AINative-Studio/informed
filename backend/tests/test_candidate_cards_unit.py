"""Unit tests for the candidate_cards read model's seams (story 7.2).

`features/candidate_cards.feature` proves the acceptance criteria; these pin
the behaviour at each seam the rebuild walks through, so a regression names the
broken piece rather than just "the card is wrong". Three groups:

  1. **The two pure computations** (`verdict_counts_for`, `say_do_gap_for`,
     `positions_summary_for`) driven directly, where a table of inputs is
     clearer than a Gherkin scenario per case — in particular every
     `VerdictValue` individually, which is how the three excluded-from-the-gap
     values get pinned one at a time rather than only in combination.
  2. **The failure and overflow paths** the feature file deliberately does not
     describe, because they are operator-facing rather than voter-facing: a
     candidacy naming a person who does not exist, a page that came back full,
     an event with no `person_id`, an unknown topic. Every one of these is a
     path where the wrong behaviour would be *silence* — a card quietly
     missing from a ballot — so each is tested for the loud behaviour it is
     supposed to have instead.
  3. **The read path's structural guarantee**: `CandidateCardReader` has no
     method that can reach a source table. Checked against the class rather
     than by review, so a later convenience method fails a test.

All synthetic, and nothing voter-derived exists anywhere in this story's
inputs: a card is a read model over public candidate records.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.ballot.cards import (
    GAP_SCORED_VERDICTS,
    PAGE_SIZE,
    PARTIAL_VERDICT_GAP_WEIGHT,
    RACE_PAGE_SIZE,
    REBUILD_TOPICS,
    SAY_DO_GAP_BASIS,
    CandidateCardBuilder,
    CandidateCardReader,
    CandidateCardRebuildConsumer,
    RebuildResult,
    positions_summary_for,
    say_do_gap_for,
    verdict_counts_for,
)
from app.bootstrap import Bootstrapper
from app.repositories import Repository
from app.repositories.models import (
    Candidacy,
    CandidateCard,
    CandidatePosition,
    District,
    Election,
    Person,
    Verdict,
    VerdictCounts,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

PERSON_ID = "person:TEST7200100"
ELECTION_ID = "us-test72u-11-03-general"
DISTRICT_ID = "ocd-division/country:us/state:zz/cd:test72u"
CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"


@pytest.fixture
def zerodb() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    return client


def _seed_candidacy(zerodb: ZeroDBClient, *, with_person: bool = True) -> str:
    Repository(client=zerodb, model=Election).upsert(
        {
            "id": ELECTION_ID,
            "jurisdiction_id": "us-federal",
            "name": "Test 72 unit general",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )
    Repository(client=zerodb, model=District).upsert(
        {
            "id": DISTRICT_ID,
            "body_id": "us-house",
            "name": "Test Unit District 72",
            "state": "ZZ",
            "valid_from": "2025-01-03",
        }
    )
    if with_person:
        Repository(client=zerodb, model=Person).upsert(
            {"id": PERSON_ID, "canonical_name": "TEST72U, Unit", "terms": []}
        )
    stored = Repository(client=zerodb, model=Candidacy).upsert(
        {
            "election_id": ELECTION_ID,
            "district_id": DISTRICT_ID,
            "person_id": PERSON_ID,
            "person_name": "TEST72U, UNIT",
            "party": "IND",
            "status": "filed",
        },
        # The person row is deliberately absent in the `with_person=False`
        # case, which is precisely what the reference check would refuse — so
        # it is skipped here to construct the broken-upstream state the
        # builder has to survive. Never the default; see
        # `Repository.upsert`'s own note on this flag.
        check_references=with_person,
    )
    return stored.id or ""


# =========================================================================
# 1. The pure computations
# =========================================================================


def test_verdict_counts_tally_each_value_separately() -> None:
    verdicts = [
        _verdict_model("kept"),
        _verdict_model("kept"),
        _verdict_model("partial"),
        _verdict_model("broken"),
        _verdict_model("no_opportunity"),
        _verdict_model("absent"),
        _verdict_model("not_testable"),
    ]
    counts = verdict_counts_for(verdicts)
    assert counts.kept == 2
    assert counts.partial == 1
    assert counts.broken == 1
    assert counts.no_opportunity == 1
    assert counts.absent == 1
    assert counts.not_testable == 1
    assert counts.total == 7


def test_verdict_counts_of_nothing_is_all_zero() -> None:
    """An empty tally is all-zero, and the CALLER decides whether to store it.

    `verdict_counts_for([])` returning zeros is correct — it was asked to
    count nothing — and `CandidateCardBuilder.rebuild_candidacy` is what
    turns "no verdicts at all" into a stored `None` rather than a row claiming
    `kept: 0`. The split matters: the computation is honest about what it
    counted, and the storage decision is where prd.md's evidence rule lands.
    """
    counts = verdict_counts_for([])
    assert counts.total == 0
    assert counts.model_dump() == VerdictCounts().model_dump()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        # The three scored values alone, at each end and the middle.
        (["kept"], 0.0),
        (["broken"], 1.0),
        (["partial"], PARTIAL_VERDICT_GAP_WEIGHT),
        (["kept", "broken"], 0.5),
        (["kept", "kept", "kept", "broken"], 0.25),
        (["broken", "broken", "broken", "kept"], 0.75),
        # Each excluded value, alone: no denominator, so no gap to report.
        # `None` and not 0.0 — reporting 0.0 would commend a candidate for a
        # record nobody could check. See `say_do_gap_for`.
        (["no_opportunity"], None),
        (["absent"], None),
        (["not_testable"], None),
        (["no_opportunity", "absent", "not_testable"], None),
        # Each excluded value does not dilute a scored one: one kept promise
        # plus three unscorable ones is still a gap of 0.0 over a denominator
        # of ONE, not 0.25 over four.
        (["kept", "no_opportunity"], 0.0),
        (["kept", "absent"], 0.0),
        (["kept", "not_testable"], 0.0),
        (["broken", "no_opportunity", "absent", "not_testable"], 1.0),
    ],
)
def test_say_do_gap_over_every_verdict_value(values: list[str], expected: float | None) -> None:
    counts = verdict_counts_for([_verdict_model(value) for value in values])
    gap = say_do_gap_for(counts)
    if expected is None:
        assert gap is None
    else:
        assert gap == pytest.approx(expected)


def test_say_do_gap_is_symmetric_under_mirroring() -> None:
    """prd.md's "nonpartisan by construction", as arithmetic.

    Every kept/broken split and its mirror image must sum to 1.0. Checked
    across the whole range rather than at one point, because a weighting that
    was symmetric only at the extremes would still advantage one direction in
    between — which is the failure a read model could smuggle in unnoticed,
    since it is the row the ballot view renders.
    """
    for kept in range(0, 8):
        broken = 7 - kept
        forward = say_do_gap_for(
            verdict_counts_for(
                [_verdict_model("kept")] * kept + [_verdict_model("broken")] * broken
            )
        )
        mirrored = say_do_gap_for(
            verdict_counts_for(
                [_verdict_model("kept")] * broken + [_verdict_model("broken")] * kept
            )
        )
        assert forward is not None and mirrored is not None
        assert forward + mirrored == pytest.approx(1.0)


def test_a_record_of_only_partials_lands_exactly_at_the_midpoint() -> None:
    """The reason `PARTIAL_VERDICT_GAP_WEIGHT` is 0.5 and not 0.3 or 0.7.

    Any other weight makes an all-`partial` record asymmetric with itself —
    its own mirror image is the same record, so its gap must be the midpoint
    or the scale is not symmetric. See that constant's own note.
    """
    counts = verdict_counts_for([_verdict_model("partial")] * 5)
    assert say_do_gap_for(counts) == pytest.approx(0.5)


def test_only_kept_partial_and_broken_are_scored() -> None:
    """The exclusion list, pinned as a fact rather than left to the formula."""
    assert set(GAP_SCORED_VERDICTS) == {"kept", "partial", "broken"}
    assert GAP_SCORED_VERDICTS["kept"] == 0.0
    assert GAP_SCORED_VERDICTS["broken"] == 1.0


def test_positions_summary_is_sorted_by_stance_so_rebuilds_are_identical() -> None:
    """Story 6.4's determinism promise, applied to the read model's array.

    Live ZeroDB's row order is unspecified (story 1.4 measured it), so without
    a sort an otherwise-unchanged card would look changed on every rebuild.
    """
    positions = [
        _position_model("en-US:z-topic:v1:for", 0.5),
        _position_model("en-US:a-topic:v1:for", -0.5),
        _position_model("en-US:m-topic:v1:for", 0.0),
    ]
    forward = positions_summary_for(positions)
    reversed_order = positions_summary_for(list(reversed(positions)))
    assert [entry.stance_id for entry in forward] == [
        "en-US:a-topic:v1:for",
        "en-US:m-topic:v1:for",
        "en-US:z-topic:v1:for",
    ]
    assert forward == reversed_order


def test_positions_summary_carries_the_evidence_count_the_badge_needs() -> None:
    """Story 6.5's confidence badge reads `evidence_count`.

    Dropping it would make the ballot view re-read `candidate_positions` to
    render a badge — the request-time aggregation this table exists to remove.
    """
    summary = positions_summary_for([_position_model("en-US:t:v1:for", 0.5)])
    assert summary[0].evidence_count == 4
    assert summary[0].confidence == pytest.approx(0.8)


# =========================================================================
# 2. The failure and overflow paths
# =========================================================================


def test_a_rebuild_for_an_unknown_candidacy_is_skipped_not_raised(zerodb: ZeroDBClient) -> None:
    builder = CandidateCardBuilder(zerodb)
    result = builder.rebuild_candidacy("us-nope:ocd-division/country:us/state:zz:person:NOBODY")
    assert result.cards_rebuilt == 0
    assert len(result.skipped) == 1


def test_a_candidacy_whose_person_row_is_missing_is_skipped_loudly(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture
) -> None:
    """datamodel.md's own integrity check says this cannot happen.

    "Every `candidacies.person_id` resolves to a `people` row", and
    `Candidacy.references` enforces it at write time. Handled anyway, because
    a card is a DERIVED row: if the invariant is broken upstream, a logged
    skip plus the nightly audit beats a 500 on a voter's ballot view. The
    assertion is on the log as much as the result — a silent skip would be a
    card quietly missing from a ballot.
    """
    candidacy_id = _seed_candidacy(zerodb, with_person=False)
    builder = CandidateCardBuilder(zerodb)
    with caplog.at_level(logging.ERROR, logger="app.ballot.cards"):
        result = builder.rebuild_candidacy(candidacy_id)
    assert result.cards_rebuilt == 0
    assert result.skipped == (candidacy_id,)
    assert "has no row" in caplog.text
    assert PERSON_ID in caplog.text


def test_a_person_rebuild_skips_a_broken_candidacy_without_abandoning_the_poll(
    zerodb: ZeroDBClient,
) -> None:
    """One corrupt candidacy must not cost the rest of the rebuild.

    The event-driven path fans out over every candidacy a person holds, so a
    raise on the first would leave later cards unrebuilt with nothing saying
    so.
    """
    _seed_candidacy(zerodb, with_person=False)
    builder = CandidateCardBuilder(zerodb)
    result = builder.rebuild_from_verdicts(PERSON_ID)
    assert result.cards_rebuilt == 0
    assert len(result.skipped) == 1
    assert "1 skipped" in result.summary()
    assert SAY_DO_GAP_BASIS in result.summary()


def test_a_recompute_for_a_person_with_no_candidacy_is_not_an_error(zerodb: ZeroDBClient) -> None:
    """Most of `people` has no candidacy row, so this is the common case.

    Story 2.6 ingests candidacies for a fraction of the 542 live `people`
    rows, so the hourly position recompute legitimately rebuilds nothing for
    most members. It must not look like a failure.
    """
    Repository(client=zerodb, model=Person).upsert(
        {"id": "person:TEST7200199", "canonical_name": "TEST72U, Nobody", "terms": []}
    )
    builder = CandidateCardBuilder(zerodb)
    result = builder.rebuild_from_positions("person:TEST7200199")
    assert result == RebuildResult()
    assert result.cards_rebuilt == 0
    assert result.skipped == ()


def test_an_event_with_no_person_id_is_ignored_loudly(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture
) -> None:
    consumer = CandidateCardRebuildConsumer(zerodb)
    with caplog.at_level(logging.WARNING, logger="app.ballot.cards"):
        result = consumer.handle_event("positions.recomputed", {"methodology_version": "2026.1"})
    assert result.cards_rebuilt == 0
    assert "no person_id" in caplog.text


def test_an_unknown_topic_is_ignored_rather_than_raised(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture
) -> None:
    """`handle_event` is the webhook entry point as well as the poll's.

    An endpoint that 500s on an event it does not recognise is a worse failure
    than one that ignores it and says so — the unknown event would be retried
    forever by the producer.
    """
    consumer = CandidateCardRebuildConsumer(zerodb)
    with caplog.at_level(logging.WARNING, logger="app.ballot.cards"):
        result = consumer.handle_event("something.else", {"person_id": PERSON_ID})
    assert result.cards_rebuilt == 0
    assert "no candidate_cards rebuild is defined" in caplog.text


def test_the_consumer_reads_both_documented_topics(zerodb: ZeroDBClient) -> None:
    """Both of datamodel.md's producers, not one.

    Pinned as a fact about the constant, because a consumer that silently read
    only `positions.recomputed` would pass every other test in this suite
    (nothing emits the other topic yet) and fail the day epic 11 ships.
    """
    assert REBUILD_TOPICS == ("verdict.published", "positions.recomputed")


def test_a_full_page_of_candidacies_is_logged_as_a_possible_truncation(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page is still a page, and a truncated fan-out is a missing card.

    Driven by shrinking the page size rather than by writing 500 candidacies,
    which would make this test a minute long for the same assertion.
    """
    candidacy_id = _seed_candidacy(zerodb)
    monkeypatch.setattr("app.ballot.cards.PAGE_SIZE", 1)
    builder = CandidateCardBuilder(zerodb)
    with caplog.at_level(logging.WARNING, logger="app.ballot.cards"):
        result = builder.rebuild_from_verdicts(PERSON_ID)
    assert result.candidacy_ids == (candidacy_id,)
    assert "full page" in caplog.text


def test_a_full_page_of_positions_is_logged_as_a_possible_truncation(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_candidacy(zerodb)
    _append_position(zerodb, "en-US:test72u:v1:for")
    monkeypatch.setattr("app.ballot.cards.PAGE_SIZE", 1)
    builder = CandidateCardBuilder(zerodb)
    with caplog.at_level(logging.WARNING, logger="app.ballot.cards"):
        builder.rebuild_from_positions(PERSON_ID)
    assert "positions_summary may be incomplete" in caplog.text


def test_a_full_page_of_verdicts_is_logged_as_a_possible_truncation(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An under-counted `verdict_counts` is a wrong number on a voter's screen."""
    _seed_candidacy(zerodb)
    _append_verdict(zerodb, "prm:test72u-0001", "kept")
    monkeypatch.setattr("app.ballot.cards.PAGE_SIZE", 1)
    builder = CandidateCardBuilder(zerodb)
    with caplog.at_level(logging.WARNING, logger="app.ballot.cards"):
        builder.rebuild_from_verdicts(PERSON_ID)
    assert "verdict_counts may be incomplete" in caplog.text


def test_more_waiting_events_than_one_run_reads_are_logged_not_looped(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer that paged forever on a busy window would hold the runner.

    The same posture `app.scoring.positions.PositionRecomputeConsumer` takes:
    the window is bounded by `limit` on purpose, and the next scheduled run
    picks up where this one stopped — a loop here would mean one busy hour of
    roll-call ingest monopolised the job runner.

    `has_more` is stubbed rather than provoked, because the in-memory fake's
    event route returns every matching event and never sets the flag (see
    `FakeZeroDBTransport._route_events`). That is a real gap in the fake, not
    in the client: the live endpoint does page. Stubbing the one field under
    test is honest about which half is being exercised — the consumer's
    reaction to the flag, not the transport's setting of it.
    """
    candidacy_id = _seed_candidacy(zerodb)
    _append_verdict(zerodb, "prm:test72u-0001", "kept")
    zerodb.create_event(topic="positions.recomputed", payload={"person_id": PERSON_ID})

    real_list_events = zerodb.list_events

    def busy_page(**kwargs: Any) -> Any:
        page = real_list_events(**kwargs)
        return page.model_copy(update={"has_more": True})

    monkeypatch.setattr(zerodb, "list_events", busy_page)
    consumer = CandidateCardRebuildConsumer(zerodb)
    with caplog.at_level(logging.WARNING, logger="app.ballot.cards"):
        result = consumer.run(limit=1)
    assert result.candidacy_ids == (candidacy_id,)
    assert "the next run will pick them up" in caplog.text


def test_a_candidacy_with_no_id_is_skipped_rather_than_written(zerodb: ZeroDBClient) -> None:
    """A guard against a state the repository makes unreachable.

    `Repository.upsert` fills `id` from the natural key before every write, so
    no stored candidacy can lack one — this is reached only by handing the
    builder an unstored model, which is what a future caller doing its own
    read could do. Tested because the alternative (an `AttributeError` or a
    card written under the id `""`) would be a silent corruption of the read
    model rather than a skip.
    """
    builder = CandidateCardBuilder(zerodb)
    unsaved = Candidacy(
        election_id=ELECTION_ID,
        district_id=DISTRICT_ID,
        person_id=PERSON_ID,
        person_name="TEST72U, UNIT",
        status="filed",
    )
    assert unsaved.id is None
    assert builder._write_card(unsaved) is None  # noqa: SLF001 - the guard has no public seam
    assert Repository(client=zerodb, model=CandidateCard).query(limit=10) == []


def test_a_full_page_of_cards_for_one_race_is_logged_as_an_error(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error, not a warning: this one is a candidate missing from a ballot."""
    candidacy_id = _seed_candidacy(zerodb)
    CandidateCardBuilder(zerodb).rebuild_candidacy(candidacy_id)
    monkeypatch.setattr("app.ballot.cards.RACE_PAGE_SIZE", 1)
    with caplog.at_level(logging.ERROR, logger="app.ballot.cards"):
        cards = CandidateCardReader(zerodb).cards_for_race(
            election_id=ELECTION_ID, district_id=DISTRICT_ID
        )
    assert len(cards) == 1
    assert "missing from a ballot" in caplog.text


def test_positions_spanning_two_methodologies_name_the_highest_and_log_it(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A partial recompute can leave current positions under two versions.

    `candidate_positions` is current per (person, stance), so a recompute that
    touched a subset of stances leaves the rest on the previous methodology.
    The card names the highest in play — the newest weights a reader could go
    and look up — and says so, rather than picking whichever row came back
    first from a store whose order is unspecified.
    """
    candidacy_id = _seed_candidacy(zerodb)
    _append_position(zerodb, "en-US:test72u-a:v1:for", methodology_version="2026.1")
    _append_position(zerodb, "en-US:test72u-b:v1:for", methodology_version="2026.2")
    builder = CandidateCardBuilder(zerodb)
    with caplog.at_level(logging.WARNING, logger="app.ballot.cards"):
        # No `methodology_version` passed, so the fallback that reads it from
        # the rows is what runs — the manual/backfill path.
        builder.rebuild_from_positions(PERSON_ID)
    card = Repository(client=zerodb, model=CandidateCard).get(candidacy_id)
    assert card is not None
    assert card.methodology_version == "2026.2"
    assert "span 2 methodology versions" in caplog.text


def test_a_manual_rebuild_reads_the_methodology_from_the_positions(zerodb: ZeroDBClient) -> None:
    """No event, so no payload to take the version from — the rows supply it."""
    candidacy_id = _seed_candidacy(zerodb)
    _append_position(zerodb, "en-US:test72u:v1:for", methodology_version="2026.7")
    CandidateCardBuilder(zerodb).rebuild_candidacy(candidacy_id)
    card = Repository(client=zerodb, model=CandidateCard).get(candidacy_id)
    assert card is not None
    assert card.methodology_version == "2026.7"


def test_a_manual_rebuild_of_a_candidate_with_no_verdicts_stores_none_not_zero(
    zerodb: ZeroDBClient,
) -> None:
    """The one place `rebuild_candidacy` could have fabricated, and does not.

    It reads BOTH source tables, so it could legitimately write a measured
    all-zero `VerdictCounts`. It writes `None` instead, for the reason
    `app.summary_card.models` gives in the same words: `kept: 0` on a
    candidate nobody has published a verdict about reads as "kept none of
    their promises".
    """
    candidacy_id = _seed_candidacy(zerodb)
    CandidateCardBuilder(zerodb).rebuild_candidacy(candidacy_id)
    card = Repository(client=zerodb, model=CandidateCard).get(candidacy_id)
    assert card is not None
    assert card.verdict_counts is None
    assert card.say_do_gap is None
    assert card.positions_summary is None


def test_a_card_falls_back_to_the_raw_district_id_when_the_district_row_is_gone(
    zerodb: ZeroDBClient,
) -> None:
    """A legible id beats a 500 for a display string.

    The same fallback `SummaryCardService._district_name` makes. Constructed
    by deleting the district row after the candidacy was written, which is the
    only way to reach it — `Candidacy.references` refuses the write otherwise.
    """
    candidacy_id = _seed_candidacy(zerodb)
    Repository(client=zerodb, model=District).delete(DISTRICT_ID)
    CandidateCardBuilder(zerodb).rebuild_candidacy(candidacy_id)
    card = Repository(client=zerodb, model=CandidateCard).get(candidacy_id)
    assert card is not None
    assert card.district == DISTRICT_ID


def test_the_timestamp_defaults_to_a_real_utc_clock(zerodb: ZeroDBClient) -> None:
    """The injected clock is a test seam, not the production default."""
    candidacy_id = _seed_candidacy(zerodb)
    CandidateCardBuilder(zerodb).rebuild_candidacy(candidacy_id)
    card = Repository(client=zerodb, model=CandidateCard).get(candidacy_id)
    assert card is not None
    assert card.updated_at.endswith("Z")
    assert card.updated_at.startswith("20")


# =========================================================================
# 3. The read path's structural guarantee
# =========================================================================


def test_the_reader_has_no_method_that_can_reach_a_source_table() -> None:
    """ "Never aggregates at request time", enforced structurally.

    `CandidateCardReader` holds exactly one repository — `candidate_cards` —
    so there is no attribute through which a convenience method could reach
    `candidate_positions` or `verdicts`. Checked by inspecting the instance's
    own attributes, so adding a second repository fails this test rather than
    waiting for a review to notice it on a hot path.
    """
    transport = FakeZeroDBTransport()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    reader = CandidateCardReader(client)
    repositories = [value for value in vars(reader).values() if isinstance(value, Repository)]
    assert len(repositories) == 1
    assert repositories[0].table_name == "candidate_cards"
    public_methods = {name for name in dir(reader) if not name.startswith("_")}
    assert public_methods == {"card_for", "cards_for_race"}


def test_a_card_that_was_never_built_reads_as_none_not_an_error(zerodb: ZeroDBClient) -> None:
    """An ordinary state, not a failure.

    Story 7.1's ballot view renders a candidacy with no card as a candidate
    with no computed data — which is true — rather than as an error.
    """
    reader = CandidateCardReader(zerodb)
    assert reader.card_for(CANDIDACY_ID) is None


def test_cards_for_a_race_with_no_cards_is_an_empty_list(zerodb: ZeroDBClient) -> None:
    reader = CandidateCardReader(zerodb)
    assert reader.cards_for_race(election_id=ELECTION_ID, district_id=DISTRICT_ID) == []


def test_the_page_sizes_are_pages_and_not_unbounded() -> None:
    """Named constants rather than an implicit default, and bounded.

    `member_votes` reaches ~600k rows per Congress and every full-table read
    in this codebase is a tracked open question; a read model is no exception
    just because its own table is small today.
    """
    assert 0 < PAGE_SIZE <= 1000
    assert 0 < RACE_PAGE_SIZE <= 1000


# --- helpers --------------------------------------------------------------


def _verdict_model(value: str) -> Verdict:
    """A `Verdict` instance for the pure computations, never stored.

    Built directly rather than through the repository because these tests
    drive `verdict_counts_for`/`say_do_gap_for`, which take models and do no
    I/O — writing the whole promise-source chain for each would test the
    repository instead of the formula. The feature file's scenarios do write
    the real chain.
    """
    return Verdict(
        promise_id=f"prm:test72u-{value}",
        person_id=PERSON_ID,
        verdict=value,  # type: ignore[arg-type] - parametrised over VerdictValue
        confidence=0.8,
        methodology_version="2026.1",
        is_current=True,
    )


def _position_model(stance_id: str, s_value: float) -> CandidatePosition:
    return CandidatePosition(
        person_id=PERSON_ID,
        stance_id=stance_id,
        s_value=s_value,
        confidence=0.8,
        evidence_count=4,
        basis="votes",
        methodology_version="2026.1",
        is_current=True,
    )


def _append_position(
    zerodb: ZeroDBClient, stance_id: str, *, methodology_version: str = "2026.1"
) -> None:
    """Append a position row, bypassing the stance reference check.

    `CandidatePosition.references` names `stances`, and seeding the topic and
    stance corpus for a test about methodology-version selection would be a
    page of setup for a fact that has nothing to do with stances. The feature
    file seeds the real stance rows; these unit tests are about the builder.
    """
    Repository(client=zerodb, model=CandidatePosition).append_version(
        {
            "person_id": PERSON_ID,
            "stance_id": stance_id,
            "s_value": 0.5,
            "confidence": 0.8,
            "evidence_count": 4,
            "basis": "votes",
            "methodology_version": methodology_version,
            "computed_at": "2026-10-19T11:00:00Z",
        },
        check_references=False,
    )


def _append_verdict(zerodb: ZeroDBClient, promise_id: str, value: str) -> None:
    """Append a verdict row, bypassing the promise reference check.

    Same reasoning as `_append_position`: `Verdict.references` names
    `promises`, and building the promise-source-promise chain for a test about
    page overflow would be setup for a fact that has nothing to do with
    promises. The feature file writes the real chain.
    """
    Repository(client=zerodb, model=Verdict).append_version(
        {
            "promise_id": promise_id,
            "person_id": PERSON_ID,
            "verdict": value,
            "confidence": 0.8,
            "methodology_version": "2026.1",
            "qa_status": "approved",
            "created_at": "2026-10-19T11:00:00Z",
        },
        check_references=False,
    )
