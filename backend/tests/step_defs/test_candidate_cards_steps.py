"""Step definitions for tests/features/candidate_cards.feature (story 7.2).

Runs against story 1.2's in-memory fake transport, never live ZeroDB, so CI
needs no credentials — the rule every backend feature in this suite follows.
The live confirmation lives in `scripts/verify_candidate_cards_live.py`, and
what it can and cannot confirm is stated in that script and in the feature
file's own header: `positions.recomputed` is a real live trigger,
`verdict.published` does not exist yet.

`break_server_side_filter()` is applied for the reason every story since 1.4
applies it: live ZeroDB's server-side `/query` filter matches nothing, so every
read here rests on the client's own paging fallback. It matters acutely in this
story, twice over — `cards_for_race` filters on (election_id, district_id) and
a leaked filter would put a candidate on the wrong ballot, and
`_candidacies_of` filters on `person_id`, where a leak would put one
candidate's record under another's name.

All synthetic. The test people are `person:TEST7200000`/`TEST7200001`, which are
not real bioguide ids, and no real candidate's card is built anywhere in this
suite. Nothing voter-derived exists in this story at all: a card is a read
model over public candidate records.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.ballot.cards import (
    VERDICT_PUBLISHED_TOPIC,
    CandidateCardBuilder,
    CandidateCardReader,
    CandidateCardRebuildConsumer,
    emit_verdict_published,
)
from app.bootstrap import Bootstrapper
from app.repositories import Repository
from app.repositories.models import (
    Candidacy,
    CandidateCard,
    CandidatePosition,
    District,
    Election,
    IssueTopic,
    Person,
    Promise,
    PromiseSource,
    Stance,
    Verdict,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/candidate_cards.feature")

# --- synthetic ids, every one marked as such ------------------------------
#
# `TEST72` in the middle so a human reading a failure can tell at a glance
# that nothing here is a real member, district or election.

PERSON_ID = "person:TEST7200000"
MIRROR_PERSON_ID = "person:TEST7200001"
OTHER_PERSON_ID = "person:TEST7200002"
NO_CANDIDACY_PERSON_ID = "person:TEST7200003"

ELECTION_ID = "us-test72-11-03-general"
OTHER_ELECTION_ID = "us-test72-06-02-primary"
DISTRICT_ID = "ocd-division/country:us/state:zz/cd:test72"
OTHER_DISTRICT_ID = "ocd-division/country:us/state:zz/cd:test72-other"
DISTRICT_NAME = "Test Congressional District 72"

TOPIC_ID = "en-US:test72-topic:v1"
STANCE_ID = f"{TOPIC_ID}:for"
OTHER_STANCE_ID = f"{TOPIC_ID}:against"

CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"
SECOND_CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{OTHER_PERSON_ID}"
OTHER_ELECTION_CANDIDACY_ID = f"{OTHER_ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"
MIRROR_CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{MIRROR_PERSON_ID}"
OTHER_DISTRICT_CANDIDACY_ID = f"{ELECTION_ID}:{OTHER_DISTRICT_ID}:{OTHER_PERSON_ID}"

METHODOLOGY_VERSION = "2026.1"


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
    Bootstrapper(client, pace=lambda: None).run()
    return client


@pytest.fixture
def builder(zerodb: ZeroDBClient) -> CandidateCardBuilder:
    # `now` pinned so `updated_at` is deterministic and a test can assert a
    # merge preserved a field without the timestamp making every row differ.
    return CandidateCardBuilder(zerodb, now=lambda: "2026-10-19T12:00:00Z")


@pytest.fixture
def reader(zerodb: ZeroDBClient) -> CandidateCardReader:
    return CandidateCardReader(zerodb)


@pytest.fixture
def consumer(zerodb: ZeroDBClient, builder: CandidateCardBuilder) -> CandidateCardRebuildConsumer:
    return CandidateCardRebuildConsumer(zerodb, builder=builder, pace=lambda: None)


@pytest.fixture
def seen() -> dict[str, Any]:
    """Carries state between Given, When and Then.

    Nothing voter-derived can reach it: this story reads public candidate
    records only, and there is no voter-shaped value anywhere in its inputs.
    """
    return {"promises": 0, "result": None, "cards": []}


# --- helpers --------------------------------------------------------------


def _cards(zerodb: ZeroDBClient) -> Repository[CandidateCard]:
    return Repository(client=zerodb, model=CandidateCard)


def _seed_reference_rows(zerodb: ZeroDBClient) -> None:
    """The rows a candidacy and a card reference.

    Written through real repositories rather than raw `upsert_row`, so story
    1.4's reference checks are actually exercised: a test that bypassed them
    could pass against data the production write path would refuse.
    """
    Repository(client=zerodb, model=Election).upsert_many(
        [
            {
                "id": ELECTION_ID,
                "jurisdiction_id": "us-federal",
                "name": "Test 72 general",
                "election_date": "2026-11-03",
                "kind": "general",
            },
            {
                "id": OTHER_ELECTION_ID,
                "jurisdiction_id": "us-federal",
                "name": "Test 72 primary",
                "election_date": "2026-06-02",
                "kind": "primary",
            },
        ]
    )
    Repository(client=zerodb, model=District).upsert_many(
        [
            {
                "id": DISTRICT_ID,
                "body_id": "us-house",
                "name": DISTRICT_NAME,
                "state": "ZZ",
                "valid_from": "2025-01-03",
            },
            {
                "id": OTHER_DISTRICT_ID,
                "body_id": "us-house",
                "name": "Test Congressional District 72-other",
                "state": "ZZ",
                "valid_from": "2025-01-03",
            },
        ]
    )
    Repository(client=zerodb, model=IssueTopic).upsert(
        {
            "id": TOPIC_ID,
            "name": "Synthetic story 7.2 topic",
            "locale": "en-US",
            "taxonomy_version": 1,
        }
    )
    Repository(client=zerodb, model=Stance).upsert_many(
        [
            {
                "id": STANCE_ID,
                "topic_id": TOPIC_ID,
                "label": "Synthetic stance, for",
                "description": "A synthetic stance used only by story 7.2's tests.",
                "direction_code": "+2",
                "wording_version": 1,
            },
            {
                "id": OTHER_STANCE_ID,
                "topic_id": TOPIC_ID,
                "label": "Synthetic stance, against",
                "description": "A second synthetic stance used only by story 7.2's tests.",
                "direction_code": "-2",
                "wording_version": 1,
            },
        ]
    )


def _person(zerodb: ZeroDBClient, person_id: str, *, name: str, photo: str | None) -> None:
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": person_id,
            "canonical_name": name,
            "photo_url": photo,
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": DISTRICT_ID,
                    "party": "IND",
                    "start": "2025-01-03",
                }
            ],
        }
    )


def _candidacy(
    zerodb: ZeroDBClient,
    *,
    person_id: str,
    election_id: str = ELECTION_ID,
    district_id: str = DISTRICT_ID,
    person_name: str,
    party: str | None = "IND",
) -> str:
    stored = Repository(client=zerodb, model=Candidacy).upsert(
        {
            "election_id": election_id,
            "district_id": district_id,
            "person_id": person_id,
            "person_name": person_name,
            "party": party,
            "is_incumbent": True,
            "status": "filed",
        }
    )
    return stored.id or ""


def _position(
    zerodb: ZeroDBClient,
    *,
    person_id: str,
    stance_id: str = STANCE_ID,
    s_value: float,
    methodology_version: str = METHODOLOGY_VERSION,
) -> None:
    """Append a `candidate_positions` version through the real repository.

    `append_version` rather than `upsert`, because the table is append-only
    (datamodel.md rule 6) and `Repository.upsert` refuses it — the same path
    story 6.2's scorer takes, so `is_current` is assigned by the code under
    test's own rules rather than by this test.
    """
    Repository(client=zerodb, model=CandidatePosition).append_version(
        {
            "person_id": person_id,
            "stance_id": stance_id,
            "s_value": s_value,
            "confidence": 0.8,
            "evidence_count": 4,
            "basis": "votes",
            "methodology_version": methodology_version,
            "computed_at": "2026-10-19T11:00:00Z",
        }
    )


def _verdict(
    zerodb: ZeroDBClient,
    *,
    person_id: str,
    candidacy_id: str,
    value: str,
    promise_index: int,
) -> str:
    """A promise and a current verdict on it, through the real repositories.

    A verdict references a promise, which references a promise source, which
    references the candidacy — so a verdict cannot be written in isolation and
    this helper builds the whole chain. That is the point: the reference
    checks story 1.4 enforces are exercised, so these tests cannot pass
    against a `verdicts` row the production write path would refuse.
    """
    source_id = f"src:test72{promise_index:04d}"
    Repository(client=zerodb, model=PromiseSource).upsert(
        {
            "id": source_id,
            "candidacy_id": candidacy_id,
            "source_type": "campaign_site",
            "url": f"https://example.invalid/test72/{promise_index}",
            # A synthetic file id. `PromiseSource.archived_file_id` is
            # required because story 9.1's whole point is that a citation
            # resolves to stored bytes; this suite never fetches it, and the
            # id is marked synthetic so it cannot be mistaken for a real
            # archive.
            "archived_file_id": f"file:test72-{promise_index:04d}",
            "captured_at": "2026-10-01T00:00:00Z",
        }
    )
    promise_id = f"prm:test72{promise_index:04d}"
    Repository(client=zerodb, model=Promise).upsert(
        {
            "id": promise_id,
            "candidacy_id": candidacy_id,
            "person_id": person_id,
            "source_id": source_id,
            "quote_span": f"I will vote for synthetic measure {promise_index}.",
            "actor": "I",
            "action": "vote for",
            "object": f"synthetic measure {promise_index}",
            "direction": "support",
            "testable": True,
            "extractor_version": "test72/v1",
            "confidence": 0.9,
        }
    )
    Repository(client=zerodb, model=Verdict).append_version(
        {
            "promise_id": promise_id,
            "person_id": person_id,
            "verdict": value,
            "confidence": 0.8,
            "methodology_version": METHODOLOGY_VERSION,
            "qa_status": "approved",
            "created_at": "2026-10-19T11:00:00Z",
        }
    )
    return promise_id


def _read_calls(fake: FakeZeroDBTransport) -> list[Any]:
    """Every recorded call that reads rows — a `/query` POST or a `/rows` GET.

    What "never aggregates at request time" is actually asserted against. See
    `app.ballot.cards`'s module docstring for why the claim is about *tables
    read*, not HTTP requests: against live ZeroDB one logical read is a POST
    plus one or more paging GETs, because the server-side filter matches
    nothing (story 1.4), and the fake reproduces that.
    """
    return [
        call
        for call in fake.calls
        if ("/query" in call.path and call.method == "POST")
        or ("/rows" in call.path and call.method == "GET")
    ]


def _tables_read(fake: FakeZeroDBTransport) -> set[str]:
    """Which tables a recorded sequence of calls touched."""
    tables: set[str] = set()
    for call in _read_calls(fake):
        # ".../database/tables/{table}/query" or ".../tables/{table}/rows"
        parts = call.path.split("/tables/")
        if len(parts) == 2:
            tables.add(parts[1].split("/")[0])
    return tables


# --- Background -----------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _a_client(zerodb: ZeroDBClient) -> None:
    """The fixture does the work; this step names it in the feature file."""


@given("a candidacy for a synthetic incumbent")
def _a_candidacy(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _seed_reference_rows(zerodb)
    _person(zerodb, PERSON_ID, name="TEST72, Alpha", photo="https://example.invalid/alpha.jpg")
    seen["candidacy_id"] = _candidacy(zerodb, person_id=PERSON_ID, person_name="TEST72, ALPHA")


# --- Given ----------------------------------------------------------------


@given("the card for that candidacy has been rebuilt")
def _card_rebuilt(builder: CandidateCardBuilder, seen: dict[str, Any]) -> None:
    builder.rebuild_candidacy(seen["candidacy_id"])


@given("a second candidacy in the same district")
def _second_candidacy_same_district(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _person(zerodb, OTHER_PERSON_ID, name="TEST72, Beta", photo=None)
    seen["second_candidacy_id"] = _candidacy(
        zerodb, person_id=OTHER_PERSON_ID, person_name="TEST72, BETA"
    )


@given("a third candidacy in a different district")
def _third_candidacy_other_district(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    seen["other_district_candidacy_id"] = _candidacy(
        zerodb,
        person_id=OTHER_PERSON_ID,
        district_id=OTHER_DISTRICT_ID,
        person_name="TEST72, BETA",
    )


@given("every card has been rebuilt")
def _every_card_rebuilt(builder: CandidateCardBuilder, seen: dict[str, Any]) -> None:
    for key in ("candidacy_id", "second_candidacy_id", "other_district_candidacy_id"):
        candidacy_id = seen.get(key)
        if candidacy_id:
            builder.rebuild_candidacy(candidacy_id)


@given("the incumbent also has a candidacy in another election")
def _candidacy_in_another_election(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    seen["other_election_candidacy_id"] = _candidacy(
        zerodb,
        person_id=PERSON_ID,
        election_id=OTHER_ELECTION_ID,
        person_name="TEST72, ALPHA",
    )


@given("the incumbent has a current position on a stance")
def _a_current_position(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _position(zerodb, person_id=PERSON_ID, s_value=0.5)
    seen["position_s_value"] = 0.5


@given("the incumbent has a superseded position and a current one on the same stance")
def _superseded_and_current_position(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _position(zerodb, person_id=PERSON_ID, s_value=-0.25)
    _position(zerodb, person_id=PERSON_ID, s_value=0.75)
    seen["position_s_value"] = 0.75


@given("the incumbent has a current verdict on a promise")
def _a_current_verdict(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    seen["promises"] += 1
    _verdict(
        zerodb,
        person_id=PERSON_ID,
        candidacy_id=seen["candidacy_id"],
        value="kept",
        promise_index=seen["promises"],
    )
    seen["verdict_value"] = "kept"


_VERDICT_LIST = (
    r"the incumbent has current verdicts of (?P<values>[a-z_, ]+?)(?: and (?P<last>[a-z_]+))?$"
)


@given(parsers.re(_VERDICT_LIST))
def _several_current_verdicts(
    zerodb: ZeroDBClient, seen: dict[str, Any], values: str, last: str | None
) -> None:
    """Parse "kept, kept, partial and broken" into four verdict rows.

    A regex rather than four separate steps, so the feature file reads as
    English and a new combination needs no new step definition.
    """
    listed = [value.strip() for value in values.split(",") if value.strip()]
    if last:
        listed.append(last.strip())
    for value in listed:
        seen["promises"] += 1
        _verdict(
            zerodb,
            person_id=PERSON_ID,
            candidacy_id=seen["candidacy_id"],
            value=value,
            promise_index=seen["promises"],
        )
    seen["verdict_values"] = listed


@given("the incumbent has a superseded verdict and a current one on the same promise")
def _superseded_and_current_verdict(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    seen["promises"] += 1
    index = seen["promises"]
    _verdict(
        zerodb,
        person_id=PERSON_ID,
        candidacy_id=seen["candidacy_id"],
        value="broken",
        promise_index=index,
    )
    # A second `append_version` for the SAME promise: the repository moves
    # `is_current` onto the new row and clears it on the old one, which is the
    # append-only behaviour datamodel.md rule 6 specifies.
    Repository(client=zerodb, model=Verdict).append_version(
        {
            "promise_id": f"prm:test72{index:04d}",
            "person_id": PERSON_ID,
            "verdict": "kept",
            "confidence": 0.9,
            "methodology_version": METHODOLOGY_VERSION,
            "qa_status": "approved",
            "created_at": "2026-10-19T12:00:00Z",
        }
    )
    seen["verdict_value"] = "kept"


@given("the card for that candidacy has been rebuilt from a positions recompute")
def _rebuilt_from_positions(builder: CandidateCardBuilder) -> None:
    builder.rebuild_from_positions(PERSON_ID, methodology_version=METHODOLOGY_VERSION)


@given("the card for that candidacy has been rebuilt from a verdict publication")
def _rebuilt_from_verdicts(builder: CandidateCardBuilder) -> None:
    builder.rebuild_from_verdicts(PERSON_ID)


@given(parsers.parse('a "{topic}" event was emitted for the incumbent'))
def _event_for_incumbent(zerodb: ZeroDBClient, topic: str) -> None:
    _emit(zerodb, topic, PERSON_ID)


@given(parsers.parse('a "{topic}" event was emitted for a person with no candidacy'))
def _event_for_person_without_candidacy(zerodb: ZeroDBClient, topic: str) -> None:
    _person(zerodb, NO_CANDIDACY_PERSON_ID, name="TEST72, Delta", photo=None)
    _emit(zerodb, topic, NO_CANDIDACY_PERSON_ID)


@given(parsers.parse('a "{topic}" event was emitted for both'))
def _event_for_both(zerodb: ZeroDBClient, topic: str) -> None:
    _emit(zerodb, topic, PERSON_ID)
    _emit(zerodb, topic, MIRROR_PERSON_ID)


def _emit(zerodb: ZeroDBClient, topic: str, person_id: str) -> None:
    """Emit one rebuild event, through the module's own emitter where there is
    one.

    `verdict.published` goes through `emit_verdict_published` so the payload
    this suite drives is the same spelling production will use — the point of
    defining the emitter beside the consumer. `positions.recomputed` is
    emitted directly here rather than by running story 6.2's scorer: this
    story's unit under test is the consumer, and driving the real scorer would
    make these scenarios depend on 6.2's scoping gap (live roll calls carry no
    `topic_ids`). The live script exercises the real producer end to end.
    """
    if topic == VERDICT_PUBLISHED_TOPIC:
        emit_verdict_published(
            zerodb,
            verdict_id=f"{person_id}:synthetic:v1",
            person_id=person_id,
            verdict="kept",
        )
        return
    zerodb.create_event(
        topic=topic,
        payload={"person_id": person_id, "methodology_version": METHODOLOGY_VERSION},
    )


@given("a second synthetic incumbent of another party whose record mirrors the first")
def _mirror_incumbent(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _person(zerodb, MIRROR_PERSON_ID, name="TEST72, Gamma", photo=None)
    seen["mirror_candidacy_id"] = _candidacy(
        zerodb,
        person_id=MIRROR_PERSON_ID,
        person_name="TEST72, GAMMA",
        # A different party, deliberately: prd.md's "one scoring codebase for
        # all candidates; no party-specific rules" is what this scenario pins,
        # and it is only a real test if the two differ.
        party="OTH",
    )


@given("both have current verdicts that mirror each other")
def _mirrored_verdicts(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """Alpha keeps 3 and breaks 1; Gamma breaks 3 and keeps 1.

    Mirrored about the midpoint: Alpha's gap is 0.25 and Gamma's is 0.75, so
    the two sum to 1.0 — which is what "mirrored about the midpoint" means for
    a measure on [0, 1], and what `say_do_gap_for`'s symmetry claim asserts.
    """
    for value in ("kept", "kept", "kept", "broken"):
        seen["promises"] += 1
        _verdict(
            zerodb,
            person_id=PERSON_ID,
            candidacy_id=seen["candidacy_id"],
            value=value,
            promise_index=seen["promises"],
        )
    for value in ("broken", "broken", "broken", "kept"):
        seen["promises"] += 1
        _verdict(
            zerodb,
            person_id=MIRROR_PERSON_ID,
            candidacy_id=seen["mirror_candidacy_id"],
            value=value,
            promise_index=seen["promises"],
        )


# --- When -----------------------------------------------------------------


@when("the card for that candidacy is rebuilt")
def _rebuild_card(builder: CandidateCardBuilder, seen: dict[str, Any]) -> None:
    seen["result"] = builder.rebuild_candidacy(seen["candidacy_id"])


@when("the card for an unknown candidacy is rebuilt")
def _rebuild_unknown_card(builder: CandidateCardBuilder, seen: dict[str, Any]) -> None:
    seen["result"] = builder.rebuild_candidacy(f"{ELECTION_ID}:{DISTRICT_ID}:person:TEST72-NOBODY")


@when("the ballot reads that candidate's card")
def _read_card(
    reader: CandidateCardReader, fake: FakeZeroDBTransport, seen: dict[str, Any]
) -> None:
    # Cleared immediately before the read, so what is counted is the READ and
    # not the rebuild that set it up.
    fake.calls.clear()
    seen["card"] = reader.card_for(seen["candidacy_id"])
    seen["read_calls"] = list(fake.calls)
    seen["read_tables"] = _tables_read(fake)


@when("the ballot reads the cards for that election and district")
def _read_race_cards(reader: CandidateCardReader, seen: dict[str, Any]) -> None:
    seen["cards"] = reader.cards_for_race(election_id=ELECTION_ID, district_id=DISTRICT_ID)


@when("the card rebuild consumer runs")
def _run_consumer(consumer: CandidateCardRebuildConsumer, seen: dict[str, Any]) -> None:
    seen["result"] = consumer.run()


@when("the card rebuild consumer runs again")
def _run_consumer_again(consumer: CandidateCardRebuildConsumer, seen: dict[str, Any]) -> None:
    seen["result"] = consumer.run()


# --- Then: the row and the read ------------------------------------------


@then(parsers.parse("candidate_cards holds {count:d} row"))
@then(parsers.parse("candidate_cards holds {count:d} rows"))
def _card_row_count(zerodb: ZeroDBClient, count: int) -> None:
    assert len(_cards(zerodb).query(limit=100)) == count


@then("the card's id is the candidacy id")
def _card_id_is_candidacy_id(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.id == seen["candidacy_id"]
    # And the field, not only the id: `references` resolves fields, so this is
    # what makes "a card names a real candidacy" checkable at write time.
    assert card.candidacy_id == seen["candidacy_id"]


@then(parsers.parse("exactly {count:d} table was read"))
def _one_read(seen: dict[str, Any], count: int) -> None:
    """One logical read of one table.

    Counted as "how many tables were touched", not "how many HTTP requests",
    and the difference is the live-ZeroDB filter bug: `get_row` issues a
    `/query` POST and then, because the server-side filter matches nothing,
    pages `/rows` client-side. See `app.ballot.cards`'s module docstring —
    the claim story 7.2 makes is that the read touches ONE table and
    aggregates nothing, and asserting a request count would pin the
    workaround rather than the property.
    """
    assert len(seen["read_tables"]) == count, (
        f"expected {count} table(s) read, got {sorted(seen['read_tables'])}"
    )
    assert seen["read_tables"] == {"candidate_cards"}


@then(parsers.parse("no row was read from {table}"))
def _no_read_from(seen: dict[str, Any], table: str) -> None:
    assert table not in seen["read_tables"], (
        f"reading a card touched {table}; a card read must aggregate nothing "
        f"(tables read: {sorted(seen['read_tables'])})"
    )


# --- Then: identity -------------------------------------------------------


@then("the card names the candidate")
def _card_names_candidate(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    # The name as FILED for this race, from `candidacies.person_name` — not
    # `people.canonical_name`, which is "TEST72, Alpha". See
    # `CandidateCardBuilder._merge`.
    assert card.person_name == "TEST72, ALPHA"


@then("the card names their party")
def _card_names_party(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.party == "IND"


@then("the card names their district by its readable name")
def _card_names_district(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.district == DISTRICT_NAME
    # And the id alongside it, which is what makes datamodel.md's documented
    # "By (election, district)" query possible — see `CandidateCard`.
    assert card.district_id == DISTRICT_ID
    assert card.election_id == ELECTION_ID


@then("the card carries the person's photo url")
def _card_carries_photo(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.photo_url == "https://example.invalid/alpha.jpg"


@then(parsers.parse("{count:d} cards are returned"))
def _cards_returned(seen: dict[str, Any], count: int) -> None:
    assert len(seen["cards"]) == count


@then("the card from the other district is not among them")
def _other_district_absent(seen: dict[str, Any]) -> None:
    returned = {card.candidacy_id for card in seen["cards"]}
    assert seen["other_district_candidacy_id"] not in returned
    assert all(card.district_id == DISTRICT_ID for card in seen["cards"])


@then("the rebuild reports that the candidacy was not found")
def _rebuild_reports_not_found(seen: dict[str, Any]) -> None:
    result = seen["result"]
    assert result.cards_rebuilt == 0
    assert len(result.skipped) == 1


# --- Then: the rebuild result --------------------------------------------


@then(parsers.parse("the rebuild reports {count:d} card rebuilt"))
@then(parsers.parse("the rebuild reports {count:d} cards rebuilt"))
def _rebuild_count(seen: dict[str, Any], count: int) -> None:
    assert seen["result"].cards_rebuilt == count


# --- Then: positions ------------------------------------------------------


@then("the card summarises that position")
def _card_summarises_position(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.positions_summary is not None
    assert len(card.positions_summary) == 1
    entry = card.positions_summary[0]
    assert entry.stance_id == STANCE_ID
    assert entry.s_value == pytest.approx(seen["position_s_value"])


@then("the card still summarises that position")
def _card_still_summarises_position(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _card_summarises_position(zerodb, seen)


@then("the card names the methodology version from the event")
def _card_names_methodology(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.methodology_version == METHODOLOGY_VERSION


@then("the card's verdict counts are absent")
def _verdict_counts_absent(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    # `None`, emphatically not an all-zero VerdictCounts: `kept: 0` would read
    # as "kept none of their promises". See `CandidateCard`'s docstring.
    assert card.verdict_counts is None


@then("the card's say-do gap is absent")
def _say_do_gap_absent(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.say_do_gap is None


@then(parsers.parse("the card summarises {count:d} position"))
@then(parsers.parse("the card summarises {count:d} positions"))
def _card_summarises_n_positions(zerodb: ZeroDBClient, seen: dict[str, Any], count: int) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.positions_summary is not None
    assert len(card.positions_summary) == count


@then("the summarised position is the current one")
def _summarised_position_is_current(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.positions_summary is not None
    assert card.positions_summary[0].s_value == pytest.approx(seen["position_s_value"])


# --- Then: verdicts -------------------------------------------------------


@then("the card counts that verdict")
def _card_counts_verdict(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.verdict_counts is not None
    assert card.verdict_counts.total == 1
    assert getattr(card.verdict_counts, seen["verdict_value"]) == 1


@then("the card still counts that verdict")
def _card_still_counts_verdict(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _card_counts_verdict(zerodb, seen)


@then(parsers.re(r"the card counts (?P<spec>\d+ [a-z_]+(?:, \d+ [a-z_]+)*(?: and \d+ [a-z_]+)?)$"))
def _card_counts_spec(zerodb: ZeroDBClient, seen: dict[str, Any], spec: str) -> None:
    """Assert "2 kept, 1 partial and 1 broken" against the stored counts."""
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.verdict_counts is not None
    expected: dict[str, int] = {}
    for part in spec.replace(" and ", ", ").split(", "):
        count, value = part.split(" ", 1)
        expected[value] = int(count)
    stored = card.verdict_counts.model_dump()
    for value, count in expected.items():
        assert stored[value] == count, f"expected {count} {value}, got {stored[value]}"
    # And nothing else was counted: a card that also claimed two `absent`
    # verdicts nobody wrote would pass a per-value check.
    assert card.verdict_counts.total == sum(expected.values())


@then(parsers.parse("the card counts {count:d} verdict in total"))
def _card_counts_total(zerodb: ZeroDBClient, seen: dict[str, Any], count: int) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.verdict_counts is not None
    assert card.verdict_counts.total == count


@then("the counted verdict is the current one")
def _counted_verdict_is_current(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.verdict_counts is not None
    assert getattr(card.verdict_counts, seen["verdict_value"]) == 1


@then("the card reports a say-do gap between 0 and 1")
def _gap_in_range(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.say_do_gap is not None
    assert 0.0 < card.say_do_gap < 1.0


@then(parsers.parse("the card reports a say-do gap of {expected:f}"))
def _gap_equals(zerodb: ZeroDBClient, seen: dict[str, Any], expected: float) -> None:
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    assert card.say_do_gap is not None
    assert card.say_do_gap == pytest.approx(expected)


# --- Then: neutrality -----------------------------------------------------


@then("their say-do gaps are exact opposites about the midpoint")
def _gaps_mirrored(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    cards = _cards(zerodb)
    alpha = cards.get(seen["candidacy_id"])
    gamma = cards.get(seen["mirror_candidacy_id"])
    assert alpha is not None and gamma is not None
    assert alpha.say_do_gap is not None and gamma.say_do_gap is not None
    # Mirrored about 0.5 on a [0, 1] measure means the two sum to 1.0.
    assert alpha.say_do_gap + gamma.say_do_gap == pytest.approx(1.0)
    assert alpha.say_do_gap != gamma.say_do_gap


@then("their verdict counts are mirror images")
def _counts_mirrored(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    cards = _cards(zerodb)
    alpha = cards.get(seen["candidacy_id"])
    gamma = cards.get(seen["mirror_candidacy_id"])
    assert alpha is not None and gamma is not None
    assert alpha.verdict_counts is not None and gamma.verdict_counts is not None
    assert alpha.verdict_counts.kept == gamma.verdict_counts.broken
    assert alpha.verdict_counts.broken == gamma.verdict_counts.kept
    assert alpha.verdict_counts.total == gamma.verdict_counts.total


@then("the card model forbids any field that is not in datamodel.md's spec")
def _model_forbids_extra_fields() -> None:
    """`extra="forbid"` on the row model, checked rather than assumed.

    The same structural guarantee `app.summary_card.models` makes for its
    response: a later edit cannot attach a `profile_id`, `user_id` or
    `interests` field to a card even by passing it as a kwarg.
    """
    assert CandidateCard.model_config.get("extra") == "forbid"
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic ValidationError
        CandidateCard.model_validate(
            {
                "id": CANDIDACY_ID,
                "candidacy_id": CANDIDACY_ID,
                "election_id": ELECTION_ID,
                "district_id": DISTRICT_ID,
                "person_id": PERSON_ID,
                "person_name": "TEST72, ALPHA",
                "district": DISTRICT_NAME,
                "updated_at": "2026-10-19T12:00:00Z",
                "voter_profile_id": "vp:should-never-be-accepted",
            }
        )


@then("no stored card field holds a voter interest, profile or alignment score")
def _no_voter_field(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """No field here is named, or resembles, a voter's data.

    Checked against the stored row's own keys rather than against the model's
    declaration, so a field added by a raw write would be caught too. The
    forbidden substrings are the ones datamodel.md's privacy rule names:
    voter profiles, interests and per-user alignment ("per-user alignment
    scores are computed per request and never stored").
    """
    card = _cards(zerodb).get(seen["candidacy_id"])
    assert card is not None
    forbidden = ("profile", "interest", "voter", "user", "alignment", "dealbreaker", "tier")
    offending = [key for key in card.model_dump() if any(word in key.lower() for word in forbidden)]
    assert not offending, f"candidate_cards row carries voter-shaped field(s): {offending}"
