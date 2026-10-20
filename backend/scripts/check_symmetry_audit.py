#!/usr/bin/env python3
"""The symmetry audit release gate (story 12.1).

backlog.md, story 12.1: "a failure blocks the next release." This is that
gate, runnable two ways:

    python scripts/check_symmetry_audit.py            # --fake (the default)
    python scripts/check_symmetry_audit.py --live      # against real ZeroDB

`app.neutrality.audit` holds all the logic; this script is the thin CLI
wrapper around it, in the same spirit as `scripts/check_panel_gate.py` (story
12.5), whose shape it follows deliberately rather than inventing a second
release-blocking mechanism. No logic of its own to assert on, so the Gherkin
feature (`tests/features/symmetry_audit.feature`) and the unit tests are
where this gate's behaviour is proven.

=============================================================================
WHY THIS GATE CAN RUN FULLY IN CI, UNLIKE THE PANEL GATE
=============================================================================

`check_panel_gate.py --fake` has an honest limitation it documents at length:
the real question ("does the LIVE panel_reviews ledger cover the LIVE
corpus?") is about deployed DATA, so the fake can only prove the gate's logic,
not that the live corpus passes.

**This gate does not have that limitation, and the difference matters.** The
question here is "is the scoring CODE symmetric?", and the code in CI is
exactly the code that ships. The fixture is synthetic by design — the audit
mints both mirrored candidates itself (see `app.neutrality.audit`'s module
docstring on why it must), so there is no live data for it to be missing. A
`--fake` run in CI therefore answers the real question completely: it drives
`PositionComputer`, `AlignmentService`, the formula, the badge, the
dealbreaker check and the reason ranking — all the real modules — over an
in-memory ZeroDB, and a partisan skew introduced in any of them turns the
build red on the push that introduced it.

`--live` exists anyway, and is not redundant. It proves the same property
over the REAL store (float round-tripping, the real published methodology's
tier weights, the server-side filter's behaviour) and writes the real
`audit_runs` row the nightly job writes. It needs ZeroDB credentials that
`.github/workflows/ci.yml` does not have — the same gap
`.github/workflows/scheduled-jobs.yml` already documents.

=============================================================================
THE SELF-CHECK, AND WHY IT IS NOT OPTIONAL
=============================================================================

Like `check_panel_gate.py`, `--fake` runs two steps back to back:

  1. **the self-check.** The exact same audit, run with a deliberate 8-point
     skew injected into one side's score AFTER the real pipeline produced it,
     and this step is REQUIRED to fail. If it passes, the audit's failing
     path is broken — someone widened the tolerance, or broke the comparison
     — and a neutrality audit that rubber-stamps a skewed pipeline is worse
     than no audit at all, so this exits 1 rather than reporting a clean pass
     it cannot stand behind.
  2. **the real check**, with no skew. THIS is the number the exit code
     reflects: a healthy build exits 0 because the real pipeline is
     symmetric.

Splitting them is what makes `--fake` usable as a required CI check rather
than one that is permanently and uninformatively red. The self-check proves
failing still works; the real check is what CI gates on.

=============================================================================
WHAT A FAILURE HERE MEANS
=============================================================================

It means the scoring pipeline treats the two directions of a political axis
differently. CLAUDE.md's first non-negotiable is "Nonpartisan by
construction. One scoring codebase for every candidate and party. No
party-specific rules", and a red build here is that promise being broken.
It is not a flaky test to retry and it is not a tolerance to widen: the
healthy delta is 0.0, not "under 0.5" (see `SYMMETRY_TOLERANCE`), so any
nonzero delta is a real divergence somewhere in the pipeline and the
violation lines name which pair and which comparison.

Exit codes:
    0  the audit passed; the release is not blocked
    1  the audit found skew, the self-check failed, or `--live` hit an error
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import Bootstrapper  # noqa: E402
from app.methodology.publish import MethodologyPublisher  # noqa: E402
from app.neutrality.audit import (  # noqa: E402
    SYMMETRY_TOLERANCE,
    SymmetryAuditor,
    SymmetryGateReport,
    check_symmetry_gate,
)
from app.profiles.crypto import ProfileKeyring, ProfileRootKey  # noqa: E402
from app.profiles.service import VoterProfileService  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402
from app.zerodb.testing import FakeZeroDBTransport  # noqa: E402

logger = logging.getLogger("check_symmetry_audit")

#: The skew the self-check injects. 8 points: far above the 0.5 tolerance so
#: there is no doubt the failure is the sabotage rather than floating point,
#: and well inside the 0-100 range so it is not absorbed by the clamp in
#: `CandidateAlignment.score` (which would prove the model's bounds work
#: rather than that the audit catches skew).
SELF_CHECK_SKEW = 8.0

#: A throwaway root key for the in-memory run. Fixed rather than random so a
#: failing CI run is reproducible, and harmless because the only profiles it
#: ever seals are this audit's own two synthetic fixtures inside a process
#: that exits seconds later. **Never used by `--live`**, which reads the real
#: `$ZERODB_PROFILE_ROOT_KEY` — a fixed key must never touch a real voter row.
_FAKE_ROOT_KEY = bytes(range(32))

_METHODOLOGY_VERSION = "2027.1"


def _fake_stack() -> tuple[ZeroDBClient, VoterProfileService]:
    """An in-memory ZeroDB with the schema and a published methodology.

    `break_server_side_filter()` for story 1.4's reason: live ZeroDB's
    server-side `/query` filter matches nothing, so the audit's reads have to
    run on the client's paging fallback here too. An audit that looked
    symmetric because a fake filter narrowed its reads correctly would be
    false confidence about the one property this check exists to establish.
    """
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport,
        project_id="proj-symmetry-self-check",
        backoff_base=0.0,
        sleep=lambda _: None,
    )
    Bootstrapper(client, pace=lambda: None).run()
    MethodologyPublisher(client).publish_new_version(_METHODOLOGY_VERSION)
    profiles = VoterProfileService(
        client=client,
        keyring=ProfileKeyring(client=client, root_key=ProfileRootKey(_FAKE_ROOT_KEY)),
    )
    return client, profiles


def _report_and_exit(report: SymmetryGateReport, *, live: bool) -> int:
    mode = "LIVE" if live else "FAKE (in-memory, CI-safe)"
    logger.info("symmetry release gate [%s]: %s", mode, report.summary())
    if report.report is not None:
        logger.info("  %s", report.report.summary())
        for pair in report.report.pairs:
            logger.info(
                "  pair %s: %s vs %s (mirrored voter %s), delta %.4f",
                pair.name,
                pair.left_score,
                pair.right_score,
                pair.mirrored_voter_score,
                pair.delta,
            )
    if report.recorded is not None:
        logger.info(
            "  recorded audit_runs row %s: passed=%s max_delta=%s run_at=%s",
            report.recorded.get("id"),
            report.recorded.get("passed"),
            report.recorded.get("max_delta"),
            report.recorded.get("run_at"),
        )

    if report.ok:
        logger.info(
            "PASS — every mirrored pair is symmetric within %s points; the release is not blocked",
            SYMMETRY_TOLERANCE,
        )
        return 0

    logger.error(
        "FAIL — the scoring pipeline is not symmetric, so the next release is BLOCKED. "
        "%d violation(s):",
        len(report.violations),
    )
    for violation in report.violations:
        logger.error("  - %s", violation)
    logger.error(
        "This is CLAUDE.md's first non-negotiable failing: 'Nonpartisan by construction. "
        "One scoring codebase for every candidate and party. No party-specific rules.' A "
        "healthy pipeline reports a delta of 0.0, not merely one under the tolerance, so a "
        "nonzero delta is a real divergence and not a flaky test. Do not widen the "
        "tolerance."
    )
    return 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Audit the REAL ZeroDB project named by ZERODB_WORKSPACE_ID/ZERODB_PROJECT_ID/"
            "ZERODB_API_KEY, writing and then deleting synthetic rows and keeping only the "
            "audit_runs row. Source local.env into the shell first; never pass credentials "
            "on the command line."
        ),
    )
    args = parser.parse_args()

    if args.live:
        client = ZeroDBClient.from_env()
        try:
            logger.info(
                "auditing symmetry against LIVE project %r — this writes synthetic "
                "SYMMETRY-AUDIT rows and deletes them again, keeping only the audit_runs row",
                client.project_id,
            )
            profiles = VoterProfileService(
                client=client,
                keyring=ProfileKeyring(client=client, root_key=ProfileRootKey.from_env()),
            )
            report = check_symmetry_gate(client, profiles=profiles)
        except ZeroDBError as exc:
            logger.error("the symmetry gate failed to run: %s", exc)
            return 1
        finally:
            client.close()
        return _report_and_exit(report, live=True)

    logger.info(
        "auditing symmetry against an in-memory fake (the CI-safe default). Unlike the "
        "panel gate, this is NOT a weaker check than --live: the question is whether the "
        "scoring CODE is symmetric, and the code here is the code that ships. See this "
        "script's module docstring."
    )

    # Step 1: the self-check. A deliberately skewed run MUST be caught, or the
    # audit's failing path is broken and nothing below can be trusted.
    client, profiles = _fake_stack()
    try:
        skewed = SymmetryAuditor(client=client, profiles=profiles, skew=SELF_CHECK_SKEW).run()
    finally:
        client.close()

    if skewed.passed:
        logger.error(
            "SELF-CHECK FAILED: a %.1f-point skew injected into one side of every mirrored "
            "pair was NOT caught (reported max delta %.4f against tolerance %s). The audit's "
            "failing path is broken — it would rubber-stamp a genuinely partisan scorer. "
            "Refusing to report a real result until this is fixed.",
            SELF_CHECK_SKEW,
            skewed.max_delta,
            SYMMETRY_TOLERANCE,
        )
        return 1
    logger.info(
        "self-check passed: a %.1f-point skew is correctly caught (max delta %.4f > %s)",
        SELF_CHECK_SKEW,
        skewed.max_delta,
        SYMMETRY_TOLERANCE,
    )

    # Step 2: the real check, no skew. This is the number CI's exit code
    # reflects.
    client, profiles = _fake_stack()
    try:
        report = check_symmetry_gate(client, profiles=profiles)
    finally:
        client.close()
    return _report_and_exit(report, live=False)


if __name__ == "__main__":
    raise SystemExit(main())
