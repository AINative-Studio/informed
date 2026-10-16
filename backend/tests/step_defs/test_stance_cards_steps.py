"""Step definitions for tests/features/stance_cards.feature (story 3.2).

Runs against story 1.2's in-memory fake transport, never live ZeroDB, so CI
needs no credentials — the rule every backend feature in this suite follows.
The live confirmation of the same behaviour lives in
`tests/test_stance_cards_live_shapes.py` and
`scripts/verify_stance_cards_live.py`.

`break_server_side_filter()` is applied for the same reason story 3.1's steps
apply it: live ZeroDB's server-side `/query` filter matches nothing (story
1.4's finding), so the seed's idempotency rests entirely on the client's own
paging fallback, and a fake with a working filter would make every "exactly 1
row" scenario pass trivially.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.repositories import MissingReferenceError, Repository
from app.repositories.models import IssueTopic, Stance
from app.seed_stances import (
    STANCES,
    WORDING_VERSION,
    StanceCard,
    StanceSeeder,
    contains_partisan_identity_word,
    stance_row_ids,
    stance_rows,
)
from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION, TaxonomySeeder, taxonomy_rows
from app.stances.service import StanceService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/stance_cards.feature")

#: datamodel.md's documented id pattern for `stances`: `{topic_id}:{slug}`,
#: where `topic_id` is itself `{locale}:{slug}:v{n}`.
ID_PATTERN = re.compile(
    r"^[a-z]{2}-[A-Z]{2}:[a-z0-9]+(?:-[a-z0-9]+)*:v\d+:[a-z0-9]+(?:-[a-z0-9]+)*$"
)

#: A seed large enough that a shuffle of 3-5 items is very unlikely to land on
#: the stored order twice in a row, but fixed so the suite never flakes. See
#: `test_the_returned_order_differs`.
SHUFFLE_SEED = 20260316


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("issue_topics", {"id": "string"})
    client.create_table("stances", {"id": "string"})
    return client


@pytest.fixture
def topics(zerodb: ZeroDBClient) -> Repository[IssueTopic]:
    return Repository(client=zerodb, model=IssueTopic)


@pytest.fixture
def stances(zerodb: ZeroDBClient) -> Repository[Stance]:
    return Repository(client=zerodb, model=Stance)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


@given("a ZeroDB client backed by an in-memory fake", target_fixture="stance_client")
def stance_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the issue taxonomy has been seeded")
def taxonomy_seeded(zerodb: ZeroDBClient, fake: FakeZeroDBTransport) -> None:
    """Story 3.1's 80 rows, which `Stance.topic_id` references.

    Seeded through the real `TaxonomySeeder` rather than a handful of fixture
    rows, because the stance seed's reference check reads every one of the 60
    subtopics it writes against — a fixture subset would make the check pass
    for the stances that happened to be covered.
    """
    TaxonomySeeder(zerodb, pace=lambda: None).run()
    fake.calls.clear()


# --- running the seed ------------------------------------------------------


@when("I seed the stance cards")
def seed_stances(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    result["report"] = StanceSeeder(zerodb, pace=lambda: None).run()


@when("I seed the stance cards again")
def seed_stances_again(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    result["report"] = StanceSeeder(zerodb, pace=lambda: None).run()


@given("the stance cards have been seeded")
def stances_already_seeded(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    result["first_report"] = StanceSeeder(zerodb, pace=lambda: None).run()
    fake.calls.clear()


@given(parsers.parse('the stance "{row_id}" has been hand-edited to label "{label}"'))
def hand_edit_stance(zerodb: ZeroDBClient, row_id: str, label: str) -> None:
    """Simulate someone changing a seeded row out from under the seed.

    The seed is the single source of truth for stance wording until story 3.3's
    panel workflow exists, so a re-run must restore the row rather than leave
    the edit in place.
    """
    zerodb.update_row("stances", row_id, {"label": label})


@when("I seed a stance whose topic is missing")
def seed_orphan_stance(stances: Repository[Stance], result: dict[str, Any]) -> None:
    try:
        stances.upsert(
            {
                "id": "en-US:zzz-no-such-subtopic:v1:some-stance",
                "topic_id": "en-US:zzz-no-such-subtopic:v1",
                "label": "An orphan stance",
                "description": "Points at a subtopic that was never seeded.",
                "direction_code": "+1",
                "wording_version": WORDING_VERSION,
            }
        )
    except MissingReferenceError as exc:
        result["refusal"] = exc
    else:
        result["refusal"] = None


@when("I try to store a stance flagged as the default")
def store_preselected_stance(stances: Repository[Stance], result: dict[str, Any]) -> None:
    """The "no stance is preselected" criterion, as a write the model refuses.

    `extra="forbid"` is what makes this a refusal rather than a silently
    stored-and-ignored field, and a silently stored one is the dangerous case:
    a `default: true` that no reader looks at today is a flag a later UI story
    could find and honour.
    """
    try:
        stances.upsert(
            {
                "id": "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs",
                "topic_id": "en-US:prescription-drug-pricing:v1",
                "label": "Cap what patients pay at the pharmacy counter",
                "description": "x",
                "direction_code": "+1",
                "wording_version": WORDING_VERSION,
                "default": True,
            }
        )
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        result["refusal"] = exc
    else:
        result["refusal"] = None


# --- reading stances back through the service ------------------------------


@when(parsers.parse('I read the stances for "{topic_id}" twice'))
def read_stances_twice(zerodb: ZeroDBClient, topic_id: str, result: dict[str, Any]) -> None:
    """Two reads through the service, with a seeded shuffle so CI cannot flake.

    The service shuffles per call (see `app.stances.service`), so two calls are
    what the ordering scenarios need. The seed is injected rather than left to
    `random` because a 3-option subtopic reproduces its stored order one time
    in six by chance, which is a flaky test rather than a neutrality failure.
    """
    import random

    service = StanceService(zerodb, rng=random.Random(SHUFFLE_SEED))
    result["topic_id"] = topic_id
    result["first_read"] = service.for_topic(topic_id)
    result["second_read"] = service.for_topic(topic_id)


# --- reading the stances back from storage ---------------------------------


def _stored_stances(stances: Repository[Stance]) -> list[Stance]:
    """Every stored stance row, read through the repository.

    A generous single page: ~190 rows by design, so one page is the whole table
    and a full page would be a bug this read surfaces rather than hides.
    """
    rows = stances.query(limit=1000)
    assert len(rows) < 1000, "the stance table should be ~190 rows; a full page means paging"
    return rows


def _subtopic_ids() -> set[str]:
    """Story 3.1's 60 subtopic ids, derived rather than retyped."""
    return {str(row.id) for row in taxonomy_rows() if row.parent_id is not None}


def _top_level_ids() -> set[str]:
    return {str(row.id) for row in taxonomy_rows() if row.parent_id is None}


def _by_topic(stances: Repository[Stance]) -> dict[str, list[Stance]]:
    grouped: dict[str, list[Stance]] = {}
    for row in _stored_stances(stances):
        grouped.setdefault(row.topic_id, []).append(row)
    return grouped


@then(parsers.parse("every subtopic has at least {minimum:d} stored stances"))
def every_subtopic_has_minimum_stances(stances: Repository[Stance], minimum: int) -> None:
    grouped = _by_topic(stances)
    thin = {
        topic_id: len(grouped.get(topic_id, []))
        for topic_id in sorted(_subtopic_ids())
        if len(grouped.get(topic_id, [])) < minimum
    }
    assert not thin, f"subtopics with fewer than {minimum} stances: {thin}"


@then("no stored stance references a top-level topic")
def no_stance_on_a_parent(stances: Repository[Stance]) -> None:
    """Stances hang off the leaves, so a voter's pick is always specific.

    A stance on "Healthcare" rather than on "Prescription drug pricing" would
    be too coarse to map to a roll call, which is what story 4's scoring needs.
    """
    offenders = [
        (row.id, row.topic_id)
        for row in _stored_stances(stances)
        if row.topic_id in _top_level_ids()
    ]
    assert not offenders, f"stances attached to a top-level topic: {offenders}"


@then("every stored stance's topic_id names a stored subtopic")
def stance_topics_resolve(stances: Repository[Stance], topics: Repository[IssueTopic]) -> None:
    stored_topic_ids = {str(row.id) for row in topics.query(limit=500)}
    dangling = [
        (row.id, row.topic_id)
        for row in _stored_stances(stances)
        if row.topic_id not in stored_topic_ids
    ]
    assert not dangling, f"stances whose topic_id is not stored: {dangling}"


@then(parsers.parse("every stored stance has wording version {version:d}"))
def every_stance_version(stances: Repository[Stance], version: int) -> None:
    wrong = [row.id for row in _stored_stances(stances) if row.wording_version != version]
    assert not wrong, f"not at wording_version {version}: {wrong}"


@then("the stance seed reports one row per declared stance")
def seed_report_counts(result: dict[str, Any]) -> None:
    report = result["report"]
    assert report.row_count == len(STANCES), (
        f"reported {report.row_count} rows, declared {len(STANCES)}"
    )
    assert set(report.stances_written) == stance_row_ids()


@then("every stored stance id is its topic id followed by a slug")
def ids_match_pattern(stances: Repository[Stance]) -> None:
    """datamodel.md's `{topic_id}:{slug}`, checked on the stored id."""
    bad: list[str] = []
    for row in _stored_stances(stances):
        row_id = str(row.id)
        if not ID_PATTERN.match(row_id):
            bad.append(row_id)
        elif not row_id.startswith(f"{row.topic_id}:"):
            bad.append(f"{row_id} does not start with its topic_id {row.topic_id}")
    assert not bad, f"ids that are not {{topic_id}}:{{slug}}: {bad[:5]}"


# --- spanning the spectrum --------------------------------------------------


@then("no subtopic's stances all share one direction code")
def directions_are_not_uniform(stances: Repository[Stance]) -> None:
    """The acceptance criterion's real content, stated negatively.

    Three stances all coded `+1` would be three flavours of one position
    dressed up as a choice, which is the failure mode "balanced" exists to
    exclude. Checked over what is stored rather than over the declaration, so
    a row written by any other path is caught too.
    """
    uniform = {
        topic_id: sorted({row.direction_code for row in rows})
        for topic_id, rows in sorted(_by_topic(stances).items())
        if len({row.direction_code for row in rows}) < 2
    }
    assert not uniform, f"subtopics whose stances all point the same way: {uniform}"


@then("every subtopic offers at least one negative and one positive direction code")
def directions_span_both_signs(stances: Repository[Stance]) -> None:
    """Two distinct codes is not enough: `+1` and `+2` are one side twice.

    An axis with options only on one side plus a neutral is still a one-sided
    menu, so the check is on the signs rather than on the count of distinct
    values.
    """
    one_sided = {}
    for topic_id, rows in sorted(_by_topic(stances).items()):
        codes = [int(row.direction_code) for row in rows]
        if not (any(code < 0 for code in codes) and any(code > 0 for code in codes)):
            one_sided[topic_id] = sorted(codes)
    assert not one_sided, f"subtopics offering only one side of the axis: {one_sided}"


@then("every stored direction code is a signed integer between -2 and 2")
def direction_codes_are_in_range(stances: Repository[Stance]) -> None:
    bad = [
        (row.id, row.direction_code)
        for row in _stored_stances(stances)
        if not re.fullmatch(r"[-+]?[012]", row.direction_code) or abs(int(row.direction_code)) > 2
    ]
    assert not bad, f"direction codes outside the documented scheme: {bad[:5]}"


@then("no two stances under one subtopic share a direction code")
def directions_are_distinct_within_a_subtopic(stances: Repository[Stance]) -> None:
    """One position per point on the axis, so the menu has no duplicate slot.

    Two stances both coded `-1` would make the negative side of the axis twice
    as likely to be picked by a voter skimming, which is a nudge even when both
    wordings are fair.
    """
    clashes = {}
    for topic_id, rows in sorted(_by_topic(stances).items()):
        codes = [int(row.direction_code) for row in rows]
        duplicated = sorted({code for code in codes if codes.count(code) > 1})
        if duplicated:
            clashes[topic_id] = duplicated
    assert not clashes, f"subtopics with a repeated direction code: {clashes}"


@then("no stance label contains a partisan-identity word")
def labels_avoid_partisan_identity(stances: Repository[Stance]) -> None:
    """A stance states an outcome; it never names the tribe that wants it.

    This is the stance-card analogue of story 3.1's topic-name check, and
    deliberately a different list: a stance card's job IS to state a position,
    so `contains_stance_presupposing_word` would reject every row here. What
    must not appear is a party, movement or pejorative label — see
    `app.seed_stances.PARTISAN_IDENTITY_WORDS`.
    """
    offenders = [
        (row.id, row.label, hit)
        for row in _stored_stances(stances)
        if (hit := contains_partisan_identity_word(row.label)) is not None
    ]
    assert not offenders, f"stance labels naming a side: {offenders[:5]}"


# --- no stance is preselected -----------------------------------------------


@then("no stored stance row carries a default or preselected field")
def no_default_field_stored(zerodb: ZeroDBClient) -> None:
    """Read from raw `row_data`, not through the model.

    Through `Stance` the answer is trivially yes — `extra="forbid"` would have
    rejected the field at write time. The question worth asking is whether the
    bytes in storage carry one, which only an unvalidated read can answer.
    """
    page = zerodb.query_rows("stances", limit=1000)
    forbidden = {
        "default",
        "preselected",
        "selected",
        "recommended",
        "is_default",
        "default_choice",
    }
    offenders = [
        (row.row_data.get("id"), sorted(set(row.row_data) & forbidden))
        for row in page.rows
        if set(row.row_data) & forbidden
    ]
    assert not offenders, f"stored stance rows carrying a selection flag: {offenders[:5]}"


@then("the write is refused as an unknown field")
def write_refused_unknown_field(result: dict[str, Any]) -> None:
    refusal = result["refusal"]
    assert refusal is not None, "a stance carrying `default: True` was accepted"
    assert "default" in str(refusal), f"refused, but not for the `default` field: {refusal}"


# --- display order ----------------------------------------------------------


@then("the two reads return the same set of stances")
def reads_agree_on_the_set(result: dict[str, Any]) -> None:
    first = {row.id for row in result["first_read"]}
    second = {row.id for row in result["second_read"]}
    assert first == second, f"the two reads disagree: {first ^ second}"
    assert len(first) >= 3, f"only {len(first)} stance(s) came back"


@then("the returned order differs between the two reads")
def the_returned_order_differs(result: dict[str, Any]) -> None:
    """Server-side shuffle per call, so no position is a persistent default.

    What this proves is narrow and deliberate: the backend does not hand the
    caller a stable ranking. Session-level consistency (the same voter seeing
    the same order throughout one session) is the client's job — see
    `app.stances.service`'s module docstring for why the backend does not keep
    session state for it.
    """
    first = [row.id for row in result["first_read"]]
    second = [row.id for row in result["second_read"]]
    assert first != second, f"both reads returned the same order: {first}"


@then("each read returns every stance stored for that subtopic")
def each_read_is_complete(result: dict[str, Any], stances: Repository[Stance]) -> None:
    expected = {
        str(row.id) for row in _stored_stances(stances) if row.topic_id == result["topic_id"]
    }
    assert expected, f"nothing is stored for {result['topic_id']}"
    for key in ("first_read", "second_read"):
        got = {str(row.id) for row in result[key]}
        assert got == expected, f"{key} is missing {expected - got} and has extra {got - expected}"


@then("no returned stance is flagged as selected, default or recommended")
def nothing_is_flagged(result: dict[str, Any]) -> None:
    """The model has no such field, so this asserts the serialised shape too."""
    forbidden = {"default", "preselected", "selected", "recommended", "is_default"}
    for key in ("first_read", "second_read"):
        for row in result[key]:
            carried = set(row.row_data()) & forbidden
            assert not carried, f"{row.id} carries {sorted(carried)}"


# --- idempotency ------------------------------------------------------------


def _rows_with_id(fake: FakeZeroDBTransport, table: str, row_id: str) -> int:
    """How many rows the fake's storage holds under one natural-key id.

    Read from the fake's own storage rather than through the client, because
    `get_row` stops at the first match and so cannot distinguish one row from
    two — which is the entire question an idempotency scenario asks.
    """
    stored = fake.rows.get(table, {})
    return sum(1 for row in stored.values() if row["row_data"].get("id") == row_id)


@then(parsers.parse("exactly {count:d} row carries each seeded stance id"))
def one_row_per_stance(fake: FakeZeroDBTransport, count: int) -> None:
    counts = {row_id: _rows_with_id(fake, "stances", row_id) for row_id in stance_row_ids()}
    offenders = {row_id: n for row_id, n in counts.items() if n != count}
    assert not offenders, (
        f"ids with a row count other than {count}: {dict(list(offenders.items())[:5])}"
    )


@then("the second stance run issued no row inserts")
def second_run_had_no_inserts(fake: FakeZeroDBTransport) -> None:
    """The mechanism, not the symptom: ZeroDB's `POST /rows` always appends."""
    inserts = [
        call
        for call in fake.calls
        if call.method == "POST" and call.path.endswith("/rows") and "/stances/" in call.path
    ]
    assert not inserts, f"the second run appended {len(inserts)} new row(s)"


@then("the second stance run updated every stance row")
def second_run_updated_rows(fake: FakeZeroDBTransport) -> None:
    updates = [
        call
        for call in fake.calls
        if call.method == "PUT" and "/rows/" in call.path and "/stances/" in call.path
    ]
    assert len(updates) == len(STANCES), f"expected {len(STANCES)} updates, saw {len(updates)}"


@then(parsers.parse('the stance "{row_id}" has its seeded label back'))
def label_restored(stances: Repository[Stance], row_id: str) -> None:
    declared = {str(row.id): row for row in stance_rows()}[row_id]
    stored = stances.get(row_id)
    assert stored is not None, f"stance {row_id!r} is not stored"
    assert stored.label == declared.label, (
        f"{row_id} is labelled {stored.label!r}, expected {declared.label!r}"
    )


# --- reference integrity ----------------------------------------------------


@then("the stance write is refused for a missing topic")
def stance_write_refused(result: dict[str, Any]) -> None:
    refusal = result["refusal"]
    assert isinstance(refusal, MissingReferenceError), (
        "a stance referencing a subtopic that does not exist was accepted"
    )
    assert refusal.referenced_table == "issue_topics"
    assert refusal.referenced_id == "en-US:zzz-no-such-subtopic:v1"


# --- a guard on the module-level data itself --------------------------------


def test_stance_rows_and_row_ids_agree() -> None:
    """`stance_row_ids()` is what the live verifier reads back.

    Derived from `stance_rows()` rather than listed separately, so the two
    cannot drift — a verifier checking a stale id list would report PASS on
    rows the seed no longer writes.
    """
    assert stance_row_ids() == {str(row.id) for row in stance_rows()}


def test_every_declared_stance_card_is_well_formed() -> None:
    """The declaration itself, before any ZeroDB machinery is involved."""
    for card in STANCES:
        assert isinstance(card, StanceCard)
        assert card.topic_slug
        assert card.slug
        assert card.row_id.endswith(f":{card.slug}")
        assert card.row_id.startswith(f"{TAXONOMY_LOCALE}:{card.topic_slug}:v{TAXONOMY_VERSION}")
