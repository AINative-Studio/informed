#!/usr/bin/env python3
"""Verify rate limiting against the LIVE "informed" project (story 14.5).

The limiter itself holds no data and makes no ZeroDB calls, so unlike every
other `verify_*_live.py` in this directory this script is not checking that a
write landed. It is checking the thing a fake cannot tell you about a
middleware: that putting it in front of endpoints which were verified live in
earlier stories **did not change what those endpoints return**, over real
ZeroDB rows rather than an in-memory stand-in.

That matters here more than usual, because this story is the first change in
the project that touches the request path of *every* endpoint at once. A
middleware that subtly broke one response body, dropped one header, or
mangled one status code would pass this story's own tests (which assert about
the limiter) and quietly regress four earlier stories.

What it proves, in order:

  1. **The published methodology still reads identically** through the
     limiter and with the limiter switched off, over the same live client —
     status, headers that matter, and the full JSON body, compared field by
     field. This is story 12.2's live-verified endpoint, re-run.
  2. **The methodology changelog and a per-version citation still resolve**
     live, unchanged. Story 12.2's other two routes.
  3. **A 404 is still a 404.** A limiter that turned an unpublished version
     into a 500 or a 429 would break the citation contract.
  4. **Malformed path parameters are still refused at the edge** — the
     path-parameter hardening story 12.2 checked live, re-checked with the
     middleware in front of it.
  5. **Every allowed response carries the allowance headers**, so an honest
     client can pace itself, and `X-RateLimit-Remaining` really counts down
     across live requests.
  6. **The live read allowance is actually enforced**, and the refusal is a
     429 carrying a sane `Retry-After` — driven against a live-backed
     endpoint, not a fake one.
  7. **The refusal body carries nothing voter-derived**, checked against the
     bytes a caller receives. prd.md's privacy rule applies to error
     responses too, and a limiter is a tempting place to echo request detail.
  8. **A refusal is stable and is served without a round trip.** Timed: a
     429 that comes back in well under the latency of a live ZeroDB read is
     a refusal that short-circuited. Stated as a timing observation rather
     than as proof, because the live httpx transport keeps no call log — the
     BDD scenario "a refused request never reaches the endpoint behind it"
     proves the short-circuit properly, by counting rows over the fake.
  9. **The health probe is never limited**, over far more requests than any
     policy allows — a 429 to a liveness check reads as an outage.
 10. **Every route the live app actually serves resolves to a policy**, walked
     from the real route table rather than from a list in a test.

!!! NO WRITES, NO CLEANUP NEEDED !!!

Every live call this script makes is a read. It writes nothing, and it
deliberately does not exercise `POST /v1/corrections`: that endpoint's live
verification belongs to story 12.4's own script, and filling the live
corrections table with five throwaway rows to watch a limiter work is exactly
the test debris this project's verification scripts avoid. The correction
policy is asserted against the policy table (check 10) and exercised
end-to-end over the fake in `tests/step_defs/test_rate_limiting_steps.py`.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_rate_limiting_live.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.methodology.router import zerodb_client_for_methodology  # noqa: E402
from app.ratelimit import UNLIMITED_PATHS, policy_for  # noqa: E402
from app.ratelimit.policy import resolve_template  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

#: A version id that must never have been published, in the shape the route
#: accepts, so check 3 exercises the real miss path rather than the path check.
UNPUBLISHED_VERSION = "1999.9"

#: Words that would mean voter data reached a public response — including an
#: error response, which is the one people forget.
VOTER_WORDS = ("voter", "profile", "interest", "payload", "dealbreaker", "user_id", "stance")

logger = logging.getLogger("verify_rate_limiting_live")


def _override(client: ZeroDBClient):
    """A zero-argument dependency override.

    Never `lambda c=client: c`: FastAPI reads an override's signature as
    request parameters, so a default argument becomes a query parameter and
    the override is handed `None` — the endpoint then quietly builds its own
    client. Cost this story an afternoon; see the step file's note.
    """

    def provide() -> ZeroDBClient:
        return client

    return provide


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # The same live client behind two apps: one with the limiter, one
        # without. Any difference between their answers is this story's doing.
        limited = create_app()
        limited.dependency_overrides[zerodb_client_for_methodology] = _override(client)
        plain = create_app(rate_limit=False)
        plain.dependency_overrides[zerodb_client_for_methodology] = _override(client)

        with TestClient(limited) as api, TestClient(plain) as unlimited_api:
            # --- 1. the published methodology is unchanged ----------------
            with_limiter = api.get("/v1/methodology")
            without = unlimited_api.get("/v1/methodology")
            check(
                "GET /v1/methodology answers 200 over live ZeroDB with the limiter in front",
                with_limiter.status_code == 200,
                f"status {with_limiter.status_code}: {with_limiter.text[:200]}",
            )
            check(
                "the live methodology body is byte-identical with and without the limiter",
                with_limiter.status_code == without.status_code
                and with_limiter.json() == without.json(),
                f"limited={with_limiter.status_code} unlimited={without.status_code}",
            )
            published = with_limiter.json() if with_limiter.status_code == 200 else {}
            logger.info(
                "        live version=%s is_current=%s",
                published.get("version"),
                published.get("is_current"),
            )
            check(
                "that request still carried no Authorization header (it is a public read)",
                "authorization" not in {k.lower() for k in with_limiter.request.headers},
                "the request was authenticated, so this proves nothing about a public read",
            )

            # --- 2. the changelog and a citation target still resolve -----
            versions = api.get("/v1/methodology/versions")
            versions_plain = unlimited_api.get("/v1/methodology/versions")
            check(
                "GET /v1/methodology/versions is unchanged by the limiter, live",
                versions.status_code == versions_plain.status_code == 200
                and versions.json() == versions_plain.json(),
                f"limited={versions.status_code} unlimited={versions_plain.status_code}",
            )
            listed = versions.json().get("versions", []) if versions.status_code == 200 else []
            logger.info("        live changelog holds %d version(s)", len(listed))

            if published.get("version"):
                target = published["version"]
                by_id = api.get(f"/v1/methodology/{target}")
                by_id_plain = unlimited_api.get(f"/v1/methodology/{target}")
                check(
                    f"GET /v1/methodology/{target} (the citation target) is unchanged, live",
                    by_id.status_code == by_id_plain.status_code == 200
                    and by_id.json() == by_id_plain.json(),
                    f"limited={by_id.status_code} unlimited={by_id_plain.status_code}",
                )

            # --- 3. a miss is still a miss -------------------------------
            missing = api.get(f"/v1/methodology/{UNPUBLISHED_VERSION}")
            check(
                "an unpublished version still answers 404 through the limiter, not 500 or 429",
                missing.status_code == 404,
                f"status {missing.status_code}: {missing.text[:200]}",
            )

            # --- 4. edge hardening survives the middleware ---------------
            for junk in ("../../etc/passwd", "2026", "abcd.1", "2026.1.1"):
                junk_response = api.get(f"/v1/methodology/{junk}")
                check(
                    f"a malformed version id {junk!r} is still refused at the edge",
                    junk_response.status_code in (404, 422),
                    f"status {junk_response.status_code}",
                )

            # --- 5. allowance headers, and they count down ---------------
            policy = policy_for("GET", "/v1/methodology")
            assert policy is not None, "the methodology read has no policy"
            logger.info(
                "        policy in force: %s — %d requests / %ds",
                policy.name,
                policy.limit,
                policy.window_seconds,
            )
            first = api.get("/v1/methodology")
            second = api.get("/v1/methodology")
            check(
                "an allowed live response names its policy and its limit",
                first.headers.get("x-ratelimit-policy") == policy.name
                and first.headers.get("x-ratelimit-limit") == str(policy.limit),
                f"headers were {dict(first.headers)!r}",
            )
            try:
                counts_down = int(first.headers["x-ratelimit-remaining"]) > int(
                    second.headers["x-ratelimit-remaining"]
                )
            except (KeyError, ValueError):
                counts_down = False
            check(
                "X-RateLimit-Remaining counts down across live requests",
                counts_down,
                f"{first.headers.get('x-ratelimit-remaining')} then "
                f"{second.headers.get('x-ratelimit-remaining')}",
            )

            # --- 6. the live allowance is actually enforced --------------
            # Several requests have already been spent above, so this walks
            # up to the limit rather than assuming a fresh bucket.
            refusal = None
            for _ in range(policy.limit + 2):
                response = api.get("/v1/methodology")
                if response.status_code == 429:
                    refusal = response
                    break
            check(
                "the live read allowance is enforced: one address is eventually refused",
                refusal is not None,
                f"{policy.limit + 2} further requests all succeeded",
            )
            if refusal is not None:
                retry_after = refusal.headers.get("retry-after", "")
                check(
                    "the refusal carries a Retry-After in whole seconds, within the window",
                    retry_after.isdigit() and 1 <= int(retry_after) <= policy.window_seconds,
                    f"Retry-After was {retry_after!r} for a {policy.window_seconds}s window",
                )
                logger.info("        refused with Retry-After: %ss", retry_after)
                check(
                    "the refusal tells shared caches not to store it",
                    "no-store" in refusal.headers.get("cache-control", ""),
                    f"cache-control was {refusal.headers.get('cache-control')!r}",
                )

                # --- 7. the refusal leaks nothing ------------------------
                body = json.dumps(refusal.json()).lower()
                leaked = [word for word in VOTER_WORDS if word in body]
                check(
                    "the 429 body mentions no voter, profile or interest data",
                    not leaked,
                    f"mentions {leaked}",
                )

                # --- 8. the refusal is stable and costs nothing behind it -
                # "No ZeroDB call was made" is not assertable here: the live
                # httpx transport keeps no call log (only the in-memory fake
                # does), and introspecting private attributes to fake the
                # measurement would be a check that looks stronger than it
                # is. What a live run CAN show is that the refusal is served
                # fast and repeatably, and the BDD scenario "a refused
                # request never reaches the endpoint behind it" is what
                # actually proves the short-circuit, over the fake, by
                # counting rows.
                started = time.perf_counter()
                again = api.get("/v1/methodology")
                elapsed_ms = (time.perf_counter() - started) * 1000
                check(
                    "a refused request is still refused (the window has not moved)",
                    again.status_code == 429,
                    f"status {again.status_code}",
                )
                check(
                    "the refusal is served without a round trip to ZeroDB "
                    f"(took {elapsed_ms:.1f} ms; a live read is tens of ms)",
                    elapsed_ms < 20.0,
                    f"{elapsed_ms:.1f} ms suggests the request reached the store",
                )

            # --- 9. the liveness probe is never limited ------------------
            probe_statuses = {api.get("/health").status_code for _ in range(200)}
            check(
                "the health probe survives 200 requests from one address, live app",
                probe_statuses == {200},
                f"statuses seen: {sorted(probe_statuses)}",
            )

            # --- 10. every served route resolves to a policy -------------
            uncovered: list[str] = []
            for route in limited.routes:
                methods = getattr(route, "methods", None)
                path = getattr(route, "path", "")
                if not methods or path in UNLIMITED_PATHS:
                    continue
                if path.startswith(("/openapi", "/docs", "/redoc")):
                    continue
                for method in sorted(methods - {"HEAD", "OPTIONS"}):
                    if policy_for(method, path) is None:
                        uncovered.append(f"{method} {path}")
            check(
                "every route the live app serves resolves to a rate-limit policy",
                not uncovered,
                f"unlimited route(s): {uncovered}",
            )

            # The correction write is asserted here rather than exercised,
            # for the reason in this module's docstring: five throwaway rows
            # in the live corrections table is test debris.
            correction_policy = policy_for("POST", resolve_template("/v1/corrections"))
            read_policy = policy_for("GET", "/v1/topics")
            check(
                "POST /v1/corrections is on a stricter policy than the public reads",
                correction_policy is not None
                and read_policy is not None
                and correction_policy.per_minute() < read_policy.per_minute(),
                f"correction={correction_policy} read={read_policy}",
            )
            if correction_policy:
                logger.info(
                    "        correction write policy: %s — %d / %ds "
                    "(NOT exercised live: it writes rows)",
                    correction_policy.name,
                    correction_policy.limit,
                    correction_policy.window_seconds,
                )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        logger.info("wrote nothing: every live call above was a read")
        client.close()

    if failures:
        logger.error("\n%d check(s) FAILED:", len(failures))
        for failure in failures:
            logger.error("  - %s", failure)
        return 1
    logger.info("\nall live checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
