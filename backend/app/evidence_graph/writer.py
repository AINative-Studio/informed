"""The one typed door for writing the evidence graph (story 13.1).

===============================================================================
WHAT THIS MODULE IS FOR
===============================================================================

backlog.md 13.1: "As the system, I want ingest and verdict agents to write the
evidence graph per the ontology, so that the agent can traverse evidence."
Acceptance: "The ontology is registered; `voted` edges exist only where
evidence needs them; canonical names are NoSQL ids."

`ZeroDBClient.graph_upsert_entity` / `graph_create_edge` are the transport.
They will write anything: an edge with a predicate the ontology never
registered, an edge whose endpoints are the wrong way round, an entity named
"Marcy Kaptur" instead of `person:B000944`. Each of those is a silent, durable
hole in the evidence graph — the kind that surfaces months later as an answer
the agent cannot cite. So no ingest or verdict module calls those two methods
directly; they all go through `EvidenceGraphWriter`, which refuses all three
before spending a request.

===============================================================================
THE THREE ACCEPTANCE CRITERIA, AND HOW EACH IS MET
===============================================================================

**1. "The ontology is registered."** It already is: story 1.3's
`app.bootstrap.Bootstrapper._bootstrap_ontology` registers
`ONTOLOGY_ENTITY_TYPES` + `ONTOLOGY_PREDICATES` on every bootstrap run. This
story's contribution is twofold. First, `DOCUMENTED_PREDICATE_TABLE` below is
datamodel.md's predicate table transcribed independently, and
`test_the_bootstrap_ontology_matches_the_documented_predicate_table` asserts
the bootstrap constant equals it — so a drift between the code and the document
fails a test rather than going unnoticed. (Checked live during this story: the
registered ontology on the real "informed" project matches, with
`advances`/`opposes` registered as the two predicates datamodel.md's single
`advances / opposes` row means.) Second, `verify_ontology()` reads the live
registration back and reports what is missing or extra, which is what a
deployment check or an integrity job calls.

**2. "`voted` edges exist only where evidence needs them."** datamodel.md:
"`voted` edges are written only for roll calls that some promise or bill-stance
edge touches. That keeps the graph to evidence, not all ~600k votes per
Congress." `write_vote_evidence` therefore refuses unless this writer has
already written a `tested_by` edge or a bill-stance edge reaching that roll
call — or the caller passes `evidence_bearing=True` to say it knows from
outside this writer's view (a verdict agent in another process). The forced
path is named and explicit precisely so the guard can never be bypassed by
accident.

**3. "Canonical names are NoSQL ids."** Every entity this writer creates is
named by the id string of the row it stands for, and `_check_row_id` rejects a
name that cannot be one — a bare human name, a blank, a name with whitespace.
That is what makes a traversal's `label` directly readable as
`get_row("people", label)` with no mapping table in between, and therefore what
makes every grounded answer citable.

===============================================================================
WHAT THIS DOES NOT DO, STATED PLAINLY
===============================================================================

**No `voted` edge has been written against real data, because the data does not
exist yet.** A `voted` edge needs a `member_votes` row, which needs a roll call
from Congress.gov, which needs `CONGRESS_GOV_API_KEY` — absent in every
environment today (see `app.scheduling.jobs`, where `load_house_rollcalls` is
registered and reports BLOCKED for exactly this reason). The writer's `voted`
path is built, tested against the fake, and guarded by criterion 2's rule; it
has never run against a real vote. Fabricating one to make this story look
complete would put an uncitable claim in the evidence graph, which is the one
thing prd.md's "evidence over opinion" principle forbids.

**No `made_promise` edge has been written against real data either, for the
same kind of reason**: `promises` rows come from story 9.3's extractor, which is
in flight in a separate PR. What exists today is 1,318 real `promise_sources`
rows (story 9.1's archived campaign pages) but no extracted promises. So
`write_promise_evidence` is built and tested, and `scripts/write_evidence_graph.py`
writes promise edges for every real promise it finds — which is zero today and
will be the real count the moment 9.3 lands, with no code change here.

**What HAS been written against real data:** `person -> ran_in -> candidacy`
edges, from the real `candidacies` rows story 2.6 loaded. That is the one
predicate in datamodel.md's table whose both endpoints exist in the live
project today, and `scripts/write_evidence_graph.py` is the script that writes
them.

===============================================================================
WHY THE WRITER IS NOT IDEMPOTENT-BY-BOOKKEEPING
===============================================================================

ZeroDB's `graph_upsert_entity` is a genuine upsert (its response carries
`status: created` vs `updated`), and `graph_create_edge` on an existing
(source, predicate, target) triple does not create a duplicate — confirmed
against the live API during story 1.2 and mirrored by the fake. So re-running
the writer over the same data converges rather than accumulating, and this
module keeps no "already written" ledger of its own. The one exception is the
in-process `_evidence_bearing_roll_calls` set, which exists only to answer
criterion 2's question within one run; a fresh process starts empty and a
caller that needs the guard across processes passes `evidence_bearing=True`
with its own reason.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.bootstrap import ONTOLOGY_ENTITY_TYPES, ONTOLOGY_PREDICATES
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: datamodel.md's "Knowledge graph ontology" predicate table, transcribed
#: independently of `app.bootstrap.ONTOLOGY_PREDICATES` so the two can be
#: compared. `advances` and `opposes` share one row in the document
#: (`advances / opposes`) and are two predicates here, because an edge carries
#: exactly one predicate and "one of two" is not something an ontology can
#: express. Everything else is one row, one entry, in document order.
DOCUMENTED_PREDICATE_TABLE: dict[str, dict[str, str]] = {
    "ran_in": {"source": "person", "target": "candidacy"},
    "made_promise": {"source": "candidacy", "target": "promise"},
    "about": {"source": "promise", "target": "stance"},
    "tested_by": {"source": "promise", "target": "roll_call"},
    "voted": {"source": "person", "target": "roll_call"},
    "sponsored": {"source": "person", "target": "bill"},
    "on": {"source": "roll_call", "target": "bill"},
    "advances": {"source": "bill", "target": "stance"},
    "opposes": {"source": "bill", "target": "stance"},
    "has_verdict": {"source": "promise", "target": "verdict"},
    "under": {"source": "stance", "target": "topic"},
}

#: The two directions a bill can bear on a stance, per datamodel.md's
#: `advances / opposes` row. A third value is a caller bug, not a new predicate.
BILL_STANCE_EFFECTS = ("advances", "opposes")

#: A canonical name must look like one of Informed's deterministic row ids:
#: at least one non-space run, no internal whitespace, and at least one of the
#: separators every documented id pattern uses (`:`, `/` or `-`). Deliberately
#: permissive about the exact shape — datamodel.md documents fifteen different
#: id patterns and a per-table regex here would be a second source of truth for
#: all of them — but strict about the thing that actually goes wrong: a human
#: name or a display label slipping in where an id belongs, which makes the
#: resulting answer uncitable. `person:B000944` passes; `Marcy Kaptur` does not.
_ROW_ID = re.compile(r"^[^\s]*[:/-][^\s]*$")


class EvidenceGraphError(Exception):
    """Base class for a refused evidence-graph write."""


class UnknownPredicateError(EvidenceGraphError):
    """The predicate is not in datamodel.md's registered ontology."""


class WrongEndpointTypeError(EvidenceGraphError):
    """The endpoints' entity types do not match the predicate's signature."""


@dataclass(frozen=True)
class OntologyReport:
    """What `verify_ontology` found registered on the live project.

    Reported rather than raised so a deployment check can print the whole
    difference at once instead of the first thing wrong — the same reason
    `app.integrity.checks` reports violations rather than raising on the first.
    """

    registered: bool
    missing_entity_types: list[str] = field(default_factory=list)
    missing_predicates: list[str] = field(default_factory=list)
    unexpected_predicates: list[str] = field(default_factory=list)
    #: Predicates registered under the right name but with the wrong source or
    #: target type — the subtlest drift, and the one that lets a wrong-way-round
    #: edge through validation.
    mistyped_predicates: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.registered
            and not self.missing_entity_types
            and not self.missing_predicates
            and not self.unexpected_predicates
            and not self.mistyped_predicates
        )

    def summary(self) -> str:
        if not self.registered:
            return "no ontology is registered for this project"
        if self.ok:
            return (
                f"the registered ontology matches datamodel.md: "
                f"{len(ONTOLOGY_ENTITY_TYPES)} entity types, "
                f"{len(DOCUMENTED_PREDICATE_TABLE)} predicates"
            )
        return (
            f"ontology drift: missing entity types {self.missing_entity_types}; "
            f"missing predicates {self.missing_predicates}; "
            f"unexpected predicates {self.unexpected_predicates}; "
            f"mistyped predicates {self.mistyped_predicates}"
        )


@dataclass(frozen=True)
class WriteReport:
    """What one `write_*_evidence` call wrote."""

    entities_written: int = 0
    edges_written: int = 0
    predicates: tuple[str, ...] = ()

    def summary(self) -> str:
        return (
            f"{self.entities_written} entit{'y' if self.entities_written == 1 else 'ies'}, "
            f"{self.edges_written} edge(s): {', '.join(self.predicates) or 'none'}"
        )


@dataclass(frozen=True)
class VoteEdgeResult:
    """Whether a `voted` edge was written, and if not, why not.

    A refusal is an ordinary outcome here, not an error: the overwhelming
    majority of a member's votes are correctly NOT in the evidence graph
    (datamodel.md's ~600k-per-Congress point), so a caller looping over a
    member's whole record expects mostly `written=False` and must not have to
    catch an exception per vote.
    """

    written: bool
    reason: str | None = None


class EvidenceGraphWriter:
    """Writes the evidence graph per datamodel.md's ontology, or refuses to.

    Stateless apart from `_evidence_bearing_roll_calls`, which records the roll
    calls THIS writer has seen evidence reach (see the module docstring on
    criterion 2). Safe to construct per ingest run; sharing one across runs
    only widens that set, which is sound but gives the guard a longer memory
    than the run it is guarding.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._client = client
        #: Roll calls a `tested_by` edge named directly.
        self._evidence_bearing_roll_calls: set[str] = set()
        #: roll_call_id -> bill_id, from every `on` edge written. Needed because
        #: the two halves of datamodel.md's `voted` rule can arrive in either
        #: order: a bill can be scored against a stance before or after the roll
        #: call on it is linked, and the roll call bears evidence either way.
        self._roll_call_bills: dict[str, str] = {}
        #: Bills that bear on some stance, from every `advances`/`opposes` edge.
        self._bill_stance_bills: set[str] = set()

    # --- the ontology -----------------------------------------------------

    @staticmethod
    def predicates() -> dict[str, Any]:
        """The registered ontology's predicates — `app.bootstrap`'s own constant.

        Returned by identity rather than copied, deliberately: the writer must
        validate against the ontology that is actually registered, and a copy
        is a second source of truth waiting to drift. `DOCUMENTED_PREDICATE_TABLE`
        is the independent transcription that keeps the bootstrap constant
        honest; this is the thing that is live.
        """
        return ONTOLOGY_PREDICATES

    @staticmethod
    def entity_types() -> tuple[str, ...]:
        """The registered ontology's entity types."""
        return ONTOLOGY_ENTITY_TYPES

    def verify_ontology(self) -> OntologyReport:
        """Read the live ontology back and compare it to datamodel.md's table.

        The only way to answer "is the ontology registered" honestly:
        `graph_upsert_ontology`'s own response echoes back whatever was just
        sent, so a write can never confirm what was there before (see
        `ZeroDBClient.graph_get_ontology`).
        """
        current = self._client.graph_get_ontology()
        if current is None:
            return OntologyReport(registered=False)

        registered_types = set(current.entity_types)
        registered_predicates: Mapping[str, Any] = current.predicates or {}

        missing_predicates: list[str] = []
        mistyped: list[str] = []
        for predicate, spec in DOCUMENTED_PREDICATE_TABLE.items():
            live = registered_predicates.get(predicate)
            if live is None:
                missing_predicates.append(predicate)
                continue
            if not isinstance(live, dict) or (
                live.get("source") != spec["source"] or live.get("target") != spec["target"]
            ):
                mistyped.append(predicate)

        report = OntologyReport(
            registered=True,
            missing_entity_types=[t for t in ONTOLOGY_ENTITY_TYPES if t not in registered_types],
            missing_predicates=missing_predicates,
            unexpected_predicates=sorted(
                set(registered_predicates) - set(DOCUMENTED_PREDICATE_TABLE)
            ),
            mistyped_predicates=mistyped,
        )
        logger.info("ontology check: %s", report.summary())
        return report

    # --- the generic edge write ------------------------------------------

    def write_edge(
        self,
        *,
        source: tuple[str, str],
        target: tuple[str, str],
        predicate: str,
        confidence: float = 1.0,
        properties: dict[str, Any] | None = None,
    ) -> WriteReport:
        """Upsert both endpoints and create one edge between them.

        `source` and `target` are `(row_id, entity_type)` pairs. Both the
        predicate and the endpoint types are validated against the registered
        ontology BEFORE the first request, so a rejected write leaves the graph
        exactly as it was — there is no half-written entity pair to clean up.
        That ordering is the whole reason this method exists rather than two
        client calls at the call site.

        Raises:
            UnknownPredicateError: the predicate is not in the ontology.
            WrongEndpointTypeError: an endpoint's type is not the one the
                predicate's signature names (including the two-arguments-
                swapped case, which is the common mistake).
            ValueError: an endpoint's name is not a plausible NoSQL row id.
        """
        spec = ONTOLOGY_PREDICATES.get(predicate)
        if spec is None:
            raise UnknownPredicateError(
                f"{predicate!r} is not a predicate in datamodel.md's ontology; "
                f"the registered predicates are {sorted(ONTOLOGY_PREDICATES)}"
            )

        source_id, source_type = source
        target_id, target_type = target
        if source_type != spec["source"] or target_type != spec["target"]:
            raise WrongEndpointTypeError(
                f"{predicate!r} connects {spec['source']} -> {spec['target']}, "
                f"not {source_type} -> {target_type}"
            )

        self._check_row_id(source_id, source_type)
        self._check_row_id(target_id, target_type)

        self._client.graph_upsert_entity(canonical_name=source_id, entity_type=source_type)
        self._client.graph_upsert_entity(canonical_name=target_id, entity_type=target_type)
        self._client.graph_create_edge(
            source_name=source_id,
            target_name=target_id,
            predicate=predicate,
            confidence=confidence,
            properties=properties or {},
        )
        # Ids and a predicate only — all public-record identifiers, nothing a
        # voter said. See `app.agent_memory` for the other side of that line.
        logger.info("wrote %s -%s-> %s", source_id, predicate, target_id)
        return WriteReport(entities_written=2, edges_written=1, predicates=(predicate,))

    # --- the per-record writes ingest and verdict agents call -------------

    def write_candidacy_evidence(
        self,
        *,
        person_id: str,
        candidacy_id: str,
        party: str | None,
        is_incumbent: bool,
    ) -> WriteReport:
        """`person -> ran_in -> candidacy`, with datamodel.md's edge properties.

        The one predicate whose both endpoints exist in the live project today,
        and therefore the one `scripts/write_evidence_graph.py` writes from real
        data. Called per `candidacies` row.
        """
        return self.write_edge(
            source=(person_id, "person"),
            target=(candidacy_id, "candidacy"),
            predicate="ran_in",
            properties={"party": party, "is_incumbent": is_incumbent},
        )

    def write_promise_evidence(
        self,
        *,
        candidacy_id: str,
        promise_id: str,
        source_id: str,
        captured_at: str,
        stance_id: str | None = None,
        stance_confidence: float = 1.0,
        verdict_id: str | None = None,
        methodology_version: str | None = None,
        verdict_is_current: bool = True,
    ) -> WriteReport:
        """Everything a promise contributes: `made_promise`, `about`, `has_verdict`.

        A promise that has been extracted but not yet classified to a stance or
        judged by the verdict agent writes only `made_promise` — that is still
        evidence (the quote, its source, the capture time), and withholding it
        until a verdict exists would mean the agent could not cite a promise
        whose verdict is pending.
        """
        report = self.write_edge(
            source=(candidacy_id, "candidacy"),
            target=(promise_id, "promise"),
            predicate="made_promise",
            properties={"source_id": source_id, "captured_at": captured_at},
        )
        if stance_id is not None:
            report = _merge(
                report,
                self.write_edge(
                    source=(promise_id, "promise"),
                    target=(stance_id, "stance"),
                    predicate="about",
                    confidence=stance_confidence,
                    properties={"confidence": stance_confidence},
                ),
            )
        if verdict_id is not None:
            report = _merge(
                report,
                self.write_edge(
                    source=(promise_id, "promise"),
                    target=(verdict_id, "verdict"),
                    predicate="has_verdict",
                    properties={
                        "methodology_version": methodology_version,
                        "is_current": verdict_is_current,
                    },
                ),
            )
        return report

    def write_promise_mapping_evidence(
        self,
        *,
        promise_id: str,
        roll_call_id: str,
        bill_id: str | None = None,
        effect: str,
        confidence: float,
        qa_status: str | None = None,
    ) -> WriteReport:
        """`promise -> tested_by -> roll_call`, one entry of `promises.mappings[]`.

        This is the write that makes a roll call evidence-bearing, so it is also
        where `_evidence_bearing_roll_calls` grows — criterion 2's guard is a
        consequence of this method having been called, never a separate flag
        someone has to remember to set.
        """
        report = self.write_edge(
            source=(promise_id, "promise"),
            target=(roll_call_id, "roll_call"),
            predicate="tested_by",
            confidence=confidence,
            properties={"effect": effect, "confidence": confidence, "qa_status": qa_status},
        )
        self._evidence_bearing_roll_calls.add(roll_call_id)
        if bill_id is not None:
            report = _merge(
                report, self.write_roll_call_evidence(roll_call_id=roll_call_id, bill_id=bill_id)
            )
        return report

    def write_roll_call_evidence(
        self,
        *,
        roll_call_id: str,
        bill_id: str,
        vote_type: str | None = None,
        is_bundled: bool = False,
    ) -> WriteReport:
        """`roll_call -> on -> bill`, with the vote typing a verdict depends on."""
        report = self.write_edge(
            source=(roll_call_id, "roll_call"),
            target=(bill_id, "bill"),
            predicate="on",
            properties={"vote_type": vote_type, "is_bundled": is_bundled},
        )
        self._roll_call_bills[roll_call_id] = bill_id
        return report

    def write_bill_stance_evidence(
        self,
        *,
        bill_id: str,
        stance_id: str,
        effect: str,
        confidence: float,
        method: str,
    ) -> WriteReport:
        """`bill -> advances|opposes -> stance`.

        Also makes any roll call already linked to this bill evidence-bearing,
        which is the "or bill-stance edge" half of datamodel.md's `voted` rule:
        a bill scored against a stance is evidence even when no promise
        mentions it.
        """
        if effect not in BILL_STANCE_EFFECTS:
            raise ValueError(
                f"effect must be one of {BILL_STANCE_EFFECTS}, got {effect!r}; "
                f"datamodel.md's predicate table has no third direction"
            )
        report = self.write_edge(
            source=(bill_id, "bill"),
            target=(stance_id, "stance"),
            predicate=effect,
            confidence=confidence,
            properties={"confidence": confidence, "method": method},
        )
        self._bill_stance_bills.add(bill_id)
        return report

    def write_sponsorship_evidence(
        self, *, person_id: str, bill_id: str, role: str
    ) -> WriteReport:
        """`person -> sponsored -> bill`, from `bills.sponsors[]`."""
        return self.write_edge(
            source=(person_id, "person"),
            target=(bill_id, "bill"),
            predicate="sponsored",
            properties={"role": role},
        )

    def write_stance_evidence(self, *, stance_id: str, topic_id: str) -> WriteReport:
        """`stance -> under -> topic`. The taxonomy's own shape in the graph."""
        return self.write_edge(
            source=(stance_id, "stance"),
            target=(topic_id, "topic"),
            predicate="under",
        )

    def write_vote_evidence(
        self,
        *,
        person_id: str,
        roll_call_id: str,
        position: str,
        evidence_bearing: bool | None = None,
    ) -> VoteEdgeResult:
        """`person -> voted -> roll_call`, but ONLY where evidence needs it.

        datamodel.md: "`voted` edges are written only for roll calls that some
        promise or bill-stance edge touches. That keeps the graph to evidence,
        not all ~600k votes per Congress."

        So this refuses — returning `written=False` rather than raising, since a
        refusal is the normal case for the vast majority of a member's record —
        unless this writer has already written a `tested_by` edge to that roll
        call, or an `on` edge from it to a bill that bears on a stance.

        Args:
            evidence_bearing: pass `True` to assert from outside this writer's
                view that the roll call bears evidence (a verdict agent in
                another process that has just written the mapping). Explicit and
                named so the guard can never be bypassed silently; passing
                `False` refuses even a roll call this writer has seen, which is
                how a caller says "not yet".
        """
        if evidence_bearing is None:
            evidence_bearing = roll_call_id in self.evidence_bearing_roll_calls()
        if not evidence_bearing:
            logger.debug(
                "declined a voted edge for %s: no evidence reaches it", roll_call_id
            )
            return VoteEdgeResult(
                written=False,
                reason="no promise or bill-stance edge touches this roll call",
            )
        self.write_edge(
            source=(person_id, "person"),
            target=(roll_call_id, "roll_call"),
            predicate="voted",
            properties={"position": position},
        )
        return VoteEdgeResult(written=True)

    def evidence_bearing_roll_calls(self) -> set[str]:
        """Every roll call this writer has seen evidence reach, in this process.

        The union of roll calls a `tested_by` edge named and roll calls whose
        `on` edge points at a bill with a stance edge. Readable so a caller
        looping over a member's votes can filter before calling at all, rather
        than calling once per vote and discarding the refusals.
        """
        return set(self._evidence_bearing_roll_calls) | {
            roll_call
            for roll_call, bill in self._roll_call_bills.items()
            if bill in self._bill_stance_bills
        }

    # --- internals --------------------------------------------------------

    @staticmethod
    def _check_row_id(value: str, entity_type: str) -> None:
        """Refuse a canonical name that cannot be a NoSQL row id.

        The acceptance criterion "canonical names are NoSQL ids" is only real if
        something enforces it. See `_ROW_ID` for why the check is shape-based
        rather than a per-table pattern.
        """
        if not value or not value.strip() or not _ROW_ID.match(value):
            raise ValueError(
                f"a {entity_type} entity's canonical name must be its NoSQL row id "
                f"(e.g. person:B000944), not {value!r}; an entity named anything else "
                f"cannot be resolved back to a citable row"
            )


def _merge(first: WriteReport, second: WriteReport) -> WriteReport:
    return WriteReport(
        entities_written=first.entities_written + second.entities_written,
        edges_written=first.edges_written + second.edges_written,
        predicates=first.predicates + second.predicates,
    )


__all__ = [
    "BILL_STANCE_EFFECTS",
    "DOCUMENTED_PREDICATE_TABLE",
    "EvidenceGraphError",
    "EvidenceGraphWriter",
    "OntologyReport",
    "UnknownPredicateError",
    "VoteEdgeResult",
    "WriteReport",
    "WrongEndpointTypeError",
]
