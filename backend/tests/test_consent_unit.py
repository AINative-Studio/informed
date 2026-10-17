"""Unit tests for the consent ledger and the versioned consent wording (14.2).

The BDD suite (`tests/features/consent_export_delete.feature`) covers the
behaviour a voter and an auditor see through HTTP. This file covers the things
only reachable below it: the content-addressed id's collision properties, the
append-only guarantee at the ledger level, the "latest event wins" reading of
current consent, and the rule that the wording a record points at must always
be resolvable.

No real voter data anywhere: every profile ref is a synthetic HMAC-shaped
string, and nothing in a consent record can hold an interest by construction.
"""

from __future__ import annotations

import logging

import pytest

from app.bootstrap import INFRASTRUCTURE_TABLES, TABLE_SCHEMAS
from app.profiles.consent import (
    CONSENT_TABLE,
    CONSENT_VERSIONS,
    CURRENT_CONSENT_VERSION,
    LAWFUL_BASIS,
    ConsentLedger,
    ConsentRecord,
    UnknownConsentVersionError,
    consent_offer,
    consent_record_id,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: Two synthetic profile refs, shaped like the real `vp:{hmac}` ids but not
#: derived from any user id. The ledger never sees a user id at all, so there
#: is nothing here to be careful with beyond the shape.
REF_A = "vp:" + "a" * 64
REF_B = "vp:" + "b" * 64


@pytest.fixture
def client() -> ZeroDBClient:
    zdb = ZeroDBClient(
        transport=FakeZeroDBTransport(), project_id="proj-test", sleep=lambda _: None
    )
    zdb.create_table(CONSENT_TABLE, TABLE_SCHEMAS[CONSENT_TABLE])
    return zdb


@pytest.fixture
def ledger(client: ZeroDBClient) -> ConsentLedger:
    return ConsentLedger(client)


# --- the consent wording ------------------------------------------------------


def test_the_current_version_is_the_newest_one_defined() -> None:
    """Derived, not a second constant that could fall behind."""
    # Written newest-first to satisfy ruff's SIM300 (an all-caps name reads as
    # the literal side of a comparison to it); the subject is still
    # CURRENT_CONSENT_VERSION.
    newest = list(CONSENT_VERSIONS)[-1]
    assert newest == CURRENT_CONSENT_VERSION
    assert CURRENT_CONSENT_VERSION in CONSENT_VERSIONS


def test_every_consent_version_names_political_opinions_explicitly() -> None:
    """GDPR explicit consent must be specific about the special category.

    Asserted over every version, not just the current one, so a future wording
    revision that softened this into "your preferences" fails here rather than
    shipping.
    """
    for version, text in CONSENT_VERSIONS.items():
        body = " ".join([text.summary, *text.what_is_stored]).lower()
        assert "political" in body, version
        assert "special-category" in body or "special category" in body, version


def test_every_consent_version_offers_anonymous_mode_in_the_same_object() -> None:
    """Story 14.2: "anonymous mode is offered as the alternative"."""
    for version, text in CONSENT_VERSIONS.items():
        assert "anonymous" in text.anonymous_alternative.lower(), version
        assert text.anonymous_alternative.strip(), version


def test_every_consent_version_names_export_and_delete() -> None:
    """The rights that make the consent withdrawable in practice (14.4)."""
    for version, text in CONSENT_VERSIONS.items():
        rights = " ".join(text.your_rights).lower()
        assert "export" in rights, version
        assert "delete" in rights, version


def test_the_offer_carries_the_lawful_basis() -> None:
    offer = consent_offer()
    assert offer.lawful_basis == LAWFUL_BASIS
    assert offer.consent_version == CURRENT_CONSENT_VERSION


def test_an_unknown_consent_version_is_refused_rather_than_defaulted() -> None:
    """A silent fallback would show wording the caller did not ask for."""
    with pytest.raises(UnknownConsentVersionError) as caught:
        consent_offer("1999.0")
    assert "1999.0" in str(caught.value)
    # The message points at the right endpoint to recover from a stale client.
    assert "/v1/profiles/consent" in str(caught.value)


def test_a_superseded_version_stays_resolvable() -> None:
    """Every version ever defined must still render.

    This is what makes an old record meaningful: a ledger row naming 2026.1
    after 2026.2 ships has to resolve to the text that was actually shown.
    """
    for version in CONSENT_VERSIONS:
        assert consent_offer(version).consent_version == version


# --- the id scheme ------------------------------------------------------------


def test_the_id_is_deterministic_for_the_same_event() -> None:
    first = consent_record_id(
        profile_ref=REF_A,
        consent_version="2026.1",
        action="granted",
        recorded_at="2026-10-16T00:00:00Z",
    )
    second = consent_record_id(
        profile_ref=REF_A,
        consent_version="2026.1",
        action="granted",
        recorded_at="2026-10-16T00:00:00Z",
    )
    assert first == second
    assert first.startswith("vc:")
    assert len(first) == len("vc:") + 64


@pytest.mark.parametrize(
    "changed",
    [
        {"profile_ref": REF_B},
        {"consent_version": "2026.2"},
        {"action": "withdrawn"},
        {"recorded_at": "2026-10-16T00:00:01Z"},
    ],
)
def test_changing_any_component_changes_the_id(changed: dict[str, str]) -> None:
    base = {
        "profile_ref": REF_A,
        "consent_version": "2026.1",
        "action": "granted",
        "recorded_at": "2026-10-16T00:00:00Z",
    }
    assert consent_record_id(**base) != consent_record_id(**{**base, **changed})


def test_field_boundaries_cannot_be_shifted_into_a_collision() -> None:
    """`("ab", "c")` must not hash the same as `("a", "bc")`.

    A plain concatenation would collide here, which would let two genuinely
    different consent events share one ledger row. The separator byte is what
    prevents it, and this is the test that pins it.
    """
    one = consent_record_id(
        profile_ref="vp:ab", consent_version="c", action="granted", recorded_at="t"
    )
    other = consent_record_id(
        profile_ref="vp:a", consent_version="bc", action="granted", recorded_at="t"
    )
    assert one != other


# --- the ledger ---------------------------------------------------------------


def test_recording_consent_stores_one_row_with_the_lawful_basis(
    ledger: ConsentLedger, client: ZeroDBClient
) -> None:
    record = ledger.record(profile_ref=REF_A, consent_version=CURRENT_CONSENT_VERSION)
    rows = client.query_rows(CONSENT_TABLE, limit=10).rows
    assert len(rows) == 1
    stored = ConsentRecord.model_validate(rows[0].row_data)
    assert stored == record
    assert stored.lawful_basis == LAWFUL_BASIS
    assert stored.action == "granted"
    assert stored.profile_ref == REF_A


def test_recording_the_identical_event_twice_is_idempotent(
    ledger: ConsentLedger, client: ZeroDBClient
) -> None:
    """Content-addressed ids make a retried write land on the existing row."""
    when = "2026-10-16T00:00:00Z"
    first = ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at=when)
    second = ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at=when)
    assert first.id == second.id
    assert len(client.query_rows(CONSENT_TABLE, limit=10).rows) == 1


def test_a_later_consent_appends_rather_than_replacing(ledger: ConsentLedger) -> None:
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at="2026-10-16T00:00:00Z")
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at="2026-10-17T00:00:00Z")
    history = ledger.history(REF_A)
    assert [record.recorded_at for record in history] == [
        "2026-10-16T00:00:00Z",
        "2026-10-17T00:00:00Z",
    ]


def test_a_withdrawal_appends_and_leaves_the_grant_in_place(ledger: ConsentLedger) -> None:
    """datamodel.md's append-only rule, applied to consent.

    The grant has to survive the withdrawal: it is the proof that the
    processing up to that moment was lawful.
    """
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at="2026-10-16T00:00:00Z")
    ledger.record(
        profile_ref=REF_A,
        consent_version="2026.1",
        action="withdrawn",
        recorded_at="2026-10-18T00:00:00Z",
    )
    actions = [record.action for record in ledger.history(REF_A)]
    assert actions == ["granted", "withdrawn"]


def test_history_is_ordered_oldest_first_regardless_of_write_order(
    ledger: ConsentLedger,
) -> None:
    """ZeroDB documents no row order, so the ledger sorts rather than trusting."""
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at="2026-12-01T00:00:00Z")
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at="2026-01-01T00:00:00Z")
    times = [record.recorded_at for record in ledger.history(REF_A)]
    assert times == sorted(times)


def test_a_grant_and_a_withdrawal_in_the_same_second_read_in_the_real_order(
    ledger: ConsentLedger,
) -> None:
    """A regression test for a bug this ordering actually had.

    `recorded_at` is second-resolution, so a voter who saves and immediately
    deletes produces two events with the SAME timestamp. The first version of
    `history` broke that tie on the sha256 id — arbitrarily — and happened to
    return ["withdrawn", "granted"], which made `current_grant` report live
    consent for a voter who had just erased everything. See
    `ConsentLedger.history`'s docstring.
    """
    same_second = "2026-10-16T12:00:00Z"
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at=same_second)
    ledger.record(
        profile_ref=REF_A,
        consent_version="2026.1",
        action="withdrawn",
        recorded_at=same_second,
    )
    assert [record.action for record in ledger.history(REF_A)] == ["granted", "withdrawn"]
    # The consequence that made the bug matter, pinned directly.
    assert ledger.current_grant(REF_A) is None


def test_the_same_second_ordering_holds_whichever_way_the_rows_come_back(
    ledger: ConsentLedger,
) -> None:
    """Written withdrawal-first, which is not a real sequence but is exactly
    what an arbitrary storage order looks like from the reader's side. The
    ledger must still present the only ordering that can have happened."""
    same_second = "2026-10-16T12:00:00Z"
    ledger.record(
        profile_ref=REF_A,
        consent_version="2026.1",
        action="withdrawn",
        recorded_at=same_second,
    )
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at=same_second)
    assert [record.action for record in ledger.history(REF_A)] == ["granted", "withdrawn"]


def test_one_voters_history_never_includes_anothers(ledger: ConsentLedger) -> None:
    ledger.record(profile_ref=REF_A, consent_version="2026.1")
    ledger.record(profile_ref=REF_B, consent_version="2026.1")
    assert [record.profile_ref for record in ledger.history(REF_A)] == [REF_A]
    assert [record.profile_ref for record in ledger.history(REF_B)] == [REF_B]


def test_history_refilters_even_when_the_server_side_filter_is_ignored() -> None:
    """Story 1.2 found the live API can ignore a `filter`. A cross-voter leak
    in a consent ledger would be a worse bug than a duplicate row, so
    `history` re-filters client-side — and this test breaks the fake's filter
    to prove it does."""
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(transport=transport, project_id="proj-test", sleep=lambda _: None)
    client.create_table(CONSENT_TABLE, TABLE_SCHEMAS[CONSENT_TABLE])
    ledger = ConsentLedger(client)

    ledger.record(profile_ref=REF_A, consent_version="2026.1")
    ledger.record(profile_ref=REF_B, consent_version="2026.1")

    assert [record.profile_ref for record in ledger.history(REF_A)] == [REF_A]


def test_recording_an_unknown_version_writes_nothing(
    ledger: ConsentLedger, client: ZeroDBClient
) -> None:
    with pytest.raises(UnknownConsentVersionError):
        ledger.record(profile_ref=REF_A, consent_version="1999.0")
    assert client.query_rows(CONSENT_TABLE, limit=10).rows == []


# --- current consent ----------------------------------------------------------


def test_no_consent_recorded_means_no_current_grant(ledger: ConsentLedger) -> None:
    assert ledger.current_grant(REF_A) is None


def test_a_grant_is_the_current_consent(ledger: ConsentLedger) -> None:
    ledger.record(profile_ref=REF_A, consent_version="2026.1")
    grant = ledger.current_grant(REF_A)
    assert grant is not None
    assert grant.action == "granted"


def test_a_withdrawal_means_there_is_no_current_grant(ledger: ConsentLedger) -> None:
    """The LATEST event decides, not the newest grant.

    A reader that scanned for the most recent `granted` row would report live
    consent for a voter who withdrew it afterwards — which is the one wrong
    answer this method must never give.
    """
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at="2026-10-16T00:00:00Z")
    ledger.record(
        profile_ref=REF_A,
        consent_version="2026.1",
        action="withdrawn",
        recorded_at="2026-10-17T00:00:00Z",
    )
    assert ledger.current_grant(REF_A) is None


def test_re_consenting_after_a_withdrawal_restores_current_consent(
    ledger: ConsentLedger,
) -> None:
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at="2026-10-16T00:00:00Z")
    ledger.record(
        profile_ref=REF_A,
        consent_version="2026.1",
        action="withdrawn",
        recorded_at="2026-10-17T00:00:00Z",
    )
    ledger.record(profile_ref=REF_A, consent_version="2026.1", recorded_at="2026-10-18T00:00:00Z")
    grant = ledger.current_grant(REF_A)
    assert grant is not None
    assert grant.recorded_at == "2026-10-18T00:00:00Z"
    # All three events survive: the ledger is a history, not a state field.
    assert len(ledger.history(REF_A)) == 3


# --- what must never be stored or logged --------------------------------------


def test_a_consent_record_cannot_hold_an_interest() -> None:
    """Enforced by the model, not by discipline.

    `extra="forbid"` means a future caller who tried to attach interests to a
    consent record gets a ValidationError rather than a stored political
    opinion in an unencrypted table.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConsentRecord(
            id="vc:" + "0" * 64,
            profile_ref=REF_A,
            consent_version="2026.1",
            action="granted",
            recorded_at="2026-10-16T00:00:00Z",
            interests=[{"topic_id": "test:anything:v1"}],  # type: ignore[call-arg]
        )


def test_the_profile_ref_is_never_logged(
    ledger: ConsentLedger, caplog: pytest.LogCaptureFixture
) -> None:
    """The HMAC is a stable per-voter handle; a log of it beside a timestamp
    plus an access log is a re-identification path — the same rule
    `VoterProfileService.save` follows for its own log line."""
    with caplog.at_level(logging.DEBUG):
        ledger.record(profile_ref=REF_A, consent_version=CURRENT_CONSENT_VERSION)
        ledger.history(REF_A)
        ledger.current_grant(REF_A)

    captured = "\n".join(record.getMessage() for record in caplog.records)
    assert REF_A not in captured
    assert "a" * 64 not in captured
    # It logs SOMETHING, so the absence above is not just an empty capture.
    assert CURRENT_CONSENT_VERSION in captured


def test_the_ledger_repr_is_redacted(ledger: ConsentLedger) -> None:
    assert repr(ledger) == "ConsentLedger(<redacted>)"


# --- the table is declared where every other one is ---------------------------


def test_the_consent_table_is_a_declared_infrastructure_table() -> None:
    """Not one of datamodel.md's 22, and declared as such.

    If this table were ever counted among the 22, `app.bootstrap`'s own
    assertion would fail — this test states the intent in words so a reader
    does not have to infer it from an arithmetic assertion.
    """
    assert CONSENT_TABLE in TABLE_SCHEMAS
    assert CONSENT_TABLE in INFRASTRUCTURE_TABLES


def test_the_consent_table_schema_has_no_field_that_could_hold_an_opinion() -> None:
    """The same discipline `voter_profiles`' schema follows: a field that
    *could* hold a political opinion in clear text is a field some future code
    will."""
    assert set(TABLE_SCHEMAS[CONSENT_TABLE]) == {
        "id",
        "profile_ref",
        "consent_version",
        "action",
        "lawful_basis",
        "recorded_at",
    }


def test_the_schema_matches_the_model_exactly() -> None:
    """A field on the model with no column, or the reverse, is the bug that
    shows up only as a live 400 or a silently-dropped value."""
    assert set(TABLE_SCHEMAS[CONSENT_TABLE]) == set(ConsentRecord.model_fields)
