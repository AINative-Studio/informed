"""No-third-party-tracker audit for story 14.7.

backlog.md 14.7's second acceptance criterion is "no third-party
trackers". As of this story that is already true — nothing in any prior
story added Google Analytics, Mixpanel, Segment or similar to the frontend
— but a criterion that is only ever checked by a human reading
`package.json` on release day is a criterion that silently stops being
checked. This module makes it a running test instead
(`tests/test_privacy_safe_analytics_unit.py`'s two "the real frontend ..."
tests), so a future PR that adds `mixpanel-browser` to
`frontend/package.json` fails CI the moment it lands, not whenever someone
next happens to look.

Two independent checks, because a tracker can arrive two ways
==================================================================

  1. **A dependency.** `find_disallowed_dependencies` reads
     `frontend/package.json`'s `dependencies` and `devDependencies` and
     flags any whose name matches `DISALLOWED_ANALYTICS_PACKAGES` (a name
     *substring* match, so a scoped package like `@segment/analytics-next`
     is caught by `segment` without needing every possible scope spelled
     out).
  2. **A raw `<script>` tag.** A tracker does not have to be an npm
     dependency — it can be pasted straight into `index.html` pointing at
     the vendor's CDN. `find_third_party_tracker_scripts` walks
     `frontend/`'s source files (`.html`, `.tsx`, `.ts`) for a `<script`
     tag whose `src` names one of `THIRD_PARTY_ANALYTICS_DOMAINS`.

Both lists are deliberately short and named, rather than one giant
denylist pretending to be exhaustive: this story's job is to catch the
well-known, large-scale trackers prd.md's "no third-party trackers" is
actually worried about, not to play an unwinnable game of enumerating every
analytics vendor that has ever existed. Extending either list is a one-line
change with its own PR, which is the right amount of ceremony for a
decision about what the product is allowed to send a voter's browsing to.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Known third-party analytics/tracking SDKs, as npm package name substrings.
#: Matched case-sensitively against the exact string in package.json, which
#: is how npm names are published.
DISALLOWED_ANALYTICS_PACKAGES: tuple[str, ...] = (
    "google-analytics",
    "ga-gtag",
    "react-ga",
    "mixpanel",
    "segment",
    "amplitude",
    "fullstory",
    "hotjar",
    "heap",
    "posthog",
    "sentry",  # error tracking, not usage analytics, but still a third-party
    # beacon of browsing data — excluded from this release on the same
    # "no third-party trackers" reading until a story explicitly scopes
    # error reporting in.
    "datadog",
    "pendo",
)

#: Known third-party analytics domains, for a raw `<script src="...">` that
#: never went through npm at all.
THIRD_PARTY_ANALYTICS_DOMAINS: tuple[str, ...] = (
    "google-analytics.com",
    "googletagmanager.com",
    "analytics.google.com",
    "cdn.mxpnl.com",
    "cdn.segment.com",
    "cdn.amplitude.com",
    "fullstory.com",
    "static.hotjar.com",
    "cdn.heapanalytics.com",
    "app.posthog.com",
    "cdn.mouseflow.com",
    "snowplowanalytics.com",
)

#: Source file extensions `find_third_party_tracker_scripts` reads. Plain
#: text, no bundler, no JS execution — this is a textual grep over source,
#: the same posture `app.integrity.checks` and the provenance audit take for
#: their own "read the real repo state" checks.
_SCRIPT_SEARCH_EXTENSIONS = (".html", ".tsx", ".ts", ".jsx", ".js")

#: Directories never worth walking: build output and dependencies, which
#: can themselves legitimately mention a tracker's name in a comment,
#: changelog or transitive dependency without that meaning the APP serves
#: one.
_EXCLUDED_DIR_NAMES = frozenset({"node_modules", "dist", "build", ".git", "coverage"})

_SCRIPT_TAG_PATTERN = re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def find_disallowed_dependencies(package_json_path: Path) -> list[str]:
    """Dependency names in `package_json_path` matching a known tracker.

    Checks `dependencies` AND `devDependencies` — a tracker pulled in only
    for local testing is still a package decision worth refusing, and this
    audit's job is to make that decision visible rather than to assume a
    dev dependency never ships.

    Returns an empty list for a clean manifest. Each finding names the
    package and which disallowed substring matched, so a failing test's
    message is actionable without opening the file.
    """
    try:
        manifest = json.loads(package_json_path.read_text())
    except FileNotFoundError:
        return []

    findings: list[str] = []
    for section in ("dependencies", "devDependencies"):
        for package_name in manifest.get(section, {}):
            for disallowed in DISALLOWED_ANALYTICS_PACKAGES:
                if disallowed in package_name.lower():
                    findings.append(
                        f"{section}.{package_name!r} matches disallowed analytics package "
                        f"{disallowed!r}"
                    )
    return findings


def find_third_party_tracker_scripts(source_root: Path) -> list[str]:
    """Every `<script src="...">` under `source_root` pointing at a known
    third-party analytics domain.

    Walks `.html`/`.ts`/`.tsx`/`.js`/`.jsx` files, skipping build output and
    dependency directories (see `_EXCLUDED_DIR_NAMES`) — those can
    legitimately mention a tracker's name without the app serving one.

    Returns an empty list when nothing is found. Each finding names the file
    and the matched `src`, so a failing test points straight at the tag to
    remove.
    """
    findings: list[str] = []
    if not source_root.exists():
        return findings

    for path in source_root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        if path.suffix not in _SCRIPT_SEARCH_EXTENSIONS:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for match in _SCRIPT_TAG_PATTERN.finditer(text):
            src = match.group(1)
            for domain in THIRD_PARTY_ANALYTICS_DOMAINS:
                if domain in src:
                    findings.append(
                        f"{path}: <script src={src!r}> matches tracker domain {domain!r}"
                    )
    return findings


__all__ = [
    "DISALLOWED_ANALYTICS_PACKAGES",
    "THIRD_PARTY_ANALYTICS_DOMAINS",
    "find_disallowed_dependencies",
    "find_third_party_tracker_scripts",
]
