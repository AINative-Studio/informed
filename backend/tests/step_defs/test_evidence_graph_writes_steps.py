"""Step definitions for tests/features/evidence_graph_writes.feature (story 13.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import ONTOLOGY_ENTITY_TYPES, ONTOLOGY_PREDICATES
from app.evidence_graph import (
    DOCUMENTED_PREDICATE_TABLE,
    EvidenceGraphWriter,
    UnknownPredicateError,
    WrongEndpointTypeError,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/evidence_graph_writes.feature")

PERSON_ID = "person:B000944"
CANDIDACY_ID = "us-2026-11-03-general:ocd-division/country:us/state:oh/cd:11:person:B000944"
PROMISE_ID = "prm:11111111-1111-4111-8111-111111111111"
STANCE_ID = "stance:housing-supply:for"
# datamodel.md: `verdicts` id pattern is `{promise_id}:v{n}`.
VERDICT_ID = f"{PROMISE_ID}:v1"
BILL_ID = "us-bill:119:hr:1234"
TESTED_ROLL_CALL = "us-house:119-2:452"
UNTOUCHED_ROLL_CALL = "us-house:119-2:453"


@dataclass
class GraphWorld:
    transport: FakeZeroDBTransport
    client: ZeroDBClient
    writer: EvidenceGraphWriter
    ontology: Any = None
    rows: dict[str, dict[str, str]] = field(default_factory=dict)
    error: Exception | None = None
    traversal: Any = None
    vote_results: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def world() -> GraphWorld:
    transport = FakeZeroDBTransport()
    client = ZeroDBClient(
        project_id=FakeZeroDBTransport.PROJECT_ID, transport=transport
    )
    return GraphWorld(transport=transport, client=client, writer=EvidenceGraphWriter(client))


@given("a bootstrapped ZeroDB project with the evidence-graph ontology registered")
def ontology_registered(world: GraphWorld) -> None:
    world.client.graph_upsert_ontology(
        entity_types=list(ONTOLOGY_ENTITY_TYPES), predicates=ONTOLOGY_PREDICATES
    )


@given("a person row and a candidacy row for that person")
def person_and_candidacy_rows(world: GraphWorld) -> None:
    # The NoSQL rows the graph entities will stand for. Kept in the world so the
    # "every entity names a row that can be read back" step has something real
    # to resolve against.
    world.rows[PERSON_ID] = {"table": "people", "id": PERSON_ID}
    world.rows[CANDIDACY_ID] = {"table": "candidacies", "id": CANDIDACY_ID}


@given("a candidacy with one promise, a stance and a published verdict")
def candidacy_promise_stance_verdict(world: GraphWorld) -> None:
    world.rows[CANDIDACY_ID] = {"table": "candidacies", "id": CANDIDACY_ID}
    world.rows[PROMISE_ID] = {"table": "promises", "id": PROMISE_ID}
    world.rows[STANCE_ID] = {"table": "stances", "id": STANCE_ID}
    world.rows[VERDICT_ID] = {"table": "verdicts", "id": VERDICT_ID}


@given("a roll call that one promise is tested by")
def tested_roll_call(world: GraphWorld) -> None:
    world.writer.write_promise_mapping_evidence(
        promise_id=PROMISE_ID,
        roll_call_id=TESTED_ROLL_CALL,
        bill_id=BILL_ID,
        effect="advances",
        confidence=0.9,
        qa_status="approved",
    )


@given("a roll call that no promise and no bill-stance edge touches")
def untouched_roll_call(world: GraphWorld) -> None:
    assert UNTOUCHED_ROLL_CALL not in world.writer.evidence_bearing_roll_calls()


@when("the registered ontology is read back")
def read_the_ontology(world: GraphWorld) -> None:
    world.ontology = world.client.graph_get_ontology()


@when("the evidence graph writer writes the candidacy's evidence")
@when("the evidence graph writer writes the candidacy's evidence again")
def write_candidacy_evidence(world: GraphWorld) -> None:
    world.writer.write_candidacy_evidence(
        person_id=PERSON_ID, candidacy_id=CANDIDACY_ID, party="D", is_incumbent=True
    )


@when("the graph is traversed from the person entity")
def traverse_from_person(world: GraphWorld) -> None:
    world.traversal = world.client.graph_traverse(entity=PERSON_ID, max_hops=2)


@when("the evidence graph writer writes the promise's evidence")
def write_promise_evidence(world: GraphWorld) -> None:
    world.writer.write_promise_evidence(
        candidacy_id=CANDIDACY_ID,
        promise_id=PROMISE_ID,
        source_id="src:abc123",
        captured_at="2026-01-02T00:00:00Z",
        stance_id=STANCE_ID,
        stance_confidence=0.9,
        verdict_id=VERDICT_ID,
        methodology_version="m1",
        verdict_is_current=True,
    )


@when("the evidence graph writer writes the member's vote evidence")
def write_vote_evidence(world: GraphWorld) -> None:
    for roll_call in (TESTED_ROLL_CALL, UNTOUCHED_ROLL_CALL):
        world.vote_results[roll_call] = world.writer.write_vote_evidence(
            person_id=PERSON_ID, roll_call_id=roll_call, position="Yea"
        )


@when(parsers.parse('the evidence graph writer is asked to write an edge with the predicate "{predicate}"'))
def write_unknown_predicate(world: GraphWorld, predicate: str) -> None:
    world.transport.calls.clear()
    try:
        world.writer.write_edge(
            source=(PERSON_ID, "person"),
            target=(CANDIDACY_ID, "candidacy"),
            predicate=predicate,
        )
    except UnknownPredicateError as exc:
        world.error = exc


@when(parsers.parse('the evidence graph writer is asked to write a "{predicate}" edge from the candidacy to the person'))
def write_reversed_edge(world: GraphWorld, predicate: str) -> None:
    world.transport.calls.clear()
    try:
        world.writer.write_edge(
            source=(CANDIDACY_ID, "candidacy"),
            target=(PERSON_ID, "person"),
            predicate=predicate,
        )
    except WrongEndpointTypeError as exc:
        world.error = exc


@then("every entity type in datamodel.md's predicate table is registered")
def every_entity_type_registered(world: GraphWorld) -> None:
    assert world.ontology is not None
    documented = {spec["source"] for spec in DOCUMENTED_PREDICATE_TABLE.values()} | {
        spec["target"] for spec in DOCUMENTED_PREDICATE_TABLE.values()
    }
    assert documented <= set(world.ontology.entity_types)


@then(
    "every predicate in datamodel.md's predicate table is registered with its "
    "documented source and target types"
)
def every_predicate_registered(world: GraphWorld) -> None:
    assert world.ontology is not None
    for predicate, spec in DOCUMENTED_PREDICATE_TABLE.items():
        registered = world.ontology.predicates.get(predicate)
        assert registered is not None, predicate
        assert registered["source"] == spec["source"], predicate
        assert registered["target"] == spec["target"], predicate


@then("no predicate outside datamodel.md's predicate table is registered")
def no_extra_predicates(world: GraphWorld) -> None:
    assert world.ontology is not None
    assert set(world.ontology.predicates) == set(DOCUMENTED_PREDICATE_TABLE)


@then(parsers.parse('a "{entity_type}" entity exists whose canonical name is the person\'s NoSQL id'))
def person_entity_exists(world: GraphWorld, entity_type: str) -> None:
    entity = world.transport.graph_entities[PERSON_ID]
    assert entity["entity_type"] == entity_type
    assert entity["canonical_name"] == PERSON_ID


@then(parsers.parse('a "{entity_type}" entity exists whose canonical name is the candidacy\'s NoSQL id'))
def candidacy_entity_exists(world: GraphWorld, entity_type: str) -> None:
    entity = world.transport.graph_entities[CANDIDACY_ID]
    assert entity["entity_type"] == entity_type
    assert entity["canonical_name"] == CANDIDACY_ID


@then(parsers.parse('a "{predicate}" edge connects the person to the candidacy'))
def ran_in_edge_exists(world: GraphWorld, predicate: str) -> None:
    assert (PERSON_ID, predicate, CANDIDACY_ID) in world.transport.graph_edges


@then("the edge carries the party and incumbency from the candidacy row")
def edge_carries_party(world: GraphWorld) -> None:
    edge = [
        call
        for call in world.transport.calls
        if (call.json or {}).get("predicate") == "ran_in"
    ][-1]
    properties = (edge.json or {})["properties"]
    assert properties["party"] == "D"
    assert properties["is_incumbent"] is True


@then("every entity reached names a row id that can be read back from its NoSQL table")
def every_entity_resolves(world: GraphWorld) -> None:
    assert world.traversal is not None
    reached = {node.entity for node in world.traversal.nodes}
    assert reached, "the traversal reached nothing"
    for name in reached:
        assert name in world.rows, f"{name} is not a NoSQL row id"


@then(parsers.parse('the graph holds exactly one "{predicate}" edge for that pair'))
def exactly_one_edge(world: GraphWorld, predicate: str) -> None:
    matches = [
        edge
        for edge in world.transport.graph_edges
        if edge == (PERSON_ID, predicate, CANDIDACY_ID)
    ]
    assert len(matches) == 1


@then(parsers.parse('a "{predicate}" edge connects the candidacy to the promise'))
def made_promise_edge(world: GraphWorld, predicate: str) -> None:
    assert (CANDIDACY_ID, predicate, PROMISE_ID) in world.transport.graph_edges


@then(parsers.parse('an "{predicate}" edge connects the promise to the stance'))
def about_edge(world: GraphWorld, predicate: str) -> None:
    assert (PROMISE_ID, predicate, STANCE_ID) in world.transport.graph_edges


@then(parsers.parse('a "{predicate}" edge connects the promise to the verdict'))
def has_verdict_edge(world: GraphWorld, predicate: str) -> None:
    assert (PROMISE_ID, predicate, VERDICT_ID) in world.transport.graph_edges


@then("the verdict edge carries its methodology version and is_current flag")
def verdict_edge_properties(world: GraphWorld) -> None:
    edge = [
        call
        for call in world.transport.calls
        if (call.json or {}).get("predicate") == "has_verdict"
    ][-1]
    properties = (edge.json or {})["properties"]
    assert properties["methodology_version"] == "m1"
    assert properties["is_current"] is True


@then(parsers.parse('a "{predicate}" edge exists for the roll call a promise tests'))
def voted_edge_for_tested(world: GraphWorld, predicate: str) -> None:
    assert (PERSON_ID, predicate, TESTED_ROLL_CALL) in world.transport.graph_edges
    assert world.vote_results[TESTED_ROLL_CALL].written is True


@then(parsers.parse('no "{predicate}" edge exists for the untouched roll call'))
def no_voted_edge_for_untouched(world: GraphWorld, predicate: str) -> None:
    assert (PERSON_ID, predicate, UNTOUCHED_ROLL_CALL) not in world.transport.graph_edges
    assert world.vote_results[UNTOUCHED_ROLL_CALL].written is False


@then("the write is refused with an unknown-predicate error")
def refused_unknown_predicate(world: GraphWorld) -> None:
    assert isinstance(world.error, UnknownPredicateError)


@then("the write is refused with an endpoint-type error")
def refused_endpoint_type(world: GraphWorld) -> None:
    assert isinstance(world.error, WrongEndpointTypeError)


@then("nothing was written to the graph")
def nothing_written_to_the_graph(world: GraphWorld) -> None:
    assert world.transport.calls == []
