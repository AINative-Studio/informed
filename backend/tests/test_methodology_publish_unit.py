"""Unit tests for `app.methodology.publish` (story 6.1).

The feature file covers the behaviour a methodology owner cares about. These
cover the edges a Gherkin scenario would read badly for: the exact numbers
against prd.md's prose, the append-only guarantee at the field level, and the
ordering guarantee that makes a torn publish recoverable.
"""

from __future__ import annotations

import pytest

from app.bootstrap import Bootstrapper
from app.integrity.checks import check_current_uniqueness
from app.methodology.models import DOCUMENTED_WEIGHT_GROUPS
from app.methodology.publish import (
    DEFAULT_EVIDENCE_BLEND,
    DEFAULT_VOTE_TYPE_WEIGHTS,
    METHODOLOGY_PUBLISHED_TOPIC,
    MethodologyPublisher,
    MethodologyPublishError,
    default_weights,
)
from app.methodology.service import PublicMethodologyService
from app.profiles.methodology import (
    DEFAULT_TIER_WEIGHTS,
    TABLE_NAME,
    MethodologyVersionService,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: datamodel.md: "`vote_type` takes `final_passage`, `amendment`, `cloture`,
#: `motion_to_recommit`, `procedural` or `other`". Spelled out here rather than
#: imported from `app.repositories.models` so that a change to either the enum
#: or this weight map has to be a deliberate change to both.
DOCUMENTED_VOTE_TYPES = frozenset(
    {"final_passage", "amendment", "cloture", "motion_to_recommit", "procedural", "other"}
)


@pytest.fixture
def client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    zerodb = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(zerodb, pace=lambda: None).run()
    return zerodb


@pytest.fixture
def publisher(client: ZeroDBClient) -> MethodologyPublisher:
    return MethodologyPublisher(client)


def _rows(client: ZeroDBClient) -> list[dict[str, object]]:
    return [row.row_data for row in client.query_rows(TABLE_NAME, limit=500).rows]


# --- the weights against prd.md -------------------------------------------


def test_the_evidence_blend_is_prd_mds_seventy_fifteen_fifteen() -> None:
    """prd.md, Feature 3: "votes 70%, sponsorships 15%, stated positions 15%"."""
    assert DEFAULT_EVIDENCE_BLEND == {"votes": 0.7, "sponsorships": 0.15, "statements": 0.15}


def test_the_evidence_blend_sums_to_one() -> None:
    """A blend that did not sum to 1 would scale every incumbent's position up
    or down relative to a challenger's, whose position comes from promises
    alone. prd.md's nonpartisan-by-construction rule means the two have to be
    on the same scale."""
    assert sum(DEFAULT_EVIDENCE_BLEND.values()) == pytest.approx(1.0)


def test_the_evidence_blend_is_stored_as_fractions_not_percentages() -> None:
    """Every weight is in (0, 1], so a scorer multiplies by it directly rather
    than having to remember to divide by 100."""
    for name, weight in DEFAULT_EVIDENCE_BLEND.items():
        assert 0 < weight <= 1, f"{name} = {weight} is not a fraction"


def test_the_vote_type_weights_cover_datamodel_mds_exact_enum() -> None:
    assert set(DEFAULT_VOTE_TYPE_WEIGHTS) == DOCUMENTED_VOTE_TYPES


def test_final_passage_is_the_heaviest_vote_type() -> None:
    """backlog.md 6.2: "Final passage outweighs procedural and bundled votes"."""
    heaviest = max(DEFAULT_VOTE_TYPE_WEIGHTS, key=lambda name: DEFAULT_VOTE_TYPE_WEIGHTS[name])
    assert heaviest == "final_passage"
    assert DEFAULT_VOTE_TYPE_WEIGHTS["final_passage"] == 1.0


def test_procedural_and_cloture_weigh_less_than_final_passage() -> None:
    """prd.md, Feature 2: "Procedural and cloture votes are shown but weighted
    lower than final passage"."""
    final = DEFAULT_VOTE_TYPE_WEIGHTS["final_passage"]
    assert DEFAULT_VOTE_TYPE_WEIGHTS["cloture"] < final
    assert DEFAULT_VOTE_TYPE_WEIGHTS["procedural"] < final


def test_no_vote_type_is_weighted_zero() -> None:
    """prd.md says procedural votes are "shown but weighted lower", not
    excluded. A zero weight would let the page display a vote that the scorer
    did not count at all, which is the one outcome worse than omitting it."""
    for name, weight in DEFAULT_VOTE_TYPE_WEIGHTS.items():
        assert weight > 0, f"{name} is weighted zero, which hides it from the scorer"


def test_every_vote_type_weight_is_a_usable_multiplier() -> None:
    for name, weight in DEFAULT_VOTE_TYPE_WEIGHTS.items():
        assert 0 < weight <= 1.0, f"{name} = {weight}"


def test_substantive_votes_outweigh_procedural_ones() -> None:
    """The ordering the module docstring argues for, pinned as an ordering
    rather than as six separate magic numbers — so a future re-weighting is
    free to change the figures and still has to preserve the reasoning."""
    weights = DEFAULT_VOTE_TYPE_WEIGHTS
    assert weights["final_passage"] > weights["amendment"]
    assert weights["amendment"] > weights["motion_to_recommit"]
    assert weights["motion_to_recommit"] > weights["cloture"]
    assert weights["cloture"] > weights["procedural"]


def test_an_unclassified_vote_is_weighted_no_higher_than_a_procedural_one() -> None:
    """`other` is what the ingest classifier assigns when it is unsure, so it
    must not be stronger evidence than the weakest thing it could be."""
    assert DEFAULT_VOTE_TYPE_WEIGHTS["other"] <= DEFAULT_VOTE_TYPE_WEIGHTS["procedural"]


def test_default_weights_carries_every_documented_group() -> None:
    assert sorted(default_weights()) == sorted(DOCUMENTED_WEIGHT_GROUPS)


def test_default_weights_keeps_story_4_2s_tier_numbers() -> None:
    """This story must not quietly re-weight the tiers while adding two groups
    beside them."""
    assert default_weights()["tiers"] == DEFAULT_TIER_WEIGHTS


def test_default_weights_returns_copies_a_caller_cannot_corrupt() -> None:
    mutated = default_weights()
    mutated["tiers"]["primary"] = 99.0
    assert DEFAULT_TIER_WEIGHTS["primary"] == 3.0
    assert default_weights()["tiers"]["primary"] == 3.0


# --- publishing -----------------------------------------------------------


def test_publishing_writes_all_three_weight_groups(publisher: MethodologyPublisher) -> None:
    published = publisher.publish_new_version("2027.1")
    assert sorted(published.weights) == sorted(DOCUMENTED_WEIGHT_GROUPS)


def test_publishing_emits_exactly_one_event_naming_the_version(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    publisher.publish_new_version("2027.1")
    events = client.list_events(topic=METHODOLOGY_PUBLISHED_TOPIC, limit=50).events
    assert len(events) == 1
    assert events[0].event_payload == {"version": "2027.1"}


def test_the_event_payload_is_only_the_version(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    """datamodel.md's Events table gives this event the payload `version`. An
    event fans out to consumers this codebase does not control, so it carries
    the minimum a consumer needs in order to read the row itself."""
    publisher.publish_new_version("2027.1")
    (event,) = client.list_events(topic=METHODOLOGY_PUBLISHED_TOPIC, limit=50).events
    assert set(event.event_payload) == {"version"}


def test_publishing_leaves_exactly_one_current_row(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    MethodologyVersionService(client).current_version()
    publisher.publish_new_version("2027.1")
    current = [row for row in _rows(client) if row.get("is_current") is True]
    assert [row["id"] for row in current] == ["2027.1"]


def test_the_superseded_row_keeps_every_field_but_the_flag(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    """The append-only guarantee at field level.

    datamodel.md: "never update history in place". The supersede is a flag
    flip and nothing else — a bare `PUT {"is_current": false}` would replace
    the row wholesale and destroy exactly the history this table exists to
    keep (confirmed live in story 1.2).
    """
    seeded = MethodologyVersionService(client).current_version()
    before = dict(client.get_row(TABLE_NAME, seeded.id).row_data)  # type: ignore[union-attr]

    publisher.publish_new_version("2027.1")

    after = dict(client.get_row(TABLE_NAME, seeded.id).row_data)  # type: ignore[union-attr]
    assert after["is_current"] is False
    assert after["weights"] == before["weights"]
    assert after["published_at"] == before["published_at"]
    assert after["changelog_url"] == before["changelog_url"]


def test_a_superseded_version_still_publishes_its_own_weights(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    """The promise `docs/methodology-changelog.md` makes to a reader: a score
    computed under `2026.1` can still show the weights it was computed with."""
    MethodologyVersionService(client).current_version()
    publisher.publish_new_version("2027.1")
    old = PublicMethodologyService(client).version("2026.1")
    assert old.is_current is False
    assert old.weights["tiers"] == DEFAULT_TIER_WEIGHTS


def test_publishing_repairs_a_table_that_already_had_two_current_rows(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    """A torn previous publish (new row written, old flag not yet cleared) is
    repaired by the next publish, because `_clear_previous_current` clears
    EVERY previously-current row rather than only the newest — the same
    reasoning as `Repository.append_version`."""
    MethodologyVersionService(client).current_version()
    client.upsert_row(
        TABLE_NAME,
        "2027.1",
        {
            "id": "2027.1",
            "published_at": "2027-01-01T00:00:00Z",
            "weights": default_weights(),
            "changelog_url": "https://informed.example/methodology/2027.1",
            "is_current": True,
        },
    )
    assert not check_current_uniqueness(client).ok

    publisher.publish_new_version("2028.1")

    assert check_current_uniqueness(client).ok
    current = [row for row in _rows(client) if row.get("is_current") is True]
    assert [row["id"] for row in current] == ["2028.1"]


def test_publishing_defaults_the_changelog_url_to_this_versions_anchor(
    publisher: MethodologyPublisher,
) -> None:
    published = publisher.publish_new_version("2027.1")
    assert published.changelog_url.endswith("methodology-changelog.md#20271")
    assert published.changelog_url.startswith("https://")


def test_an_explicit_changelog_url_and_date_are_honoured(
    publisher: MethodologyPublisher,
) -> None:
    published = publisher.publish_new_version(
        "2027.1",
        changelog_url="https://example.test/m/2027.1",
        published_at="2027-01-01T00:00:00Z",
    )
    assert published.changelog_url == "https://example.test/m/2027.1"
    assert published.published_at == "2027-01-01T00:00:00Z"


# --- refusals: nothing written, nothing emitted ---------------------------


@pytest.mark.parametrize("version", ["v2", "2026", "2026.", "26.1", "2026.1.1", "", "latest"])
def test_a_version_id_off_datamodel_mds_pattern_is_refused(
    publisher: MethodologyPublisher, client: ZeroDBClient, version: str
) -> None:
    with pytest.raises(MethodologyPublishError):
        publisher.publish_new_version(version)
    assert _rows(client) == []
    assert client.list_events(topic=METHODOLOGY_PUBLISHED_TOPIC, limit=50).events == []


def test_republishing_the_version_in_force_is_refused(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    """Idempotence here would be a lie: a second publish of the id already in
    force emits a second event and triggers a second full recompute for no
    change. A methodology change is a NEW version id."""
    MethodologyVersionService(client).current_version()
    with pytest.raises(MethodologyPublishError, match="already the version in force"):
        publisher.publish_new_version("2026.1")
    assert client.list_events(topic=METHODOLOGY_PUBLISHED_TOPIC, limit=50).events == []


def test_republishing_a_superseded_version_id_is_allowed(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    """The refusal above is about the version in FORCE, not about any id ever
    used. Re-promoting a superseded methodology is a legitimate rollback — the
    weights it carries are still the published ones for that id — so it is a
    publication like any other and emits its own event."""
    MethodologyVersionService(client).current_version()
    publisher.publish_new_version("2027.1")
    publisher.publish_new_version("2026.1", weights=default_weights())
    assert MethodologyVersionService(client).current_version().id == "2026.1"
    assert len(client.list_events(topic=METHODOLOGY_PUBLISHED_TOPIC, limit=50).events) == 2


@pytest.mark.parametrize("missing", ["tiers", "evidence_blend", "vote_types"])
def test_a_version_missing_a_documented_weight_group_is_refused(
    publisher: MethodologyPublisher, client: ZeroDBClient, missing: str
) -> None:
    weights = default_weights()
    del weights[missing]
    with pytest.raises(MethodologyPublishError, match=missing):
        publisher.publish_new_version("2027.1", weights=weights)
    assert _rows(client) == []


def test_a_version_with_an_empty_weight_group_is_refused(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    """`{}` is not a published weighting — the same rule
    `PublishedMethodology.weights_not_yet_published` applies on the read
    side, enforced here on the write side so the two cannot disagree."""
    weights = default_weights()
    weights["vote_types"] = {}
    with pytest.raises(MethodologyPublishError, match="vote_types"):
        publisher.publish_new_version("2027.1", weights=weights)
    assert _rows(client) == []


# --- a seed is not a publication ------------------------------------------


def test_seeding_the_default_emits_no_event(client: ZeroDBClient) -> None:
    """Story 4.2's seed establishes the baseline rather than changing it, so
    there is nothing for `methodology.published`'s "full recompute" consumer to
    recompute — no score was ever computed under a previous version."""
    MethodologyVersionService(client).current_version()
    assert client.list_events(topic=METHODOLOGY_PUBLISHED_TOPIC, limit=50).events == []


def test_the_public_page_reports_nothing_unpublished_after_a_real_publish(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    """The gap story 12.2 documented and this story closes: before 6.1, the
    live row carried `tiers` only and the page correctly reported the other two
    groups in `weights_not_yet_published`."""
    seeded = PublicMethodologyService(client).current()
    assert sorted(seeded.weights_not_yet_published) == ["evidence_blend", "vote_types"]

    publisher.publish_new_version("2027.1")

    assert PublicMethodologyService(client).current().weights_not_yet_published == []


# --- the integrity invariant ----------------------------------------------


def test_the_check_passes_on_a_published_table(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    publisher.publish_new_version("2027.1")
    assert check_current_uniqueness(client).ok


def test_the_check_ignores_an_empty_methodology_table(client: ZeroDBClient) -> None:
    """A project that has published no methodology yet has nothing to be wrong
    about, and story 4.2 seeds a row the first time anything reads one — so
    "empty" is a pre-first-read state, not a broken one. Reporting it would
    fail the nightly check on every freshly bootstrapped environment."""
    report = check_current_uniqueness(client)
    assert report.ok
    assert not [v for v in report.violations if v.table == TABLE_NAME]


def test_the_check_catches_a_table_with_no_current_version(client: ZeroDBClient) -> None:
    MethodologyVersionService(client).current_version()
    client.update_row(TABLE_NAME, "2026.1", {"is_current": False})
    violations = [v for v in check_current_uniqueness(client).violations if v.table == TABLE_NAME]
    assert len(violations) == 1
    assert "expected exactly 1" in violations[0].detail


def test_the_check_counts_the_methodology_rows_it_scanned(
    publisher: MethodologyPublisher, client: ZeroDBClient
) -> None:
    """The report's `scanned` has to include this table, or a nightly run would
    report a check that read nothing as having passed."""
    before = check_current_uniqueness(client).scanned
    publisher.publish_new_version("2027.1")
    assert check_current_uniqueness(client).scanned == before + 1
