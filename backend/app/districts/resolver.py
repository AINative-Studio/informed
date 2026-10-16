"""Resolving a street address to the districts that contain it (story 2.3).

This module geocodes for real. Story 1.5 left a deliberate stub here — one
fixed district for every address, loudly labelled as such — and story 2.3
replaces it with `CensusAddressResolver`, a client for the US Census Geocoder
(`geocoding.geo.census.gov`), the free, keyless source prd.md names for
"US address -> district".

The seam story 1.5 built is unchanged: `AddressResolver` is still the protocol,
`ResolvedAddress` is still the answer, `AddressResolutionError` is still the
failure, and `BallotService` still takes a resolver by injection. Only the
default changed, from `StubAddressResolver()` to `CensusAddressResolver()`. Two
exception subclasses are new (`AmbiguousAddressError`,
`GeocoderUnavailableError`), both of `AddressResolutionError`, so every existing
caller that catches the base class keeps working.

What the live API actually returns (confirmed live, 2026-10-16)
===============================================================

    GET https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress
        ?address=400+Broad+St,+Seattle,+WA+98109
        &benchmark=Public_AR_Current&vintage=Current_Current&format=json

    {"result": {
       "input": {...},
       "addressMatches": [{
         "matchedAddress": "400 BROAD ST, SEATTLE, WA, 98109",
         "addressComponents": {...},
         "coordinates": {"x": -122.34, "y": 47.61},
         "geographies": {
           "States": [{"STUSAB": "WA", "STATE": "53", "NAME": "Washington", ...}],
           "120th Congressional Districts": [
              {"CD120": "07", "CDSESSN": "120", "GEOID": "5307",
               "NAME": "Congressional District 7", "BASENAME": "7", ...}],
           ... nine more geography layers ...
    }}]}}

Four things in that shape drive the whole implementation, and each was a
surprise worth writing down:

1. **No FIPS crosswalk is needed.** The story anticipated mapping the state
   FIPS code (`STATE: "53"`) to `WA` through a 50-plus-row table. The response
   carries `States[].STUSAB` — the USPS two-letter code — directly, so the
   crosswalk is read from the geocoder's own answer instead of maintained here.
   A hand-kept FIPS table is a thing that can drift and be subtly wrong for a
   territory; not having one is strictly better. `STATE` is deliberately
   ignored.

2. **The layer key carries the Congress number.** It is
   `"120th Congressional Districts"` today and was `"119th ..."` before. A
   hard-coded key would break on a date nobody is watching, so the layer is
   found by *shape* — the one key containing "Congressional" — and the district
   number by the `CD` + digits field inside it. See `_congressional_layer`.

3. **`CD120` is zero-padded; story 2.2's `district_id` is not.** The geocoder
   says `"07"`; the 442 distinct House `people.terms[].district_id` values
   already live in ZeroDB say `/cd:7`. Since datamodel.md has no joins and the
   ballot query matches these two ids as **strings**, a padded id would match
   nothing and every voter in a single-digit district would get an empty
   delegation with no error to explain it. `ocd_id_for` strips the padding.
   This is the whole of the reconciliation with story 2.2 — see that section
   below.

4. **`00` and `98` are not district numbers.** `00` is the geocoder's code for
   a state with a single at-large seat (WY, VT, DE, AK, ND, SD) and `98` for a
   non-voting delegate or resident commissioner (DC, PR, GU, VI, AS, MP). Both
   mean "no congressional district", and both map to the state-only OCD-ID —
   which is exactly what story 2.2's loader does with its own `district: 0`.

Reconciliation with story 2.2's live legislator data
=====================================================

Story 2.2 called its `terms[].district_id` scheme "provisional" and said story
2.3 might have to migrate it. **It does not.** The two schemes are identical,
verified against the live project rather than by reading the code:

  * 539 `people` rows carry 442 distinct House `district_id`s and 50 Senate
    ones, all of the form `ocd-division/country:us/state:{ss}[/cd:{n}]`.
  * Every `/cd:{n}` is **unpadded** — zero rows in a padded or `at-large`
    form.
  * 12 House `district_id`s are state-only (AK, AS, DC, DE, GU, MP, ND, PR, SD,
    VI, VT, WY), exactly the at-large and delegate seats.
  * Nine live geocoder lookups (WA-07, NY-12, OH-11, WY-00, AK-00, VT-00,
    DE-00, DC-98, PR-98) each produced an id that is present in that live set.

So no migration was performed and none is needed; `ocd_id_for` is written to
produce precisely what is already stored. The one thing that *was* stale is the
`districts` table, which was **empty** — meaning every ballot lookup 404'd
before this story. See `app.districts.store` for how a `districts` row now
comes to exist.

A note on `cd:at-large`, which is **not** the scheme: story 1.5's feature file
uses `ocd-division/country:us/state:wy/cd:at-large` for a Wyoming fixture. That
is a third spelling, present only in that story's synthetic test data and
matching no live row. It is corrected to the state-only form by this story so
the repo carries one scheme rather than two.

Privacy: where the address goes, and where it does not
======================================================

prd.md's privacy rule and this story's own acceptance criterion
("the address is never stored") are enforced structurally, not by discipline:

  * **It is never stored.** Nothing in this module writes the address anywhere.
    `ResolvedAddress` has no field it could travel in (asserted by a test that
    checks the dataclass's fields, not just today's values), so it cannot reach
    a row, a response or a cache by being passed along inside a result.
  * **It is never logged.** Every log line and every exception message in this
    module names the *district* or the *failure*, never the address — including
    on the failure paths, which is where the obvious implementation leaks it
    ("could not geocode {address}" is the natural and wrong log line). The
    geocoder's own `matchedAddress` echo is equally off limits and is never
    read. A test drives all four failure paths and asserts no record contains
    the address.
  * **It is never cached.** No memoisation, deliberately: the cache key would
    *be* the address, held for the process's lifetime. A test asserts the
    resolver instance holds nothing resembling it after resolving.
  * **It is sent to exactly one third party**, the Census Geocoder, which is
    the irreducible cost of geocoding at all, and is the source prd.md picked.
    `layers` narrows the response to the two geographies actually read, so the
    geocoder returns less address-adjacent data than its default.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)

#: The Census Geocoder's one-line-address **geographies** endpoint. Not
#: `locations/`: that one returns coordinates only, and this story needs the
#: congressional district, which only `geographies/` attaches.
CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

#: Sent explicitly rather than relying on the service's documented defaults, so
#: a change to *its* default cannot silently move Informed onto a different set
#: of district boundaries. `Public_AR_Current` / `Current_Current` is "current
#: address ranges against the current vintage", which is what a voter asking
#: who represents them today means.
CENSUS_BENCHMARK = "Public_AR_Current"
CENSUS_VINTAGE = "Current_Current"

#: Only the two geography layers this resolver reads. The default response
#: carries eleven (census blocks, tracts, urban areas, state legislative
#: districts...); narrowing it is a small privacy win — fewer address-adjacent
#: geographies come back — as well as a smaller payload.
CENSUS_LAYERS = "States,Congressional Districts"

#: The live API's own cap, enforced locally so an over-long address is a
#: message about the address rather than a wasted round trip and a raw upstream
#: 400. Confirmed live: a blank or over-long address answers
#: `{"errors":["Address cannot be empty and cannot exceed 100 characters"]}`.
MAX_ADDRESS_LENGTH = 100

DEFAULT_TIMEOUT_SECONDS = 10.0

#: The geocoder's two codes for "this seat has no congressional district
#: number": `00` a state with one at-large seat, `98` a non-voting delegate or
#: resident commissioner. Both map to the state-only OCD-ID, which is what
#: story 2.2's loader does with its own `district: 0`.
AT_LARGE_CODES = frozenset({"00", "98"})

#: Matches the district-number field inside the congressional layer, whose name
#: carries the Congress number: `CD118`, `CD119`, `CD120`, ... Found by pattern
#: so a session bump does not need a code change.
_CD_FIELD = re.compile(r"^CD\d{2,3}$")


class AddressResolutionError(Exception):
    """An address could not be resolved to a district.

    Raised for a blank or unusable address, for one the geocoder cannot find,
    and by `BallotService` when the resolved district is not in ZeroDB. All are
    answered to the voter rather than logged as a server fault, which is why
    they share one base: the handler turns it into a 4xx with a message the
    voter can act on.

    The message must never contain the address itself: these messages are
    logged and returned, and prd.md's privacy rule applies to both.
    """


class DistrictNotFoundError(AddressResolutionError):
    """An address resolved to a district Informed cannot record.

    A subclass, because the two failures deserve different HTTP answers but the
    same "tell the voter, don't log a fault" handling: a blank address is the
    voter's input to fix (422), while a district Informed cannot store is its
    own data gap (404) and nothing the voter can do differently. Callers that do
    not care about the distinction catch `AddressResolutionError` and get both.

    Before story 2.3 this meant "there is no `districts` row for that id", which
    story 2.3 retired by creating the row on first resolution. What it means now
    is that the row could not be *written* — in practice because the chamber it
    references was not seeded. Same category of failure, same status; see
    `app.districts.store`.
    """


class AmbiguousAddressError(AddressResolutionError):
    """The geocoder matched the address in more than one district.

    A real and common case, not a defensive branch: `100 Main St, Springfield`
    returns seven live matches across six congressional districts in four
    states. Taking the first would tell a Massachusetts voter they live in
    Colorado's 4th — a wrong answer delivered with full confidence, which for
    this product is worse than no answer (prd.md: evidence over opinion). So
    the voter is asked for more detail instead.

    Matches that *agree* on one district are not ambiguous and resolve
    normally; see `_single_district_among`.
    """


class GeocoderUnavailableError(AddressResolutionError):
    """The Census Geocoder could not be reached, or failed on its own side.

    Distinct from every other failure because it is the only one the voter can
    fix by waiting, and the only one that is Informed's problem rather than the
    address's. Telling a voter to check their street number when the geocoder
    is down sends them to fix something that is not broken.
    """


@dataclass(frozen=True)
class ResolvedAddress:
    """The districts an address falls in. Never the address itself.

    The three fields are the whole of it, deliberately: an address cannot reach
    a row, a log line or a response by riding along inside a resolution result
    if there is no field for it. A test asserts these exact field names, so
    adding a fourth that carried the address would fail rather than leak.

    Attributes:
        district_id: OCD-ID of the US House district, e.g.
            `ocd-division/country:us/state:wa/cd:7`, or the state-only division
            for an at-large or delegate seat. The `districts` row id.
        senate_district_id: OCD-ID of the state division the two senators
            represent — the House id with the `/cd:*` segment dropped. Senators
            are elected statewide, so there is no congressional district for
            them, and datamodel.md's `people.terms[].district_id` is what the
            delegation query filters on.
        state: Two-letter state code, upper case, for display and for the
            state-level queries later stories add.
    """

    district_id: str
    senate_district_id: str
    state: str


@dataclass(frozen=True)
class ResolvedDistrict:
    """A `ResolvedAddress` plus the geocoder's own name for the district.

    Two types rather than a fourth field on `ResolvedAddress`, because the two
    answer different questions: `ResolvedAddress` is "which divisions is this
    voter in", which is all the delegation query needs, and the name is only
    needed on the one path that may have to *create* the `districts` row. A
    caller that does not create rows never sees the name and cannot
    accidentally treat it as identity.

    Carries no address, for the same structural reason `ResolvedAddress`
    does not.

    Attributes:
        districts: the divisions the address falls in.
        name: the district's name exactly as the geocoder labels it, e.g.
            "Congressional District 7" or "Delegate District (at Large)".
    """

    districts: ResolvedAddress
    name: str


@runtime_checkable
class AddressResolver(Protocol):
    """Turns an address into the districts containing it.

    `CensusAddressResolver` implements this. The protocol is what `BallotService`
    depends on, never a concrete class, which is what let story 2.3 be a new
    class and one changed default rather than a rewrite — and is what will let a
    second jurisdiction's resolver (prd.md's `JurisdictionAdapter`) slot in the
    same way.
    """

    def resolve(self, address: str) -> ResolvedAddress:
        """Resolve one address.

        Raises:
            AddressResolutionError: the address is blank or cannot be resolved.
        """
        ...

    def resolve_district(self, address: str) -> ResolvedDistrict:
        """Resolve one address, also returning the district's name.

        The name is what lets a caller create the `districts` row for a
        district nobody has looked up before (`DistrictStore.ensure`) without
        consulting a second source for it.

        Raises:
            AddressResolutionError: the address is blank or cannot be resolved.
        """
        ...


def senate_division_for(district_id: str) -> str:
    """The statewide OCD division a congressional district sits in.

    `ocd-division/country:us/state:ca/cd:19` -> `ocd-division/country:us/state:ca`.
    Senators have no congressional district, so their `terms[].district_id` is
    the state division; this is the one place that relationship is expressed, so
    story 2.2's ingest and the ballot query cannot disagree about it.

    An id with no `/cd:` segment (a statewide division already) is returned
    unchanged rather than being mangled.
    """
    head, separator, _ = district_id.partition("/cd:")
    return head if separator else district_id


def state_code_from(district_id: str) -> str | None:
    """The two-letter state code in an OCD-ID, upper case, or None.

    None rather than a raise: a malformed id is a data problem for the caller to
    report in its own terms, and OCD-IDs for non-state jurisdictions (a
    territory, or a future non-US division) legitimately have no state segment.
    """
    for segment in district_id.split("/"):
        key, separator, value = segment.partition("state:")
        if separator and not key:
            return value.upper()
    return None


def ocd_id_for(state_abbreviation: str, district_code: str) -> str:
    """The `districts` row id for one state and congressional-district code.

    This function is the contract between story 2.3's geocoding and story 2.2's
    already-loaded `people.terms[].district_id` values. Both sides must spell
    the same district the same way, because datamodel.md has no joins and the
    ballot query compares these ids as strings.

        ("WA", "07") -> ocd-division/country:us/state:wa/cd:7
        ("NY", "12") -> ocd-division/country:us/state:ny/cd:12
        ("WY", "00") -> ocd-division/country:us/state:wy        (at-large)
        ("DC", "98") -> ocd-division/country:us/state:dc        (delegate)

    Two normalisations, each matching what is already live:

      * **the state is lower-cased**, per datamodel.md's worked example
        (`.../state:ca/cd:19`) and all 492 live ids;
      * **the district number's leading zero is stripped**, because the
        geocoder pads to two digits and no live `terms[].district_id` does.
        Without this, `/cd:07` would match none of the 442 live House ids and a
        Washington voter in district 7 would be shown an empty delegation.

    Raises:
        AddressResolutionError: the state is blank, or the district code is
            neither an at-large code nor a number. Never names the address.
    """
    state = (state_abbreviation or "").strip().lower()
    if not state:
        raise AddressResolutionError(
            "We couldn't tell which state that address is in. Please check it and try again."
        )

    division = f"ocd-division/country:us/state:{state}"
    code = (district_code or "").strip()
    if code in AT_LARGE_CODES:
        return division
    if not code.isdigit():
        # Not a number and not a known at-large code: a schema change, not a
        # voter error. Logged with the code (never the address) so an operator
        # can see a new convention appear.
        logger.warning("unrecognised congressional district code %r for state %r", code, state)
        raise AddressResolutionError(
            "We couldn't work out your congressional district from that address."
        )
    return f"{division}/cd:{int(code)}"


def district_name_from(match: dict[str, Any]) -> str:
    """The district's human name, taken from the geocoder's own label.

    `districts.name` (datamodel.md) is reference data, and prd.md's
    evidence-over-opinion rule applies to reference data as much as to
    verdicts: the geocoder already distinguishes

        "Congressional District 7"
        "Congressional District (at Large)"       (WY, VT, DE, AK, ND, SD)
        "Delegate District (at Large)"            (DC, GU, VI, AS, MP)
        "Resident Commissioner District (at Large)" (PR)

    and calling DC's seat a "Congressional District" would be factually wrong.
    So the label is read, not invented.

    Falls back to a constructed name only if the layer carries no `NAME`, which
    the live service always does.
    """
    layer = _congressional_layer(match)
    name = str(layer.get("NAME") or "").strip()
    if name:
        return name
    return f"Congressional District {layer.get(_cd_field_name(layer), '')}".strip()


class CensusAddressResolver:
    """Resolves an address to its districts via the US Census Geocoder.

    The real implementation of `AddressResolver`, replacing story 1.5's stub.
    Free and keyless (prd.md's data-sources table), so there is no credential
    to inject and nothing environment-specific about it.

    `http_get` is injected rather than calling `httpx.get` directly so every
    test drives a captured response and CI never reaches census.gov
    (CLAUDE.md). It defaults to the real `httpx.get`, so production code
    constructs this class with no arguments.

    Deliberately **stateless and cache-free**. A memoising resolver would be an
    easy latency win and a direct violation of this story's privacy criterion,
    because the cache key would be the address itself, retained for the
    process's lifetime. Story 2.4's p95 budget is met by caching the
    *district*-keyed read (`districts` rows, `candidate_cards`), never the
    address-keyed one.
    """

    def __init__(
        self,
        *,
        http_get: Callable[..., httpx.Response] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        url: str = CENSUS_GEOCODER_URL,
    ) -> None:
        self._http_get = http_get if http_get is not None else httpx.get
        self._timeout_seconds = timeout_seconds
        self._url = url

    def resolve(self, address: str) -> ResolvedAddress:
        """Resolve one address to its House and Senate divisions.

        The address is used to build one outbound request and then dropped. It
        is not stored, not logged, not cached, and has no field in the returned
        `ResolvedAddress` to travel in.

        Raises:
            AddressResolutionError: the address is blank, over-long, or the
                geocoder could not turn it into a district.
            AmbiguousAddressError: it matched more than one district.
            GeocoderUnavailableError: the geocoder could not be reached.

        No message ever contains the address (prd.md, privacy first).
        """
        return self.resolve_district(address).districts

    def resolve_district(self, address: str) -> ResolvedDistrict:
        """Resolve one address, also returning the geocoder's district name.

        The same single request as `resolve`; the name comes out of the
        response that is already in hand, so a caller that needs to create the
        `districts` row costs no extra lookup.

        Raises:
            AddressResolutionError: the address is blank, over-long, or the
                geocoder could not turn it into a district.
            AmbiguousAddressError: it matched more than one district.
            GeocoderUnavailableError: the geocoder could not be reached.
        """
        candidate = (address or "").strip()
        if not candidate:
            # Checked before the request, so an empty field costs no round trip
            # and gets a message about the field rather than about the service.
            raise AddressResolutionError("An address is required to find your representatives.")
        if len(candidate) > MAX_ADDRESS_LENGTH:
            raise AddressResolutionError(
                f"That address is longer than the {MAX_ADDRESS_LENGTH} characters the "
                "address lookup accepts. Please shorten it to street, city, state and ZIP."
            )

        matches = self._matches_for(candidate)
        if not matches:
            raise AddressResolutionError(
                "We couldn't find that address. Please check the street number, "
                "city, state and ZIP code, and try again."
            )
        return _single_district_among(matches)

    # The protocol's `resolve`/`resolve_district` pair is the whole public
    # surface; everything below is how one response becomes an answer.

    def _matches_for(self, address: str) -> list[dict[str, Any]]:
        """The geocoder's `addressMatches` for one address.

        Every failure here becomes an `AddressResolutionError` subclass, so no
        caller ever sees an `httpx` exception or a raw upstream body — ui-prd.md
        asks for an inline, specific error, and a stack trace is neither.
        """
        params = {
            "address": address,
            "benchmark": CENSUS_BENCHMARK,
            "vintage": CENSUS_VINTAGE,
            "layers": CENSUS_LAYERS,
            "format": "json",
        }
        try:
            response = self._http_get(self._url, params=params, timeout=self._timeout_seconds)
        except httpx.HTTPError as exc:
            # The exception is logged by *type*, never stringified: an httpx
            # error's message can include the full request URL, and the URL
            # carries the address in a query parameter.
            logger.warning("census geocoder unreachable: %s", type(exc).__name__)
            raise GeocoderUnavailableError(
                "Address lookup is temporarily unavailable. Please try again in a moment."
            ) from None

        if response.status_code >= 500:
            logger.warning("census geocoder returned %d", response.status_code)
            raise GeocoderUnavailableError(
                "Address lookup is temporarily unavailable. Please try again in a moment."
            )
        if response.status_code >= 400:
            # A 4xx is about the address, not about the service. Telling the
            # voter "temporarily unavailable" would send them back to retry
            # something that will never work.
            logger.info("census geocoder rejected a lookup with %d", response.status_code)
            raise AddressResolutionError(
                "We couldn't look that address up. Please check the street number, "
                "city, state and ZIP code, and try again."
            )

        try:
            payload = response.json()
        except ValueError:
            # A 200 that is not JSON is an upstream gateway or maintenance
            # page, which is a service problem rather than an address problem.
            logger.warning("census geocoder returned a non-JSON body with status 200")
            raise GeocoderUnavailableError(
                "Address lookup is temporarily unavailable. Please try again in a moment."
            ) from None

        if not isinstance(payload, dict):
            raise GeocoderUnavailableError(
                "Address lookup is temporarily unavailable. Please try again in a moment."
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            logger.warning("census geocoder response carried no `result` object")
            raise GeocoderUnavailableError(
                "Address lookup is temporarily unavailable. Please try again in a moment."
            )
        matches = result.get("addressMatches")
        return [m for m in matches if isinstance(m, dict)] if isinstance(matches, list) else []


def _single_district_among(matches: list[dict[str, Any]]) -> ResolvedDistrict:
    """The one district all these matches agree on.

    Several matches are common and usually harmless: two ZIP+4 ranges of the
    same street sit in the same district, and resolving that would be wrong to
    refuse. Several matches in *different* districts is the dangerous case —
    `100 Main St, Springfield` really does return six districts in four states —
    and is refused rather than guessed, because a confidently wrong district is
    worse than an honest "please be more specific".

    Agreement is compared on the resolved OCD-ID, not on the geocoder's own
    GEOID or on the district's name, so the comparison is over the identity
    Informed actually uses downstream.
    """
    resolutions = [_resolution_for(match) for match in matches]
    distinct = {resolution.districts for resolution in resolutions}
    if len(distinct) > 1:
        # Logged as a count only: the matched addresses are the voter's address
        # normalised, so neither they nor the districts' spelling go in a log
        # line alongside something that could identify the lookup.
        logger.info("address matched %d distinct districts; asking for more detail", len(distinct))
        raise AmbiguousAddressError(
            "That address matched more than one district. Please add the street "
            "number, city, state or ZIP code so we can tell which one you mean."
        )
    return resolutions[0]


def _resolution_for(match: dict[str, Any]) -> ResolvedDistrict:
    """One `addressMatches` entry turned into a resolved district and its name.

    Reads exactly two geographies and nothing else — not `matchedAddress`, not
    `addressComponents`, not `coordinates`. Those carry the address back and are
    never touched, so there is no path by which the geocoder's echo of the
    address reaches a row, a log or a response.
    """
    state = _state_abbreviation(match)
    layer = _congressional_layer(match)
    district_code = str(layer.get(_cd_field_name(layer), "")).strip()
    district_id = ocd_id_for(state, district_code)
    return ResolvedDistrict(
        districts=ResolvedAddress(
            district_id=district_id,
            senate_district_id=senate_division_for(district_id),
            state=state.upper(),
        ),
        name=district_name_from(match),
    )


def _state_abbreviation(match: dict[str, Any]) -> str:
    """The USPS two-letter state code, from the geocoder's `States` layer.

    `STUSAB`, not the FIPS `STATE` code: the response hands the abbreviation
    over directly, so Informed keeps no FIPS-to-abbreviation crosswalk of its
    own to drift out of date or be subtly wrong for a territory.
    """
    states = (match.get("geographies") or {}).get("States") or []
    if not states or not isinstance(states[0], dict):
        logger.warning("census geocoder match carried no `States` geography")
        raise AddressResolutionError(
            "We couldn't tell which state that address is in. Please check it and try again."
        )
    return str(states[0].get("STUSAB") or "").strip()


def _congressional_layer(match: dict[str, Any]) -> dict[str, Any]:
    """The congressional-district geography, found by shape not by name.

    The key carries the Congress number — `"120th Congressional Districts"`
    today, `"119th ..."` before, `"121st ..."` after the next apportionment — so
    it is located by the one key containing "Congressional" rather than by a
    literal that would expire silently.
    """
    geographies = match.get("geographies") or {}
    for key, rows in geographies.items():
        if "congressional" not in str(key).lower():
            continue
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    logger.warning(
        "census geocoder match carried no congressional district layer; layers were %r",
        sorted(str(k) for k in geographies),
    )
    raise AddressResolutionError(
        "We couldn't work out your congressional district from that address. "
        "Please check it and try again."
    )


def _cd_field_name(layer: dict[str, Any]) -> str:
    """The name of the district-number field inside the congressional layer.

    `CD118`/`CD119`/`CD120`/... — the Congress number again, so matched by
    pattern. Falls back to `BASENAME`, which carries the same number for a
    numbered district, if the service ever renames the field entirely.
    """
    for key in layer:
        if _CD_FIELD.match(str(key)):
            return str(key)
    return "BASENAME"


__all__ = [
    "AT_LARGE_CODES",
    "CENSUS_BENCHMARK",
    "CENSUS_GEOCODER_URL",
    "CENSUS_LAYERS",
    "CENSUS_VINTAGE",
    "MAX_ADDRESS_LENGTH",
    "AddressResolutionError",
    "AddressResolver",
    "AmbiguousAddressError",
    "CensusAddressResolver",
    "DistrictNotFoundError",
    "GeocoderUnavailableError",
    "ResolvedAddress",
    "ResolvedDistrict",
    "district_name_from",
    "ocd_id_for",
    "senate_division_for",
    "state_code_from",
]
