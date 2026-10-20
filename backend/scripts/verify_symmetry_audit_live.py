#!/usr/bin/env python3
"""Verify the symmetry audit against the LIVE "informed" project (story 12.1).

Every story from 1.3 on has found at least one discrepancy between what the
in-memory fake agrees to and what ZeroDB actually does, so this story's core
claim — the real scoring pipeline treats the two directions of a political
axis identically — is checked against the real API as well as the fake.

!!! THIS SCRIPT WRITES SYNTHETIC TEST DATA AND DELETES IT AGAIN !!!

The audit itself does the writing and the deleting (see
`app.neutrality.audit`'s "THE SYNTHETIC DATA" section); this script's extra
job is to PROVE the cleanup worked, by re-reading every id afterwards. A
`candidate_positions` row is a claim about how a named person voted, and
fabricating one for a real incumbent would be the worst thing this codebase
could put in its database — so everything carries `SYMMETRY-AUDIT` in its own
id, following the convention `verify_alignment_scoring_live.py` established
for story 6.2.

The one row deliberately KEPT is the `audit_runs` row. That is the story's own
acceptance criterion ("results are written to `audit_runs`"), it names no
person, and it is the record the release gate reads.

What it proves, in order:

  1. **`audit_runs` accepts the row shape this story writes**, against the
     real schema. Nothing had ever written to this table — the columns existed
     in `app.bootstrap.TABLE_SCHEMAS` and no code had used one.
  2. **`max_delta` and `passed` round-trip as a real float and a real bool.**
     The hazard story 12.2 pinned for tier weights: `0.0` arriving back as
     `"0.0"`, or `False` as `"false"`, would make the release gate read a
     failing audit as passing — the single worst bug this story could have.
  3. **The real pipeline is symmetric over live ZeroDB.** Positions computed
     by the real `PositionComputer` from live rows, scores from the real
     `AlignmentService`, compared. This is the check the fake cannot make
     meaningfully: live floats make a round trip through JSON and a database,
     and the in-memory fake's do not.
  4. **The audit's own failing path still works live.** A deliberate skew is
     injected and MUST be caught — the same self-check
     `scripts/check_panel_gate.py` runs for its own gate, and for the same
     reason: a neutrality audit whose failing path is broken is worse than no
     audit.
  5. **The release gate blocks and allows correctly**, reading the live
     `audit_runs` row.
  6. **Every synthetic row is gone**, confirmed by a live re-read of each id —
     including the `candidate_positions` rows the audit fabricated, which is
     the set that matters most.
  7. **No live row links a synthetic voter to a candidate.** datamodel.md: "no
     table links a person's priorities to a candidate."

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    export ZERODB_PROFILE_ROOT_KEY="$(
      python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
    )"
    python backend/scripts/verify_symmetry_audit_live.py

The root key is generated fresh for the run and never persisted, the same way
`verify_alignment_scoring_live.py` does it: the two throwaway profiles this
writes are unreadable the moment the shell exits, which is belt and braces
alongside the delete. If `ZERODB_PROFILE_ROOT_KEY` is already set this script
leaves it alone — it never generates one over a real key, because doing so
would make an existing voter's row undecryptable.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.neutrality.audit import (  # noqa: E402
    MARKER,
    SYMMETRY_TOLERANCE,
    SymmetryAuditor,
    audit_runs_table_row_id,
    check_symmetry_gate,
    default_pairs,
)
from app.profiles.crypto import ProfileKeyring, ProfileRootKey  # noqa: E402
from app.profiles.methodology import MethodologyVersionService  # noqa: E402
from app.profiles.service import VoterProfileService  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402

#: Tables the audit touches and this script re-reads to confirm cleanup.
_TOUCHED = (
    "candidate_positions",
    "member_votes",
    "roll_calls",
    "stances",
    "issue_topics",
    "people",
    "candidacies",
    "districts",
    "elections",
    "provenance_log",
    "voter_profiles",
)


class Failure(AssertionError):
    """A live check failed. Raised rather than printed so cleanup still runs."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)
    print(f"  ok   {message}")


def _profiles(client: ZeroDBClient) -> VoterProfileService:
    return VoterProfileService(
        client=client,
        keyring=ProfileKeyring(client=client, root_key=ProfileRootKey.from_env()),
    )


def _synthetic_rows(client: ZeroDBClient) -> dict[str, list[str]]:
    """Every live row in a touched table whose serialised form mentions the
    marker. A blunt instrument deliberately: the point is to catch anything
    the audit wrote and failed to remove, including a row this script's author
    did not think of.
    """
    found: dict[str, list[str]] = {}
    for table in _TOUCHED:
        ids = [
            str(row.row_data.get("id"))
            for row in client.query_rows(table, limit=500).rows
            if MARKER.lower() in str(row.row_data).lower()
        ]
        if ids:
            found[table] = sorted(ids)
    return found


# --- the checks ------------------------------------------------------------


def verify_clean_start(client: ZeroDBClient) -> None:
    print("\n[1] the live project holds no leftover synthetic audit data")
    leftover = _synthetic_rows(client)
    check(
        leftover == {},
        f"no SYMMETRY-AUDIT row is present before the run (found {leftover})",
    )


def verify_the_audit(client: ZeroDBClient, profiles: VoterProfileService) -> Any:
    print("\n[2] running the real audit against live ZeroDB")
    version = MethodologyVersionService(client).current_version()
    print(f"  info  live current methodology: {version.id}")
    print(f"  info  live tier weights: {version.tier_weights}")

    report = SymmetryAuditor(client=client, profiles=profiles).run()

    print(f"\n  >>> {report.summary()}")
    for pair in report.pairs:
        print(f"\n  pair {pair.name!r}:")
        print(f"    candidate L score ............ {pair.left_score}")
        print(f"    candidate R score (mirrored) . {pair.right_score}")
        if pair.left_score is not None and pair.right_score is not None:
            print(f"    L + R ........................ {pair.left_score + pair.right_score}")
        print(f"    mirrored voter / mirrored R .. {pair.mirrored_voter_score}")
        print(f"    badges ....................... {pair.left_badge} / {pair.right_badge}")
        print(
            f"    evidence ..................... "
            f"{pair.left_evidence_count} / {pair.right_evidence_count}"
        )
        print(
            f"    dealbreaker warnings ......... "
            f"{pair.left_warning_count} / {pair.right_warning_count}"
        )
        print(f"    delta ........................ {pair.delta}")

    print("\n[3] the symmetry relationships prd.md requires, over live rows")
    check(
        len(report.pairs) == len(default_pairs()),
        f"every mirrored pair was scored ({len(report.pairs)} of {len(default_pairs())})",
    )
    for pair in report.pairs:
        check(
            pair.left_score is not None and pair.right_score is not None,
            f"{pair.name}: both candidates got a real score from live positions",
        )
        assert pair.left_score is not None and pair.right_score is not None
        total = pair.left_score + pair.right_score
        check(
            abs(total - 100.0) <= SYMMETRY_TOLERANCE,
            f"{pair.name}: the two mirrored candidates' scores sum to {total}, within "
            f"{SYMMETRY_TOLERANCE} of 100",
        )
        check(
            pair.mirrored_voter_score is not None,
            f"{pair.name}: the mirrored voter got a real score",
        )
        assert pair.mirrored_voter_score is not None
        delta = abs(pair.left_score - pair.mirrored_voter_score)
        check(
            delta <= SYMMETRY_TOLERANCE,
            f"{pair.name}: the mirrored voter's score for its aligned candidate "
            f"({pair.mirrored_voter_score}) is within {SYMMETRY_TOLERANCE} of the original "
            f"voter's ({pair.left_score}) — prd.md's own scenario",
        )
        check(
            pair.left_badge == pair.right_badge,
            f"{pair.name}: both candidates carry the same confidence badge ({pair.left_badge})",
        )
        check(
            pair.left_evidence_count == pair.right_evidence_count,
            f"{pair.name}: both rest on the same amount of evidence ({pair.left_evidence_count})",
        )
        check(
            pair.left_reason_stance_ids == pair.right_reason_stance_ids,
            f"{pair.name}: the top reasons name the same interests",
        )
        for stance_id, direction in pair.left_reason_directions.items():
            other = pair.right_reason_directions.get(stance_id)
            check(
                other is not None and other != direction,
                f"{pair.name}: reason {stance_id} moved the two candidates in opposite "
                f"directions ({direction} vs {other})",
            )
        if pair.has_dealbreaker:
            check(
                (pair.left_warning_count > 0) != (pair.right_warning_count > 0),
                f"{pair.name}: the dealbreaker warning fired on exactly one side",
            )
        check(pair.symmetric, f"{pair.name}: symmetric on every comparison")

    check(report.passed, f"the live audit passed: {report.summary()}")
    check(
        report.max_delta == 0.0,
        f"the live max delta is exactly 0.0, not merely under the tolerance "
        f"(got {report.max_delta}) — a nonzero delta on a healthy pipeline would mean "
        f"something is diverging and happening to stay inside 0.5",
    )
    return report


def verify_the_audit_runs_row(client: ZeroDBClient, report: Any) -> None:
    print("\n[4] the audit_runs row, live (story 12.1's own acceptance criterion)")
    row_id = audit_runs_table_row_id(report.run_at)
    stored = client.get_row("audit_runs", row_id)
    check(stored is not None, f"the audit_runs row {row_id} wrote and reads back live")
    assert stored is not None
    data = stored.row_data
    print(f"  info  live row: {data}")

    check(
        set(data)
        == {
            "id",
            "run_at",
            "methodology_version",
            "max_delta",
            "passed",
            "report_file_id",
        },
        f"it carries exactly datamodel.md's documented fields (got {sorted(set(data))})",
    )
    check(
        isinstance(data["max_delta"], float),
        f"max_delta round-trips as a real float, not a string "
        f"(got {type(data['max_delta']).__name__})",
    )
    check(
        isinstance(data["passed"], bool),
        f"passed round-trips as a real bool, not a string "
        f"(got {type(data['passed']).__name__}) — a string 'false' is truthy, and the "
        f"release gate reads this field",
    )
    check(data["passed"] is True, "it records that the live audit passed")
    check(
        data["methodology_version"] == report.methodology_version,
        f"it names the methodology that scored it ({data['methodology_version']})",
    )
    for forbidden in ("stance_id", "tier", "intensity", "interests", "vp:", "payload"):
        check(
            forbidden not in str(data),
            f"{forbidden!r} does not appear in the audit_runs row",
        )


def verify_the_failing_path(client: ZeroDBClient, profiles: VoterProfileService) -> None:
    print("\n[5] the audit's failing path, live (the self-check)")
    skewed = SymmetryAuditor(client=client, profiles=profiles, skew=8.0).run()
    check(
        not skewed.passed,
        f"an 8-point skew is caught live (max delta {skewed.max_delta:.4f} > {SYMMETRY_TOLERANCE})",
    )
    check(
        bool(skewed.asymmetries),
        f"it says what was wrong: {skewed.asymmetries[0] if skewed.asymmetries else '(nothing)'}",
    )
    row_id = audit_runs_table_row_id(skewed.run_at)
    stored = client.get_row("audit_runs", row_id)
    check(stored is not None, "the failing run wrote its own audit_runs row")
    assert stored is not None
    check(
        stored.row_data["passed"] is False,
        "that row records passed=False, so a release gate reading it blocks",
    )


def verify_the_gate(client: ZeroDBClient, profiles: VoterProfileService) -> None:
    print("\n[6] the release gate over the live audit_runs row")
    blocked = check_symmetry_gate(client, profiles=None)
    check(
        not blocked.ok,
        "the gate BLOCKS the release while the stored result is the failing one "
        f"({blocked.summary()})",
    )

    allowed = check_symmetry_gate(client, profiles=profiles)
    check(
        allowed.ok,
        f"a fresh passing audit ALLOWS the release ({allowed.summary()})",
    )
    recorded = check_symmetry_gate(client, profiles=None)
    check(
        recorded.ok,
        f"and the gate agrees reading only the restored stored row ({recorded.summary()})",
    )


def verify_cleanup(client: ZeroDBClient) -> None:
    print("\n[7] confirming every synthetic row is gone")
    leftover = _synthetic_rows(client)
    check(
        leftover == {},
        f"no SYMMETRY-AUDIT row remains in any touched table (found {leftover})",
    )

    positions = [
        str(row.row_data.get("id"))
        for row in client.query_rows("candidate_positions", limit=500).rows
        if MARKER.lower() in str(row.row_data).lower()
    ]
    check(
        positions == [],
        f"no candidate_positions row asserts how a synthetic member voted (found {positions})",
    )

    print("\n[8] no live row links a synthetic voter to a candidate")
    linking = [
        (table, str(row.row_data.get("id")))
        for table in ("candidate_positions", "candidate_cards", "member_votes")
        for row in client.query_rows(table, limit=500).rows
        if "vp:" in str(row.row_data)
    ]
    check(linking == [], f"datamodel.md's no-join rule holds live (found {linking})")

    print("\n[9] the audit_runs row is deliberately KEPT")
    rows = [row.row_data for row in client.query_rows("audit_runs", limit=500).rows]
    check(
        bool(rows),
        f"{len(rows)} audit_runs row(s) remain — the story's own acceptance criterion",
    )
    for row in sorted(rows, key=lambda r: str(r.get("id")))[-3:]:
        print(f"  info  kept: {row}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-clean-start",
        action="store_true",
        help=(
            "do not require the project to be free of SYMMETRY-AUDIT rows before "
            "starting. Debug only, after a previous run was interrupted."
        ),
    )
    args = parser.parse_args()

    # Configured, unlike most verify scripts here, because this one is SLOW
    # against live ZeroDB: the audit writes ~90 rows across three mirrored
    # pairs through the real repositories, against a ~60 req/min ceiling, and
    # with no logging a run looks identical to a hang for several minutes. The
    # app's own INFO lines (each repository write, each alignment computed)
    # are the progress indicator. They carry ids and counts only — see
    # `app.scoring.service`'s note on what its logger may and may not say.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    client = ZeroDBClient.from_env()
    profiles = _profiles(client)
    print(f"live project: {client.project_id}")

    failure: BaseException | None = None
    try:
        if not args.skip_clean_start:
            verify_clean_start(client)
        report = verify_the_audit(client, profiles)
        verify_the_audit_runs_row(client, report)
        verify_the_failing_path(client, profiles)
        verify_the_gate(client, profiles)
        verify_cleanup(client)
    except BaseException as exc:  # noqa: BLE001 — report, then still report cleanup
        failure = exc
        print(f"\nFAILED: {exc}")
        try:
            print("\n--- cleanup state after the failure ---")
            leftover = _synthetic_rows(client)
            print(f"  leftover synthetic rows: {leftover or 'none'}")
        except BaseException as inner:  # noqa: BLE001
            print(f"  could not read cleanup state: {inner}")
    finally:
        client.close()

    if failure is not None:
        return 1
    print(
        "\nall live checks passed: the real scoring pipeline is symmetric over live "
        "ZeroDB, the audit_runs row is recorded, the failing path still works, and "
        "every synthetic row was removed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
