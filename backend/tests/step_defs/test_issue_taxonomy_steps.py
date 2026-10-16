"""Step definitions for tests/features/issue_taxonomy.feature (story 3.1).

Runs against story 1.2's in-memory fake transport, never live ZeroDB, so CI
needs no credentials — the rule every backend feature in this suite follows.
The live confirmation of the same behaviour lives in
`tests/test_issue_taxonomy_live_shapes.py` and
`scripts/verify_issue_taxonomy_live.py`.

The fake has `break_server_side_filter()` applied for the same reason story
2.1's steps do: live ZeroDB's server-side `/query` filter matches nothing
(story 1.4's finding), and the seed's idempotency rests entirely on the
client's own paging fallback. A fake with a working filter would make every
"exactly 1 row" scenario below pass trivially.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.repositories import MissingReferenceError, Repository
from app.repositories.models import IssueTopic
from app.seed_taxonomy import (
    TAXONOMY_LOCALE,
    TAXONOMY_VERSION,
    TaxonomySeeder,
    contains_stance_presupposing_word,
    taxonomy_row_ids,
    taxonomy_rows,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/issue_taxonomy.feature")

#: datamodel.md's documented id pattern for `issue_topics`: `{locale}:{slug}:v{n}`.
ID_PATTERN = re.compile(r"^[a-z]{2}-[A-Z]{2}:[a-z0-9]+(?:-[a-z0-9]+)*:v\d+$")


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
    return client


@pytest.fixture
def topics(zerodb: ZeroDBClient) -> Repository[IssueTopic]:
    return Repository(client=zerodb, model=IssueTopic)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


@given("a ZeroDB client backed by an in-memory fake", target_fixture="taxonomy_client")
def taxonomy_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the issue topics table exists")
def issue_topics_table_exists(zerodb: ZeroDBClient) -> None:
    """A no-op: the `zerodb` fixture already creates the table with no rows."""


# --- running the seed ------------------------------------------------------


@when("I seed the issue taxonomy")
def seed_taxonomy(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    result["report"] = TaxonomySeeder(zerodb, pace=lambda: None).run()


@when("I seed the issue taxonomy again")
def seed_taxonomy_again(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    result["report"] = TaxonomySeeder(zerodb, pace=lambda: None).run()


@given("the issue taxonomy has already been seeded once")
def taxonomy_already_seeded(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    result["first_report"] = TaxonomySeeder(zerodb, pace=lambda: None).run()
    fake.calls.clear()


@given(parsers.parse('the topic "{row_id}" has been hand-edited to name "{name}"'))
def hand_edit_topic(zerodb: ZeroDBClient, row_id: str, name: str) -> None:
    """Simulate someone changing a seeded row out from under the seed.

    The seed is the single source of truth for the taxonomy, so a re-run must
    restore the row rather than leave the edit in place — which an `upsert`
    that quietly skipped existing rows would not do.
    """
    zerodb.update_row("issue_topics", row_id, {"name": name})


@when("I seed a subtopic whose parent is missing")
def seed_orphan_subtopic(topics: Repository[IssueTopic], result: dict[str, Any]) -> None:
    try:
        topics.upsert(
            {
                "id": "en-US:zzz-orphan-subtopic:v1",
                "parent_id": "en-US:zzz-no-such-parent:v1",
                "name": "Orphan subtopic",
                "locale": TAXONOMY_LOCALE,
                "taxonomy_version": TAXONOMY_VERSION,
            }
        )
    except MissingReferenceError as exc:
        result["refusal"] = exc
    else:
        result["refusal"] = None


# --- reading the taxonomy back ---------------------------------------------


def _stored(topics: Repository[IssueTopic]) -> list[IssueTopic]:
    """Every stored topic row, read through the repository.

    A generous single page: the taxonomy is ~80 rows by design, two orders of
    magnitude below `member_votes`, so one page is the whole table here and a
    larger stored count would be a bug this read should surface rather than
    silently truncate.
    """
    rows = topics.query(limit=500)
    assert len(rows) < 500, "the taxonomy should be ~80 rows; a full page means paging is needed"
    return rows


def _parents(topics: Repository[IssueTopic]) -> list[IssueTopic]:
    return [row for row in _stored(topics) if row.parent_id is None]


def _children(topics: Repository[IssueTopic]) -> list[IssueTopic]:
    return [row for row in _stored(topics) if row.parent_id is not None]


@then(parsers.parse("the taxonomy has {count:d} top-level topics"))
def top_level_count(topics: Repository[IssueTopic], count: int) -> None:
    actual = len(_parents(topics))
    assert actual == count, f"{actual} top-level topics stored, expected {count}"


@then(parsers.parse("the taxonomy has {count:d} subtopics"))
def subtopic_count(topics: Repository[IssueTopic], count: int) -> None:
    actual = len(_children(topics))
    assert actual == count, f"{actual} subtopics stored, expected {count}"


@then(parsers.parse('every seeded topic has locale "{locale}"'))
def every_topic_locale(topics: Repository[IssueTopic], locale: str) -> None:
    wrong = [row.id for row in _stored(topics) if row.locale != locale]
    assert not wrong, f"not in {locale}: {wrong}"


@then(parsers.parse("every seeded topic has taxonomy version {version:d}"))
def every_topic_version(topics: Repository[IssueTopic], version: int) -> None:
    wrong = [row.id for row in _stored(topics) if row.taxonomy_version != version]
    assert not wrong, f"not at taxonomy_version {version}: {wrong}"


@then("every top-level topic has no parent")
def parents_have_no_parent(topics: Repository[IssueTopic]) -> None:
    stored = _stored(topics)
    roots = {row.id for row in stored if row.parent_id is None}
    assert roots, "no top-level topics were stored at all"


@then("every subtopic's parent is a stored top-level topic")
def children_point_at_a_root(topics: Repository[IssueTopic]) -> None:
    stored = _stored(topics)
    roots = {row.id for row in stored if row.parent_id is None}
    dangling = [
        (row.id, row.parent_id)
        for row in stored
        if row.parent_id is not None
        if row.parent_id not in roots
    ]
    assert not dangling, f"subtopics whose parent is not a stored top-level topic: {dangling}"


@then(parsers.parse("every top-level topic has at least {minimum:d} subtopics"))
def each_root_has_children(topics: Repository[IssueTopic], minimum: int) -> None:
    stored = _stored(topics)
    counts = {row.id: 0 for row in stored if row.parent_id is None}
    for row in stored:
        if row.parent_id in counts:
            counts[row.parent_id] += 1
    thin = {topic_id: count for topic_id, count in counts.items() if count < minimum}
    assert not thin, f"top-level topics with fewer than {minimum} subtopics: {thin}"


@then("no subtopic is the parent of another topic")
def tree_is_two_levels(topics: Repository[IssueTopic]) -> None:
    """datamodel.md's `parent_id` permits any depth; story 3.1 specifies two.

    A third level would break the "about 20 topics and 60 subtopics" count the
    acceptance criterion states, and would mean a voter picking a topic could
    be shown grandchildren the stance cards of story 3.2 do not cover.
    """
    stored = _stored(topics)
    children_ids = {row.id for row in stored if row.parent_id is not None}
    grandchildren = [(row.id, row.parent_id) for row in stored if row.parent_id in children_ids]
    assert not grandchildren, f"the tree is three levels deep at: {grandchildren}"


@then(parsers.parse('the topic "{row_id}" exists with name "{name}"'))
def topic_exists_with_name(topics: Repository[IssueTopic], row_id: str, name: str) -> None:
    row = topics.get(row_id)
    assert row is not None, f"topic {row_id!r} is not stored"
    assert row.name == name, f"{row_id} is named {row.name!r}, expected {name!r}"


@then(parsers.parse('the topic "{row_id}" has parent "{parent_id}"'))
def topic_has_parent(topics: Repository[IssueTopic], row_id: str, parent_id: str) -> None:
    row = topics.get(row_id)
    assert row is not None, f"topic {row_id!r} is not stored"
    assert row.parent_id == parent_id
    assert topics.get(parent_id) is not None, f"parent {parent_id!r} is not stored"


@then(parsers.parse('every seeded id is a locale, a slug and a version, like "{example}"'))
def ids_match_pattern(topics: Repository[IssueTopic], example: str) -> None:
    """datamodel.md's `{locale}:{slug}:v{n}`, with the example asserted too.

    The example from the Gherkin is checked against the same pattern, so a
    scenario that quoted a shape the regex does not actually accept fails here
    rather than silently documenting the wrong pattern.
    """
    assert ID_PATTERN.match(example), f"the scenario's own example {example!r} does not match"
    bad = [row.id for row in _stored(topics) if not ID_PATTERN.match(str(row.id))]
    assert not bad, f"ids that are not {{locale}}:{{slug}}:v{{n}}: {bad}"


# --- naming neutrality ------------------------------------------------------


@then("no topic name contains a stance-presupposing word")
def names_are_neutral(topics: Repository[IssueTopic]) -> None:
    """prd.md's first product principle, enforced on the data rather than trusted.

    See `app.seed_taxonomy.STANCE_PRESUPPOSING_WORDS` for the word list and why
    each entry is on it.
    """
    offenders = [
        (row.id, row.name, hit)
        for row in _stored(topics)
        if (hit := contains_stance_presupposing_word(row.name)) is not None
    ]
    assert not offenders, f"stance-presupposing topic names: {offenders}"


@then("no two seeded topics share a name")
def names_are_unique(topics: Repository[IssueTopic]) -> None:
    names = [row.name for row in _stored(topics)]
    duplicates = {name for name in names if names.count(name) > 1}
    assert not duplicates, f"duplicate topic names: {sorted(duplicates)}"


@then("no two seeded topics share an id")
def ids_are_unique(topics: Repository[IssueTopic]) -> None:
    ids = [row.id for row in _stored(topics)]
    duplicates = {row_id for row_id in ids if ids.count(row_id) > 1}
    assert not duplicates, f"duplicate topic ids: {sorted(str(d) for d in duplicates)}"


# --- the report -------------------------------------------------------------


@then(
    parsers.parse("the taxonomy seed reports {parents:d} topics and {children:d} subtopics written")
)
def seed_report_counts(result: dict[str, Any], parents: int, children: int) -> None:
    report = result["report"]
    assert len(report.topics_written) == parents, (
        f"reported {len(report.topics_written)} topics, expected {parents}"
    )
    assert len(report.subtopics_written) == children, (
        f"reported {len(report.subtopics_written)} subtopics, expected {children}"
    )


# --- idempotency ------------------------------------------------------------


def _rows_with_id(fake: FakeZeroDBTransport, table: str, row_id: str) -> int:
    """How many rows the fake's storage holds under one natural-key id.

    Read from the fake's own storage rather than through the client, because
    `get_row` stops at the first match and so cannot distinguish one row from
    two — which is the entire question an idempotency scenario asks.
    """
    stored = fake.rows.get(table, {})
    return sum(1 for row in stored.values() if row["row_data"].get("id") == row_id)


@then(parsers.parse("exactly {count:d} row carries each seeded topic id"))
def one_row_per_topic(fake: FakeZeroDBTransport, count: int) -> None:
    wrong = {row_id: _rows_with_id(fake, "issue_topics", row_id) for row_id in taxonomy_row_ids()}
    offenders = {row_id: n for row_id, n in wrong.items() if n != count}
    assert not offenders, f"ids with a row count other than {count}: {offenders}"


@then("the second run issued no row inserts")
def second_run_had_no_inserts(fake: FakeZeroDBTransport) -> None:
    """The mechanism, not the symptom: ZeroDB's `POST /rows` always appends.

    "Exactly one row per id" holds only as long as the upsert's find step
    resolves the natural-key id to an internal UUID and PUTs it, so the second
    run must issue zero POSTs — asserted here rather than inferred from the
    final count.
    """
    inserts = [call for call in fake.calls if call.method == "POST" and call.path.endswith("/rows")]
    assert not inserts, f"the second run appended {len(inserts)} new row(s)"


@then(parsers.parse("the second run updated {count:d} rows"))
def second_run_updated_rows(fake: FakeZeroDBTransport, count: int) -> None:
    updates = [call for call in fake.calls if call.method == "PUT" and "/rows/" in call.path]
    assert len(updates) == count, f"expected {count} updates, saw {len(updates)}"


# --- ordering and reference integrity --------------------------------------


@then(parsers.parse('the topic "{parent_id}" was written before "{child_id}"'))
def parent_written_first(fake: FakeZeroDBTransport, parent_id: str, child_id: str) -> None:
    """Order matters: the subtopic's reference check reads its parent's row.

    Asserted on the recorded calls rather than the final state, because the
    final state is identical either way — and seeding a child first would fail
    the repository's reference check live while passing a state assertion.
    """
    writes = [
        call
        for call in fake.calls
        if call.method in {"POST", "PUT"} and "/rows" in call.path and call.json is not None
    ]
    positions = {
        _written_id(call.json): index
        for index, call in enumerate(writes)
        if _written_table(call.path) == "issue_topics"
    }
    assert parent_id in positions, f"{parent_id} was never written"
    assert child_id in positions, f"{child_id} was never written"
    assert positions[parent_id] < positions[child_id], (
        f"{child_id} was written at call {positions[child_id]}, "
        f"before {parent_id} at {positions[parent_id]}"
    )


def _written_table(path: str) -> str | None:
    """The table a `.../tables/{table}/rows[/{uuid}]` write addressed."""
    segments = path.split("/")
    try:
        return segments[segments.index("tables") + 1]
    except (ValueError, IndexError):  # pragma: no cover - not a table path
        return None


def _written_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    row_data = payload.get("row_data")
    if isinstance(row_data, dict):
        return row_data.get("id")
    return payload.get("id")


@then("the write is refused for a missing parent")
def write_refused(result: dict[str, Any]) -> None:
    refusal = result["refusal"]
    assert isinstance(refusal, MissingReferenceError), (
        "a subtopic referencing a parent that does not exist was accepted"
    )
    assert refusal.referenced_table == "issue_topics"
    assert refusal.referenced_id == "en-US:zzz-no-such-parent:v1"


# --- a guard on the module-level data itself --------------------------------


def test_taxonomy_rows_and_row_ids_agree() -> None:
    """`taxonomy_row_ids()` is what the live verifier reads back.

    It is derived from `taxonomy_rows()` rather than written out separately, so
    this pins that they cannot drift — a verifier reading a stale id list would
    report PASS on rows the seed no longer writes.
    """
    assert taxonomy_row_ids() == {str(row.id) for row in taxonomy_rows()}
