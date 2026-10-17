"""The evidence-graph writer (story 13.1).

Three acceptance criteria, each with its own group of tests below:

  1. **The ontology is registered.** Story 1.3's `_bootstrap_ontology` already
     registers it; this story's job is to pin that what is registered is
     EXACTLY datamodel.md's predicate table, and that an edge whose predicate
     or endpoint types fall outside it is refused before any write.
  2. **`voted` edges exist only where evidence needs them.** datamodel.md:
     "`voted` edges are written only for roll calls that some promise or
     bill-stance edge touches. That keeps the graph to evidence, not all ~600k
     votes per Congress." So the writer takes the set of roll calls evidence
     reaches and writes nothing for the rest.
  3. **Canonical names are NoSQL ids.** Every entity the writer creates is
     named by the id string of the row it stands for, so a traversal resolves
     straight back to a citable record. `test_every_entity_name_is_a_real_row_id`
     is the test that makes this mechanical rather than aspirational.
"""

from __future__ import annotations

import pytest

from app.bootstrap import ONTOLOGY_ENTITY_TYPES, ONTOLOGY_PREDICATES
from app.evidence_graph import (
    DOCUMENTED_PREDICATE_TABLE,
    EvidenceGraphWriter,
    UnknownPredicateError,
    WrongEndpointTypeError,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

PERSON_ID = "person:B000944"
CANDIDACY_ID = (
    "us-2026-11-03-general:ocd-division/country:us/state:oh/cd:11:person:B000944"
)
PROMISE_ID = "prm:11111111-1111-4111-8111-111111111111"
STANCE_ID = "stance:housing-supply:for"
TOPIC_ID = "topic:housing"
ROLL_CALL_ID = "us-house:119-2:452"
UNTOUCHED_ROLL_CALL_ID = "us-house:119-2:453"
BILL_ID = "us-bill:119:hr:1234"
# datamodel.md: `verdicts` id pattern is `{promise_id}:v{n}`.
VERDICT_ID = f"{PROMISE_ID}:v1"


@pytest.fixture
def transport() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(transport: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        project_id=FakeZeroDBTransport.PROJECT_ID, transport=transport
    )


@pytest.fixture
def writer(client: ZeroDBClient) -> EvidenceGraphWriter:
    return EvidenceGraphWriter(client)


# --- criterion 1: the ontology is the one datamodel.md documents ----------


def test_the_bootstrap_ontology_matches_the_documented_predicate_table() -> None:
    """The acceptance criterion "the ontology is registered" is only meaningful
    if what is registered is datamodel.md's own table.

    `DOCUMENTED_PREDICATE_TABLE` is that table transcribed independently of
    `app.bootstrap.ONTOLOGY_PREDICATES`, so this comparison catches a drift in
    either direction — a predicate renamed in the code, a predicate added to
    the document and not the code, or an endpoint type quietly swapped.
    """
    assert ONTOLOGY_PREDICATES == DOCUMENTED_PREDICATE_TABLE
    # Every type either side of a documented predicate must be a registered
    # entity type, or the predicate could never be written.
    needed = {spec["source"] for spec in DOCUMENTED_PREDICATE_TABLE.values()} | {
        spec["target"] for spec in DOCUMENTED_PREDICATE_TABLE.values()
    }
    assert needed == set(ONTOLOGY_ENTITY_TYPES)


def test_the_documented_table_has_the_eleven_predicates_datamodel_md_lists() -> None:
    """Ten rows in datamodel.md's table; `advances / opposes` is one row and
    two predicates, so eleven registered predicates is the correct count.
    """
    assert len(DOCUMENTED_PREDICATE_TABLE) == 11
    assert {"advances", "opposes"} <= set(DOCUMENTED_PREDICATE_TABLE)


def test_the_writer_knows_exactly_datamodel_mds_predicates() -> None:
    """The writer validates against ONE ontology definition, not a second copy.

    `app.bootstrap.ONTOLOGY_PREDICATES` is the registered ontology; the writer
    must validate against that same constant rather than a duplicate that can
    drift from it.
    """
    assert EvidenceGraphWriter.predicates() is ONTOLOGY_PREDICATES
    assert EvidenceGraphWriter.entity_types() is ONTOLOGY_ENTITY_TYPES


def test_the_registered_ontology_matches_what_the_writer_validates_against(
    writer: EvidenceGraphWriter, client: ZeroDBClient
) -> None:
    client.graph_upsert_ontology(
        entity_types=list(ONTOLOGY_ENTITY_TYPES), predicates=ONTOLOGY_PREDICATES
    )
    report = writer.verify_ontology()
    assert report.ok, report.summary()
    assert report.missing_entity_types == []
    assert report.missing_predicates == []
    assert report.unexpected_predicates == []


def test_verify_ontology_names_a_missing_predicate(
    writer: EvidenceGraphWriter, client: ZeroDBClient
) -> None:
    narrowed = {k: v for k, v in ONTOLOGY_PREDICATES.items() if k != "voted"}
    client.graph_upsert_ontology(entity_types=list(ONTOLOGY_ENTITY_TYPES), predicates=narrowed)
    report = writer.verify_ontology()
    assert not report.ok
    assert report.missing_predicates == ["voted"]


def test_verify_ontology_names_a_predicate_outside_the_documented_table(
    writer: EvidenceGraphWriter, client: ZeroDBClient
) -> None:
    widened = dict(ONTOLOGY_PREDICATES)
    widened["endorses"] = {"source": "person", "target": "candidacy"}
    client.graph_upsert_ontology(entity_types=list(ONTOLOGY_ENTITY_TYPES), predicates=widened)
    report = writer.verify_ontology()
    assert not report.ok
    assert report.unexpected_predicates == ["endorses"]


def test_verify_ontology_reports_an_unregistered_project(writer: EvidenceGraphWriter) -> None:
    report = writer.verify_ontology()
    assert not report.ok
    assert report.registered is False


def test_an_unknown_predicate_is_refused_before_any_write(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    with pytest.raises(UnknownPredicateError, match="endorses"):
        writer.write_edge(
            source=(PERSON_ID, "person"),
            target=(CANDIDACY_ID, "candidacy"),
            predicate="endorses",
        )
    assert transport.graph_entities == {}
    assert transport.graph_edges == set()
    assert transport.calls == []


def test_endpoints_of_the_wrong_type_are_refused_before_any_write(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    with pytest.raises(WrongEndpointTypeError, match="ran_in"):
        writer.write_edge(
            source=(CANDIDACY_ID, "candidacy"),
            target=(PERSON_ID, "person"),
            predicate="ran_in",
        )
    assert transport.graph_edges == set()
    assert transport.calls == []


def test_every_documented_predicate_can_actually_be_written(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    """A predicate the ontology registers but the writer cannot write is a
    silent hole in the evidence graph, so each of the eleven is exercised.
    """
    sample = {
        "person": PERSON_ID,
        "candidacy": CANDIDACY_ID,
        "promise": PROMISE_ID,
        "stance": STANCE_ID,
        "roll_call": ROLL_CALL_ID,
        "bill": BILL_ID,
        "verdict": VERDICT_ID,
        "topic": TOPIC_ID,
    }
    for predicate, spec in ONTOLOGY_PREDICATES.items():
        writer.write_edge(
            source=(sample[spec["source"]], spec["source"]),
            target=(sample[spec["target"]], spec["target"]),
            predicate=predicate,
        )
    written = {predicate for _, predicate, _ in transport.graph_edges}
    assert written == set(ONTOLOGY_PREDICATES)


# --- criterion 3: canonical names are NoSQL ids ---------------------------


def test_candidacy_evidence_writes_a_ran_in_edge(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_candidacy_evidence(
        person_id=PERSON_ID,
        candidacy_id=CANDIDACY_ID,
        party="D",
        is_incumbent=True,
    )
    assert (PERSON_ID, "ran_in", CANDIDACY_ID) in transport.graph_edges
    assert transport.graph_entities[PERSON_ID]["entity_type"] == "person"
    assert transport.graph_entities[CANDIDACY_ID]["entity_type"] == "candidacy"


def test_the_ran_in_edge_carries_party_and_incumbency(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    """datamodel.md's edge properties for `person -> ran_in -> candidacy`."""
    writer.write_candidacy_evidence(
        person_id=PERSON_ID, candidacy_id=CANDIDACY_ID, party="R", is_incumbent=False
    )
    edge_call = [
        call
        for call in transport.calls
        if call.path.endswith("/graph/edge") and (call.json or {}).get("predicate") == "ran_in"
    ][-1]
    properties = (edge_call.json or {})["properties"]
    assert properties["party"] == "R"
    assert properties["is_incumbent"] is False


def test_every_entity_name_is_a_real_row_id(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    """"Canonical names are NoSQL ids": the entity's name IS the row id, so a
    traversal resolves back to a citable record with no mapping table.
    """
    writer.write_candidacy_evidence(
        person_id=PERSON_ID, candidacy_id=CANDIDACY_ID, party="D", is_incumbent=True
    )
    assert set(transport.graph_entities) == {PERSON_ID, CANDIDACY_ID}
    for name, entity in transport.graph_entities.items():
        assert entity["canonical_name"] == name


def test_a_traversal_from_the_person_reaches_the_candidacy_by_its_row_id(
    writer: EvidenceGraphWriter, client: ZeroDBClient
) -> None:
    writer.write_candidacy_evidence(
        person_id=PERSON_ID, candidacy_id=CANDIDACY_ID, party="D", is_incumbent=True
    )
    traversal = client.graph_traverse(entity=PERSON_ID, max_hops=1)
    reached = {node.entity for node in traversal.nodes}
    assert CANDIDACY_ID in reached


def test_rewriting_the_same_candidacy_evidence_changes_nothing(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    for _ in range(2):
        writer.write_candidacy_evidence(
            person_id=PERSON_ID, candidacy_id=CANDIDACY_ID, party="D", is_incumbent=True
        )
    ran_in = [edge for edge in transport.graph_edges if edge[1] == "ran_in"]
    assert len(ran_in) == 1


def test_an_entity_name_that_is_not_a_row_id_shape_is_refused(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    """A human name instead of a row id would make the answer uncitable."""
    with pytest.raises(ValueError, match="row id"):
        writer.write_candidacy_evidence(
            person_id="Marcy Kaptur",
            candidacy_id=CANDIDACY_ID,
            party="D",
            is_incumbent=True,
        )
    assert transport.calls == []


# --- promise evidence -----------------------------------------------------


def test_promise_evidence_writes_made_promise_about_and_has_verdict(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_promise_evidence(
        candidacy_id=CANDIDACY_ID,
        promise_id=PROMISE_ID,
        source_id="src:abc",
        captured_at="2026-01-02T00:00:00Z",
        stance_id=STANCE_ID,
        stance_confidence=0.91,
        verdict_id=VERDICT_ID,
        methodology_version="m1",
        verdict_is_current=True,
    )
    assert (CANDIDACY_ID, "made_promise", PROMISE_ID) in transport.graph_edges
    assert (PROMISE_ID, "about", STANCE_ID) in transport.graph_edges
    assert (PROMISE_ID, "has_verdict", VERDICT_ID) in transport.graph_edges


def test_the_verdict_edge_carries_methodology_version_and_is_current(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_promise_evidence(
        candidacy_id=CANDIDACY_ID,
        promise_id=PROMISE_ID,
        source_id="src:abc",
        captured_at="2026-01-02T00:00:00Z",
        verdict_id=VERDICT_ID,
        methodology_version="m2",
        verdict_is_current=True,
    )
    edge = [
        call
        for call in transport.calls
        if (call.json or {}).get("predicate") == "has_verdict"
    ][-1]
    properties = (edge.json or {})["properties"]
    assert properties["methodology_version"] == "m2"
    assert properties["is_current"] is True


def test_the_made_promise_edge_carries_its_source_and_capture_time(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_promise_evidence(
        candidacy_id=CANDIDACY_ID,
        promise_id=PROMISE_ID,
        source_id="src:abc",
        captured_at="2026-01-02T00:00:00Z",
    )
    edge = [
        call
        for call in transport.calls
        if (call.json or {}).get("predicate") == "made_promise"
    ][-1]
    properties = (edge.json or {})["properties"]
    assert properties["source_id"] == "src:abc"
    assert properties["captured_at"] == "2026-01-02T00:00:00Z"


def test_promise_evidence_without_a_stance_or_verdict_writes_only_made_promise(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    """A promise extracted but not yet classified or judged is still evidence."""
    writer.write_promise_evidence(
        candidacy_id=CANDIDACY_ID,
        promise_id=PROMISE_ID,
        source_id="src:abc",
        captured_at="2026-01-02T00:00:00Z",
    )
    predicates = {predicate for _, predicate, _ in transport.graph_edges}
    assert predicates == {"made_promise"}


# --- criterion 2: voted edges only where evidence needs them -------------


def test_a_voted_edge_is_written_for_a_roll_call_a_promise_tests(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_promise_mapping_evidence(
        promise_id=PROMISE_ID,
        roll_call_id=ROLL_CALL_ID,
        bill_id=BILL_ID,
        effect="advances",
        confidence=0.88,
        qa_status="approved",
    )
    writer.write_vote_evidence(
        person_id=PERSON_ID, roll_call_id=ROLL_CALL_ID, position="Yea"
    )
    assert (PERSON_ID, "voted", ROLL_CALL_ID) in transport.graph_edges


def test_no_voted_edge_is_written_for_a_roll_call_no_evidence_touches(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    """The ~600k-votes-per-Congress guard. datamodel.md is explicit that this is
    the rule that keeps the graph to evidence rather than to the whole record.
    """
    result = writer.write_vote_evidence(
        person_id=PERSON_ID, roll_call_id=UNTOUCHED_ROLL_CALL_ID, position="Nay"
    )
    assert result.written is False
    assert result.reason == "no promise or bill-stance edge touches this roll call"
    assert transport.graph_edges == set()


def test_a_bill_stance_edge_also_makes_a_roll_call_evidence_bearing(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    """datamodel.md says "some promise OR bill-stance edge", so a bill scored
    against a stance is enough on its own — no promise required.
    """
    writer.write_bill_stance_evidence(
        bill_id=BILL_ID,
        stance_id=STANCE_ID,
        effect="advances",
        confidence=0.8,
        method="classifier-v0",
    )
    writer.write_roll_call_evidence(roll_call_id=ROLL_CALL_ID, bill_id=BILL_ID, vote_type="passage")
    result = writer.write_vote_evidence(
        person_id=PERSON_ID, roll_call_id=ROLL_CALL_ID, position="Yea"
    )
    assert result.written is True
    assert (PERSON_ID, "voted", ROLL_CALL_ID) in transport.graph_edges


def test_the_voted_edge_carries_the_position(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_promise_mapping_evidence(
        promise_id=PROMISE_ID, roll_call_id=ROLL_CALL_ID, effect="advances", confidence=0.9
    )
    writer.write_vote_evidence(person_id=PERSON_ID, roll_call_id=ROLL_CALL_ID, position="Yea")
    edge = [
        call for call in transport.calls if (call.json or {}).get("predicate") == "voted"
    ][-1]
    assert (edge.json or {})["properties"]["position"] == "Yea"


def test_a_vote_can_be_forced_when_the_caller_already_knows_it_is_evidence(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    """A verdict agent that has just written the mapping in another process
    knows the roll call bears evidence even though THIS writer has not seen it.
    Forcing is explicit and named, so the guard can never be bypassed silently.
    """
    result = writer.write_vote_evidence(
        person_id=PERSON_ID,
        roll_call_id=ROLL_CALL_ID,
        position="Yea",
        evidence_bearing=True,
    )
    assert result.written is True
    assert (PERSON_ID, "voted", ROLL_CALL_ID) in transport.graph_edges


def test_the_evidence_bearing_roll_call_set_is_readable(
    writer: EvidenceGraphWriter,
) -> None:
    writer.write_promise_mapping_evidence(
        promise_id=PROMISE_ID, roll_call_id=ROLL_CALL_ID, effect="advances", confidence=0.9
    )
    assert writer.evidence_bearing_roll_calls() == {ROLL_CALL_ID}


# --- stance/topic and bill edges ------------------------------------------


def test_stance_under_topic_is_written(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_stance_evidence(stance_id=STANCE_ID, topic_id=TOPIC_ID)
    assert (STANCE_ID, "under", TOPIC_ID) in transport.graph_edges


def test_a_bill_opposing_a_stance_uses_the_opposes_predicate(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_bill_stance_evidence(
        bill_id=BILL_ID, stance_id=STANCE_ID, effect="opposes", confidence=0.7, method="manual"
    )
    assert (BILL_ID, "opposes", STANCE_ID) in transport.graph_edges
    assert (BILL_ID, "advances", STANCE_ID) not in transport.graph_edges


def test_an_unknown_bill_stance_effect_is_refused(writer: EvidenceGraphWriter) -> None:
    with pytest.raises(ValueError, match="effect"):
        writer.write_bill_stance_evidence(
            bill_id=BILL_ID, stance_id=STANCE_ID, effect="sideways", confidence=0.7, method="manual"
        )


def test_sponsorship_is_written_with_its_role(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_sponsorship_evidence(person_id=PERSON_ID, bill_id=BILL_ID, role="sponsor")
    assert (PERSON_ID, "sponsored", BILL_ID) in transport.graph_edges


def test_a_roll_call_on_a_bill_carries_its_vote_type_and_bundling(
    writer: EvidenceGraphWriter, transport: FakeZeroDBTransport
) -> None:
    writer.write_roll_call_evidence(
        roll_call_id=ROLL_CALL_ID, bill_id=BILL_ID, vote_type="passage", is_bundled=True
    )
    edge = [call for call in transport.calls if (call.json or {}).get("predicate") == "on"][-1]
    properties = (edge.json or {})["properties"]
    assert properties["vote_type"] == "passage"
    assert properties["is_bundled"] is True


# --- the writer reports what it did ---------------------------------------


def test_the_writer_reports_entities_and_edges_written(writer: EvidenceGraphWriter) -> None:
    report = writer.write_candidacy_evidence(
        person_id=PERSON_ID, candidacy_id=CANDIDACY_ID, party="D", is_incumbent=True
    )
    assert report.entities_written == 2
    assert report.edges_written == 1
    assert "ran_in" in report.summary()
