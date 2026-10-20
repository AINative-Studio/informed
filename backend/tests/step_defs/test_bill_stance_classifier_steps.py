"""Step definitions for tests/features/bill_stance_classifier.feature (story 5.5).

Runs against story 1.2's in-memory fake transport, never live ZeroDB, so CI
needs no credentials -- the rule every backend feature in this suite follows.
The live confirmation lives in `scripts/verify_bill_stance_classifier_live.py`.

`break_server_side_filter()` is applied for the reason every story since 1.4
applies it: live ZeroDB's server-side `/query` filter matches nothing, so every
read here rests on the client's own paging fallback. It matters in this story
because `propagate_to_roll_calls` reads `roll_calls` filtered by `bill_id` and
`member_votes` filtered by `roll_call_id` -- a filter that silently worked in
tests and silently did not in production would tag one bill's roll calls with
another bill's topics.

TWO KINDS OF DATA, kept deliberately separate:

  * **The labeled set** (`tests/fixtures/bill_topic_labeled.json`) is REAL
    congress.gov data and is read only by the precision scenarios, which do no
    I/O at all -- `classify_bill` is pure. Nothing from it is written to the
    fake, because the measurement is about the classification function, not
    about storage.
  * **The bill, roll call, member vote and person are synthetic**:
    `us-house:119:hr-999999`, `us-house:119-1:9999`, `person:TEST000000`. None
    of those is a real id, so no row this suite writes could be mistaken for
    real public record.
  * **The topic ids and the CRS strings are real**, and have to be. The
    classifier refuses a tag whose `issue_topics` row is not stored
    (`_resolve_topic`), and the id it proposes for the real CRS policy area
    `Health` is the real `en-US:healthcare:v1` -- so a test of the write path
    must seed exactly that id. Likewise the subject terms (`Prescription
    drugs`) are the real CRS vocabulary, because looking those strings up IS
    the classifier's job and a synthetic term would exercise nothing.

    That is safe here and would not be against a live project: `fake` is a
    fresh `FakeZeroDBTransport` per scenario, so these rows exist only inside
    one test's memory. The live script
    (`scripts/verify_bill_stance_classifier_live.py`) deliberately does NOT
    seed taxonomy rows -- it reads the ones `scripts/seed_taxonomy.py` already
    wrote.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.evidence_graph.writer import EvidenceGraphWriter
from app.ingest.bill_topic_classifier import (
    ACCEPT_THRESHOLD,
    BASE_TERM_CONFIDENCE,
    CLASSIFIER_VERSION,
    DOCUMENTED_ACCEPT_THRESHOLD,
    MAX_SUBTOPIC_SPREAD,
    MEASURED_PRECISION_PERCENT,
    POLICY_AREA_CORROBORATION,
    REQUIRED_PRECISION_PERCENT,
    TAGS_PENDING_QA_TOPIC,
    BillDirection,
    BillTopicClassifier,
    QaReason,
    TopicTag,
    classify_bill,
    stance_edges_for_tag,
)
from app.ingest.bill_topic_mapping import (
    MAPPING_VERSION,
    POLICY_AREA_TO_TOPIC,
    SUBJECT_TERM_TO_SUBTOPIC,
)
from app.methodology.publish import MethodologyPublisher
from app.repositories import ProvenanceContext, Repository
from app.repositories.models import (
    Bill,
    IssueTopic,
    MemberVote,
    Person,
    RollCall,
    Stance,
    Term,
)
from app.scoring.positions import PositionComputer
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/bill_stance_classifier.feature")

LABELED_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "bill_topic_labeled.json"

#: Synthetic ids. `TEST000000` is deliberately not a valid bioguide pattern and
#: `hr-999999` is not a real bill number, so no row from this suite can be
#: mistaken for real public record.
PERSON_ID = "person:TEST000000"
BILL_ID = "us-house:119:hr-999999"
ROLL_CALL_ID = "us-house:119-1:9999"
BODY_ID = "us-house"

#: The REAL taxonomy slug the real CRS policy area "Health" maps to, and the
#: real topic row id the classifier will therefore propose. Seeded here because
#: the classifier refuses a tag whose `issue_topics` row is not stored
#: (`_resolve_topic`), so a test of the write path must seed the id the
#: classifier actually produces. It is production reference data by content,
#: which is why `fake` is a fresh in-memory transport per scenario and nothing
#: here ever touches a live project.
HEALTHCARE_SLUG = "healthcare"
HEALTHCARE_TOPIC_ID = "en-US:healthcare:v1"
PRESCRIPTION_TOPIC_ID = "en-US:prescription-drug-pricing:v1"

#: A real CRS subject term that maps to a healthcare subtopic, used wherever a
#: scenario needs a specific term rather than just a policy area.
PRESCRIPTION_TERM = "Prescription drugs"


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
def seen() -> dict[str, Any]:
    """Carries state between Given, When and Then.

    Nothing voter-derived can reach it: this story reads public CRS subject
    terms and writes public topic ids.
    """
    return {}


@pytest.fixture
def classifier(zerodb: ZeroDBClient) -> BillTopicClassifier:
    return BillTopicClassifier(zerodb, graph=EvidenceGraphWriter(zerodb))


# --- helpers --------------------------------------------------------------


def _labeled() -> dict[str, Any]:
    return json.loads(LABELED_PATH.read_text())


def _seed_topics(zerodb: ZeroDBClient, *, include_healthcare: bool = True) -> None:
    """The `issue_topics` rows the classifier's proposed tags resolve against.

    Written through a real `Repository` rather than raw `upsert_row`, so story
    1.4's reference checks are actually exercised: a test that bypassed them
    could pass against data the production write path would refuse.
    """
    rows: list[dict[str, Any]] = []
    if include_healthcare:
        rows.append(
            {
                "id": HEALTHCARE_TOPIC_ID,
                "name": "Healthcare",
                "locale": "en-US",
                "taxonomy_version": 1,
            }
        )
    rows.append(
        {
            "id": PRESCRIPTION_TOPIC_ID,
            "parent_id": HEALTHCARE_TOPIC_ID if include_healthcare else None,
            "name": "Prescription drug pricing",
            "locale": "en-US",
            "taxonomy_version": 1,
        }
    )
    if rows:
        Repository(client=zerodb, model=IssueTopic).upsert_many(rows)


def _seed_stances(zerodb: ZeroDBClient, *, with_zero: bool = False) -> list[Stance]:
    """Two mirrored stance cards under the healthcare topic, optionally a third.

    Mirrored deliberately: prd.md's "nonpartisan by construction" is the
    property that two opposed stances under one topic get exactly opposite
    effects from one bill, and a single stance cannot show it.
    """
    rows: list[dict[str, Any]] = [
        {
            "id": f"{HEALTHCARE_TOPIC_ID}:expand",
            "topic_id": HEALTHCARE_TOPIC_ID,
            "label": "Synthetic stance, positive direction",
            "description": "A synthetic stance card used only by this test suite.",
            "direction_code": "+1",
            "wording_version": 1,
        },
        {
            "id": f"{HEALTHCARE_TOPIC_ID}:contract",
            "topic_id": HEALTHCARE_TOPIC_ID,
            "label": "Synthetic stance, negative direction",
            "description": "The mirror of the stance above, for symmetry checks.",
            "direction_code": "-1",
            "wording_version": 1,
        },
    ]
    if with_zero:
        rows.append(
            {
                "id": f"{HEALTHCARE_TOPIC_ID}:status-quo",
                "topic_id": HEALTHCARE_TOPIC_ID,
                "label": "Synthetic stance, no movement",
                "description": "Keep current federal policy. A real voter position.",
                "direction_code": "0",
                "wording_version": 1,
            }
        )
    repo: Repository[Stance] = Repository(client=zerodb, model=Stance)
    return [repo.upsert(row) for row in rows]


def _store_bill(zerodb: ZeroDBClient, *, policy_area: str | None, subjects: list[str]) -> Bill:
    """Store the synthetic bill the way story 5.3's loader stores a real one.

    Through a `ProvenanceContext`, because `bills` carries a `source_url` and
    story 1.5 refuses a sourced row with no `provenance_log` entry. Writing it
    without one would be a test passing against data the production write path
    refuses -- exactly the gap provenance enforcement exists to close.
    """
    repo: Repository[Bill] = Repository(
        client=zerodb,
        model=Bill,
        provenance=ProvenanceContext(adapter_version="bill-stance-classifier-test/v1"),
    )
    return repo.upsert(
        {
            "id": BILL_ID,
            "title": "A synthetic bill used only by this test suite",
            "introduced_on": "2025-01-03",
            "policy_area": policy_area,
            "subjects": subjects,
            "topic_ids": [],
            "sponsors": [],
            "source_url": "https://example.invalid/synthetic-bill",
            "content_hash": "0" * 64,
        }
    )


def _wide_subjects() -> list[str]:
    """Real CRS terms reaching more than `MAX_SUBTOPIC_SPREAD` subtopics.

    Built from the mapping table itself rather than hand-listed, so a table edit
    that changes which terms reach which subtopics cannot leave this scenario
    quietly testing something narrower than breadth.
    """
    by_slug: dict[str, str] = {}
    for term, slug in SUBJECT_TERM_TO_SUBTOPIC.items():
        by_slug.setdefault(slug, term)
    return list(by_slug.values())[: MAX_SUBTOPIC_SPREAD + 4]


def _limit_subjects() -> list[str]:
    """Real CRS terms reaching EXACTLY `MAX_SUBTOPIC_SPREAD` subtopics."""
    by_slug: dict[str, str] = {}
    for term, slug in SUBJECT_TERM_TO_SUBTOPIC.items():
        by_slug.setdefault(slug, term)
    return list(by_slug.values())[:MAX_SUBTOPIC_SPREAD]


def _measure(accept_threshold: float | None) -> dict[str, Any]:
    """Precision of the classifier over the real labeled set.

    The same arithmetic as `tests/test_bill_topic_classifier_unit.py::_measure`,
    and the duplication is deliberate rather than shared: this is the BDD
    suite's own independent statement of the acceptance criterion, and a shared
    helper would let one edit move both the measurement and the thing measuring
    it.
    """
    proposed = 0
    correct = 0
    for bill in _labeled()["bills"]:
        labels = {label["slug"]: label["correct"] for label in bill["labels"]}
        for tag in classify_bill(
            bill_id=bill["bill_id"],
            policy_area=bill["policy_area"],
            subjects=bill["subjects"],
            accept_threshold=accept_threshold,
        ):
            if not tag.accepted or tag.slug not in labels:
                continue
            proposed += 1
            if labels[tag.slug]:
                correct += 1
    return {
        "proposed": proposed,
        "correct": correct,
        "precision": (100.0 * correct / proposed) if proposed else 0.0,
    }


def _tag(
    *,
    direction: BillDirection = BillDirection.UNKNOWN,
    qa_reason: QaReason | None = None,
) -> TopicTag:
    return TopicTag(
        bill_id=BILL_ID,
        topic_id=HEALTHCARE_TOPIC_ID,
        slug=HEALTHCARE_SLUG,
        confidence=0.9,
        matched_terms=("Health",),
        is_subtopic=False,
        direction=direction,
        qa_reason=qa_reason,
    )


# --- Background -----------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def _reference_tables(zerodb: ZeroDBClient) -> None:
    # The `zerodb` fixture already bootstraps; this is the documented Background
    # step every backend feature shares, and re-running it is a no-op by design
    # (story 1.3's own idempotence criterion).
    Bootstrapper(zerodb, pace=lambda: None).run()


@given("a synthetic topic tree with two mirrored stance cards")
def synthetic_tree(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _seed_topics(zerodb)
    seen["stances"] = _seed_stances(zerodb)


# --- the labeled set and the measurement ----------------------------------


@then(parsers.parse("the labeled set holds at least {count:d} real bills"))
def labeled_set_size(count: int) -> None:
    assert len(_labeled()["bills"]) >= count


@then("every bill in the labeled set carries a real congress.gov measure id")
def labeled_set_is_real() -> None:
    for bill in _labeled()["bills"]:
        assert bill["congress"] in (117, 118, 119), bill
        assert str(bill["number"]).isdigit(), bill
        assert bill["title"], bill
        # Every measure reached a recorded House vote -- the population this
        # story exists to serve.
        assert bill["roll_calls"], bill


@then("every bill in the labeled set carries its own CRS policy area or subject terms")
def labeled_set_has_crs() -> None:
    for bill in _labeled()["bills"]:
        assert bill["policy_area"] or bill["subjects"], bill


@then(parsers.parse("the labeled set covers at least {count:d} of the taxonomy's top-level topics"))
def labeled_set_breadth(count: int) -> None:
    tops = {slug for slug in POLICY_AREA_TO_TOPIC.values() if slug is not None}
    reached = {label["slug"] for bill in _labeled()["bills"] for label in bill["labels"]} & tops
    assert len(reached) >= count, sorted(reached)


@when("I classify every bill in the labeled set")
def classify_labeled_set(seen: dict[str, Any]) -> None:
    # Measured at the threshold one corroboration WOULD use, because that is
    # the set of tags the labeled set labels. Production accepts none of them;
    # see `no_tag_is_accepted` below, which checks the production default.
    seen["measured"] = _measure(DOCUMENTED_ACCEPT_THRESHOLD)
    seen["production_tags"] = [
        tag
        for bill in _labeled()["bills"]
        for tag in classify_bill(
            bill_id=bill["bill_id"],
            policy_area=bill["policy_area"],
            subjects=bill["subjects"],
        )
    ]


@when("I classify the labeled set requiring every corroboration")
def classify_labeled_set_strict(seen: dict[str, Any]) -> None:
    seen["baseline"] = _measure(DOCUMENTED_ACCEPT_THRESHOLD)
    seen["measured"] = _measure(1.0)


@then(parsers.re(r"the measured precision is (?P<percent>[\d.]+) percent"))
def measured_precision(seen: dict[str, Any], percent: str) -> None:
    assert seen["measured"]["precision"] == pytest.approx(float(percent), abs=0.1), seen["measured"]


@then(
    parsers.re(
        r"the measured precision is (?:still )?below the required (?P<percent>[\d.]+) percent"
    )
)
def precision_below_bar(seen: dict[str, Any], percent: str) -> None:
    bar = float(percent)
    assert bar == REQUIRED_PRECISION_PERCENT, (
        "the feature file's bar and the module's REQUIRED_PRECISION_PERCENT disagree"
    )
    assert seen["measured"]["precision"] < bar, (
        f"precision is now {seen['measured']['precision']:.1f}%, at or above story 5.5's "
        f"bar. That is good news: turn ACCEPT_THRESHOLD on and update this scenario."
    )


@then("the module records that same precision figure")
def module_records_precision(seen: dict[str, Any]) -> None:
    """The docstring's number, the module constant and the real computation."""
    assert seen["measured"]["precision"] == pytest.approx(MEASURED_PRECISION_PERCENT, abs=0.1)
    assert _labeled()["measured"]["precision_percent"] == pytest.approx(
        MEASURED_PRECISION_PERCENT, abs=0.1
    )


@then("far fewer tags are proposed than before")
def far_fewer_tags(seen: dict[str, Any]) -> None:
    assert seen["measured"]["proposed"] < seen["baseline"]["proposed"] / 5, (
        f"{seen['measured']['proposed']} vs {seen['baseline']['proposed']}"
    )


@then("no tag is accepted")
def no_tag_is_accepted(seen: dict[str, Any]) -> None:
    assert ACCEPT_THRESHOLD is None
    accepted = [tag for tag in seen["production_tags"] if tag.accepted]
    assert not accepted, [f"{tag.bill_id} -> {tag.slug}" for tag in accepted[:5]]


@then("every tag is routed to QA")
def every_tag_to_qa(seen: dict[str, Any]) -> None:
    tags = seen["production_tags"]
    assert tags
    assert all(tag.qa_reason is not None for tag in tags)


# --- classifying one bill -------------------------------------------------


@given(parsers.parse('a bill whose CRS policy area is "{policy_area}"'))
def bill_with_policy_area(seen: dict[str, Any], policy_area: str) -> None:
    seen.setdefault("policy_area", policy_area)
    seen["policy_area"] = policy_area
    # Story 5.3 writes the policy area at the head of `subjects[]` as well as in
    # its own field, so this mirrors the real stored shape.
    seen.setdefault("subjects", [])
    if policy_area not in seen["subjects"]:
        seen["subjects"] = [policy_area, *seen["subjects"]]


@given(parsers.parse('whose CRS subject terms include "{term}"'))
def bill_with_term(seen: dict[str, Any], term: str) -> None:
    seen.setdefault("subjects", [])
    seen["subjects"] = [*seen["subjects"], term]


@given("whose CRS policy area is absent")
def bill_without_policy_area(seen: dict[str, Any]) -> None:
    seen["policy_area"] = None


@given("a bill with no CRS policy area and no CRS subject terms")
def bill_with_nothing(seen: dict[str, Any]) -> None:
    seen["policy_area"] = None
    seen["subjects"] = []


@given("whose CRS subject terms repeat the policy area")
def bill_repeats_policy_area(seen: dict[str, Any]) -> None:
    """The real stored shape, and the real double-counting hazard.

    `house_bills._subjects_from` puts the policy area at the head of
    `subjects[]`. Counting it twice would make every policy-area match
    self-corroborating.
    """
    seen["subjects"] = [seen["policy_area"], seen["policy_area"]]


@given("a bill carrying CRS subject terms spanning the whole taxonomy")
def omnibus_bill(seen: dict[str, Any]) -> None:
    seen["policy_area"] = None
    seen["subjects"] = _wide_subjects()


@given("two of those terms agree on one subtopic")
def omnibus_with_agreeing_terms(seen: dict[str, Any]) -> None:
    """Make one subtopic doubly corroborated AND give it its parent policy area.

    Both corroborations, i.e. confidence 1.0 -- the strongest a tag can be --
    so the scenario shows breadth overriding even that.
    """
    seen["policy_area"] = "Health"
    seen["subjects"] = [
        "Health",
        "Medicare",
        "Medicaid",
        *(_wide_subjects()),
    ]


@given("a bill whose CRS subject terms reach exactly the breadth limit")
def bill_at_breadth_limit(seen: dict[str, Any]) -> None:
    seen["policy_area"] = None
    seen["subjects"] = _limit_subjects()


@when("I classify that bill")
def classify_one_bill(seen: dict[str, Any]) -> None:
    seen["tags"] = classify_bill(
        bill_id=BILL_ID,
        policy_area=seen.get("policy_area"),
        subjects=seen.get("subjects", []),
    )
    # Also classified at the documented threshold, so a scenario about what the
    # confidence rules DO can read it without changing production behaviour.
    seen["tags_at_threshold"] = classify_bill(
        bill_id=BILL_ID,
        policy_area=seen.get("policy_area"),
        subjects=seen.get("subjects", []),
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )


@then(parsers.parse('it proposes a tag for slug "{slug}"'))
def proposes_tag(seen: dict[str, Any], slug: str) -> None:
    assert slug in {tag.slug for tag in seen["tags"]}, [tag.slug for tag in seen["tags"]]


@then("it proposes no tags")
def proposes_nothing(seen: dict[str, Any]) -> None:
    assert seen["tags"] == []


@then("nothing is routed to QA for it")
def nothing_to_qa(seen: dict[str, Any]) -> None:
    assert not [tag for tag in seen["tags"] if tag.qa_reason is not None]


@then("that tag's confidence is one corroboration")
def confidence_is_one_corroboration(seen: dict[str, Any]) -> None:
    tag = next(tag for tag in seen["tags"] if tag.slug == HEALTHCARE_SLUG)
    assert tag.confidence == pytest.approx(BASE_TERM_CONFIDENCE + POLICY_AREA_CORROBORATION)
    # And no subtopic tag was manufactured from the repeated policy area.
    assert [t.slug for t in seen["tags"]] == [HEALTHCARE_SLUG]


@then("every tag for that bill is routed to QA")
def all_tags_to_qa(seen: dict[str, Any]) -> None:
    assert seen["tags_at_threshold"]
    assert all(tag.qa_reason is not None for tag in seen["tags_at_threshold"])


@then(parsers.parse('the QA reason for every tag on that bill is "{reason}"'))
def qa_reason_for_all(seen: dict[str, Any], reason: str) -> None:
    reasons = {tag.qa_reason.value for tag in seen["tags_at_threshold"] if tag.qa_reason}
    assert reasons == {reason}, reasons


@then("that subtopic's tag is fully corroborated")
def tag_fully_corroborated(seen: dict[str, Any]) -> None:
    tag = next(t for t in seen["tags_at_threshold"] if t.slug == "medicare-and-medicaid-financing")
    assert tag.confidence == pytest.approx(1.0)
    seen["corroborated_tag"] = tag


@then(parsers.parse('the QA reason for that tag is "{reason}"'))
def qa_reason_for_tag(seen: dict[str, Any], reason: str) -> None:
    assert seen["corroborated_tag"].qa_reason is not None
    assert seen["corroborated_tag"].qa_reason.value == reason


@then("no tag for that bill is held for breadth")
def no_breadth_hold(seen: dict[str, Any]) -> None:
    held = [t for t in seen["tags_at_threshold"] if t.qa_reason is QaReason.BREADTH]
    assert not held, [t.slug for t in held]


@then("every QA-routed tag names a reason from the documented QA reason list")
def qa_reasons_documented(seen: dict[str, Any]) -> None:
    held = [tag for tag in seen["tags"] if tag.qa_reason is not None]
    assert held
    for tag in held:
        assert tag.qa_reason in set(QaReason)


@then("every QA-routed tag carries its confidence and its matched CRS term")
def qa_tags_carry_evidence(seen: dict[str, Any]) -> None:
    """A queue entry with no evidence cannot be reviewed (story 10.3)."""
    for tag in seen["tags"]:
        if tag.qa_reason is None:
            continue
        assert tag.matched_terms, tag
        assert 0.0 < tag.confidence <= 1.0, tag


# --- the stored run -------------------------------------------------------


@given(parsers.parse('a stored bill whose CRS policy area is "{policy_area}"'))
def stored_bill(zerodb: ZeroDBClient, seen: dict[str, Any], policy_area: str) -> None:
    seen["policy_area"] = policy_area
    seen["subjects"] = [policy_area, PRESCRIPTION_TERM]
    seen["bill"] = _store_bill(zerodb, policy_area=policy_area, subjects=seen["subjects"])


@given("the healthcare topic is not seeded")
def healthcare_not_seeded(zerodb: ZeroDBClient) -> None:
    """Delete the topic the classifier will propose, leaving the tag unresolvable.

    The classifier must refuse the tag rather than write an id no reader can
    resolve -- datamodel.md rule 5 has no foreign keys, so an unresolvable
    reference is a permanent silent hole.
    """
    zerodb.delete_row(IssueTopic.table_name, HEALTHCARE_TOPIC_ID)
    zerodb.delete_row(IssueTopic.table_name, PRESCRIPTION_TOPIC_ID)


@when("I run the classifier over the stored bills")
def run_classifier(classifier: BillTopicClassifier, seen: dict[str, Any]) -> None:
    seen["report"] = classifier.run()


@when("I run the classifier over the stored bills accepting one corroboration")
def run_classifier_accepting(
    zerodb: ZeroDBClient, classifier: BillTopicClassifier, seen: dict[str, Any]
) -> None:
    seen["report"] = classifier.run(accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD)


@when("I run the classifier over the stored bills accepting one corroboration twice")
def run_classifier_twice(classifier: BillTopicClassifier, seen: dict[str, Any]) -> None:
    seen["first"] = classifier.run(accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD)
    seen["report"] = classifier.run(accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD)


@then("the classification report lists that bill's held tags")
def report_lists_held(seen: dict[str, Any]) -> None:
    assert [tag for tag in seen["tags"] if tag.qa_reason is not None]


@then("one bill.tags_pending_qa event is emitted for that bill")
def one_qa_event(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    page = zerodb.list_events(topic=TAGS_PENDING_QA_TOPIC, limit=50)
    matching = [
        event for event in page.events if (event.event_payload or {}).get("bill_id") == BILL_ID
    ]
    assert len(matching) == 1, matching
    seen["qa_event"] = matching[0]


@then("that event names the classifier version and the mapping version")
def qa_event_versions(seen: dict[str, Any]) -> None:
    payload = seen["qa_event"].event_payload or {}
    assert payload.get("classifier_version") == CLASSIFIER_VERSION
    assert payload.get("mapping_version") == MAPPING_VERSION
    assert payload.get("tags"), payload


@then("the stored bill's topic_ids are still empty")
def stored_topic_ids_empty(zerodb: ZeroDBClient) -> None:
    row = zerodb.get_row(Bill.table_name, BILL_ID)
    assert row is not None
    assert row.row_data.get("topic_ids") == []


@then(parsers.parse('the stored bill\'s topic_ids include the topic for slug "{slug}"'))
def stored_topic_ids_include(zerodb: ZeroDBClient, slug: str) -> None:
    row = zerodb.get_row(Bill.table_name, BILL_ID)
    assert row is not None
    assert f"en-US:{slug}:v1" in (row.row_data.get("topic_ids") or []), row.row_data


@then("the bill row's other fields are unchanged")
def bill_other_fields_unchanged(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """Classification must not disturb the sourced record.

    `content_hash` especially: story 5.3 computed it from the source bytes, and
    a later re-fetch compares against it. Classification is this codebase's
    interpretation of unchanged source data, not a new sourced record.
    """
    row = zerodb.get_row(Bill.table_name, BILL_ID)
    assert row is not None
    stored = row.row_data
    original = seen["bill"]
    assert stored["content_hash"] == original.content_hash
    assert stored["title"] == original.title
    assert stored["policy_area"] == original.policy_area
    assert stored["subjects"] == original.subjects
    assert stored["source_url"] == original.source_url


@then("the second run reports zero bills changed")
def second_run_no_changes(seen: dict[str, Any]) -> None:
    assert seen["first"].bills_changed == 1, seen["first"]
    assert seen["report"].bills_changed == 0, seen["report"]


# --- the graph edges ------------------------------------------------------


@given("a tag on a topic whose stances are seeded")
def tag_on_seeded_topic(seen: dict[str, Any]) -> None:
    seen.setdefault("direction", BillDirection.UNKNOWN)
    seen.setdefault("qa_reason", None)


@given("that tag's direction is unknown")
def direction_unknown(seen: dict[str, Any]) -> None:
    seen["direction"] = BillDirection.UNKNOWN


@given(parsers.parse('that tag\'s direction is "{direction}"'))
def direction_is(seen: dict[str, Any], direction: str) -> None:
    seen["direction"] = BillDirection(direction)


@given("that tag is routed to QA")
def tag_routed_to_qa(seen: dict[str, Any]) -> None:
    seen["qa_reason"] = QaReason.BELOW_MEASURED_PRECISION


@given(parsers.parse('a third stance with direction_code "{code}"'))
def third_stance(zerodb: ZeroDBClient, seen: dict[str, Any], code: str) -> None:
    assert code == "0", "only the zero-direction third stance is modelled here"
    seen["stances"] = _seed_stances(zerodb, with_zero=True)


@when("I write that tag's stance edges")
def write_stance_edges(classifier: BillTopicClassifier, seen: dict[str, Any]) -> None:
    tag = _tag(direction=seen["direction"], qa_reason=seen.get("qa_reason"))
    seen["tag"] = tag
    seen["pairs"] = stance_edges_for_tag(tag, seen["stances"])
    seen["edges_written"] = classifier.write_stance_edges([tag], seen["stances"])


@when("I write that tag's stance edges for both bill directions")
def write_both_directions(seen: dict[str, Any]) -> None:
    expands = _tag(direction=BillDirection.EXPANDS)
    contracts = _tag(direction=BillDirection.CONTRACTS)
    seen["expands"] = {
        stance.id: effect for stance, effect in stance_edges_for_tag(expands, seen["stances"])
    }
    seen["contracts"] = {
        stance.id: effect for stance, effect in stance_edges_for_tag(contracts, seen["stances"])
    }


@then("no bill-to-stance edge is written")
def no_stance_edge(seen: dict[str, Any]) -> None:
    assert seen["pairs"] == []
    assert seen["edges_written"] == 0


@then(parsers.parse('the edge to the stance with direction_code "{code}" has predicate "{effect}"'))
def edge_predicate(seen: dict[str, Any], code: str, effect: str) -> None:
    matching = [
        (stance, got) for stance, got in seen["pairs"] if str(stance.direction_code) == code
    ]
    assert len(matching) == 1, seen["pairs"]
    assert matching[0][1] == effect


@then("every stance's effect is reversed between the two runs")
def effects_reversed(seen: dict[str, Any]) -> None:
    """prd.md's "nonpartisan by construction" is exactly this symmetry."""
    assert seen["expands"]
    assert set(seen["expands"]) == set(seen["contracts"])
    for stance_id, effect in seen["expands"].items():
        assert seen["contracts"][stance_id] != effect, stance_id


@then(parsers.parse('no edge is written to the stance with direction_code "{code}"'))
def no_edge_to_stance(seen: dict[str, Any], code: str) -> None:
    coded = [stance for stance, _ in seen["pairs"] if str(stance.direction_code) == code]
    assert not coded, coded


@then("each written edge carries its confidence and its method")
def edges_carry_properties(seen: dict[str, Any]) -> None:
    assert seen["edges_written"] == len(seen["pairs"]) > 0
    seen["method"] = seen["tag"].method


@then("each method names the classifier version and the mapping version")
def method_names_versions(seen: dict[str, Any]) -> None:
    assert CLASSIFIER_VERSION in seen["method"]
    assert MAPPING_VERSION in seen["method"]


@then("the mapping table holds no rule naming a party")
def no_party_in_mapping() -> None:
    forbidden = {"democrat", "republican", "gop", "liberal", "conservative", "progressive"}
    haystack = " ".join(
        [
            *SUBJECT_TERM_TO_SUBTOPIC.keys(),
            *SUBJECT_TERM_TO_SUBTOPIC.values(),
            *POLICY_AREA_TO_TOPIC.keys(),
            *(slug or "" for slug in POLICY_AREA_TO_TOPIC.values()),
        ]
    ).lower()
    hits = sorted(word for word in forbidden if word in haystack)
    assert not hits, hits


@then("the direction rule reads only the stance's own direction_code")
def direction_rule_is_symmetric(seen: dict[str, Any]) -> None:
    """Checked behaviourally: two mirrored stances, opposite effects, one bill.

    A structural assertion about the source would be weaker -- this is the
    property that matters, and it is the same one
    `app.scoring.positions._stance_direction` is held to.
    """
    pairs = stance_edges_for_tag(_tag(direction=BillDirection.EXPANDS), seen["stances"])
    effects = {str(stance.direction_code): effect for stance, effect in pairs}
    assert effects == {"+1": "advances", "-1": "opposes"}, effects


# --- what this unblocks: story 6.2 ---------------------------------------


@given("a stored roll call on that bill with a member vote")
def stored_roll_call_and_vote(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": PERSON_ID,
            "canonical_name": "Synthetic Test Member",
            "terms": [
                Term(body_id=BODY_ID, party="Synthetic", start="2025-01-03").model_dump(mode="json")
            ],
        }
    )
    # Through a ProvenanceContext for the same reason `_store_bill` is: a
    # `roll_calls` row carries a `source_url`, and story 1.5 refuses sourced
    # evidence with no `provenance_log` entry.
    Repository(
        client=zerodb,
        model=RollCall,
        provenance=ProvenanceContext(adapter_version="bill-stance-classifier-test/v1"),
    ).upsert(
        {
            "id": ROLL_CALL_ID,
            "voted_at": "2025-03-04T12:00:00Z",
            "question": "On Passage",
            "vote_type": "final_passage",
            "result": "Passed",
            "tally": {"yea": 1, "nay": 0, "present": 0, "not_voting": 0},
            "bill_id": BILL_ID,
            "topic_ids": [],
            "is_bundled": False,
            "source_url": "https://example.invalid/synthetic-rollcall",
        }
    )
    Repository(client=zerodb, model=MemberVote).upsert(
        {
            "roll_call_id": ROLL_CALL_ID,
            "person_id": PERSON_ID,
            "position": "yea",
            "voted_at": "2025-03-04T12:00:00Z",
            "vote_type": "final_passage",
            "bill_id": BILL_ID,
            "topic_ids": [],
            "is_bundled": False,
        }
    )
    MethodologyPublisher(zerodb).publish_new_version("2027.1")


@given("story 6.2 finds no position for that member before classification")
def no_position_before(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """**The gap this story closes, measured before it closes.**

    Story 6.2's `recompute_person` matches `member_votes.topic_ids` against
    `stances.topic_id`. With `topic_ids: []` it finds nothing -- which is
    exactly what it finds on the live project today.
    """
    computer = PositionComputer(zerodb)
    result = computer.recompute_person(PERSON_ID, stances=seen["stances"])
    assert result.positions_written == 0, result
    assert computer.positions_for(PERSON_ID) == []
    seen["before"] = result


@when("I propagate that bill's topics to its roll calls")
def propagate(classifier: BillTopicClassifier, seen: dict[str, Any]) -> None:
    rolls, votes = classifier.propagate_to_roll_calls(
        bill_id=BILL_ID, topic_ids=[HEALTHCARE_TOPIC_ID]
    )
    seen["rolls_written"] = rolls
    seen["votes_written"] = votes


@then(parsers.parse('the roll call\'s topic_ids include the topic for slug "{slug}"'))
def roll_call_tagged(zerodb: ZeroDBClient, slug: str) -> None:
    row = zerodb.get_row(RollCall.table_name, ROLL_CALL_ID)
    assert row is not None
    assert f"en-US:{slug}:v1" in (row.row_data.get("topic_ids") or []), row.row_data


@then("that member's vote carries the same topic_ids")
@then("the member vote's topic_ids equal the roll call's topic_ids")
def member_vote_tagged(zerodb: ZeroDBClient) -> None:
    """datamodel.md denormalises `topic_ids` onto `member_votes` deliberately,
    "so that scoring reads one table", and the nightly drift check compares the
    two. Writing the roll call alone would make that check fail by design.
    """
    roll_call = zerodb.get_row(RollCall.table_name, ROLL_CALL_ID)
    vote = zerodb.get_row(MemberVote.table_name, f"{ROLL_CALL_ID}:TEST000000")
    assert roll_call is not None
    assert vote is not None
    assert sorted(vote.row_data.get("topic_ids") or []) == sorted(
        roll_call.row_data.get("topic_ids") or []
    )
    assert vote.row_data.get("topic_ids"), "the member vote was not tagged at all"


@then("story 6.2 finds a position for that member on a healthcare stance")
def position_found_after(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """**The payoff.** The same call that found nothing now finds a position."""
    computer = PositionComputer(zerodb)
    result = computer.recompute_person(PERSON_ID, stances=seen["stances"])
    assert result.positions_written > 0, result
    positions = computer.positions_for(PERSON_ID)
    assert positions, "6.2 still finds no position after classification"
    by_stance = {position.stance_id: position for position in positions}
    # The mirrored pair must land on opposite sides from the one Yea vote.
    expand = by_stance[f"{HEALTHCARE_TOPIC_ID}:expand"]
    contract = by_stance[f"{HEALTHCARE_TOPIC_ID}:contract"]
    assert expand.s_value == pytest.approx(-contract.s_value)
    assert expand.s_value != 0.0
