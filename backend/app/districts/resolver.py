"""Resolving a street address to the districts that contain it.

=============================================================================
  THE ONLY RESOLVER IN THIS FILE IS A STUB. IT DOES NOT GEOCODE ANYTHING.
  Real address -> district lookup is **story 2.3**, which replaces
  `StubAddressResolver` with a US Census Geocoder client behind the
  `AddressResolver` protocol below. Until then every address resolves to one
  fixed district, named in `StubAddressResolver.DISTRICT_ID`.
=============================================================================

Why a stub rather than a partial geocoder. Story 1.5's job is to prove the
React -> FastAPI -> ZeroDB path carries real data end to end. A half-built
geocoder would add a live third-party HTTP dependency (and its flakiness, its
rate limits and its address-normalisation edge cases) to the one story whose
whole purpose is proving the *other* three hops work. Worse, a geocoder that
mostly works is the kind of thing a later story trusts: story 2.3's acceptance
criteria ("the Census Geocoder returns an OCD-ID; an invalid address gives a
helpful error; the address is never stored") would look already satisfied.
A stub that is unmistakably a stub cannot be mistaken for that.

What the stub does keep honest, because story 2.3 inherits it:

  * **The shape of the answer.** A real OCD-ID (datamodel.md: `districts` is
    keyed by OCD-ID), and a Senate division id derived from the state rather
    than invented, so the service's "two senators for one address" query is the
    same query it will run against real geocoded output.
  * **The seam.** `AddressResolver` is a protocol, and `BallotService` takes one
    by injection, so story 2.3 is a new class and one default, not a rewrite.
  * **The privacy rule.** prd.md's "privacy first" and story 2.3's "the address
    is never stored": `ResolvedAddress` deliberately does not carry the address
    it was resolved from, so an address cannot reach a response, a log line or a
    row by being passed along inside the resolution result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class AddressResolutionError(Exception):
    """An address could not be resolved to a district.

    Raised for a blank or unusable address, and by `BallotService` when the
    resolved district is not in ZeroDB. Both are answered to the voter rather
    than logged as a server fault, which is why they share one exception: the
    handler turns it into a 4xx with a message the voter can act on.

    The message must never contain the address itself: these messages are
    logged and returned, and prd.md's privacy rule applies to both.
    """


class DistrictNotFoundError(AddressResolutionError):
    """An address resolved to a district Informed has no row for.

    A subclass, because the two failures deserve different HTTP answers but the
    same "tell the voter, don't log a fault" handling: a blank address is the
    voter's input to fix (422), while a missing `districts` row is Informed's
    data gap (404) and nothing the voter can do differently. Callers that do not
    care about the distinction catch `AddressResolutionError` and get both.
    """


@dataclass(frozen=True)
class ResolvedAddress:
    """The districts an address falls in. Never the address itself.

    Attributes:
        district_id: OCD-ID of the US House district, e.g.
            `ocd-division/country:us/state:ca/cd:19`. The `districts` row id.
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


@runtime_checkable
class AddressResolver(Protocol):
    """Turns an address into the districts containing it.

    Story 2.3's Census Geocoder client implements this; `StubAddressResolver`
    stands in until then. A caller depends on this protocol, never on either
    concrete class, so swapping them is a one-line change at the injection
    point.
    """

    def resolve(self, address: str) -> ResolvedAddress:
        """Resolve one address.

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


class StubAddressResolver:
    """**A stub.** Returns one fixed district for every address (story 1.5).

    Not a geocoder. It does not look at the address beyond rejecting a blank
    one, and it returns the same district for an address in California and one
    in Maine. Story 2.3 replaces it; see this module's docstring for why it is
    deliberately this dumb.

    `district_id` is constructor-injectable so a test (or a second seeded
    district) can point it elsewhere without a second class.
    """

    #: The district every address resolves to. California's 19th is
    #: datamodel.md's own worked example of an OCD-ID, so the skeleton's test
    #: data matches the documentation rather than inventing a district.
    DISTRICT_ID = "ocd-division/country:us/state:ca/cd:19"

    def __init__(self, district_id: str | None = None) -> None:
        self._district_id = district_id or self.DISTRICT_ID

    def resolve(self, address: str) -> ResolvedAddress:
        """Return the fixed district, after refusing a blank address.

        The blank check is the one piece of real behaviour here, and it is real
        because the API contract needs it now: an empty `?address=` must be a
        422 with a helpful message, not a confident answer about California.

        Raises:
            AddressResolutionError: the address is empty or whitespace. The
                message names the field, never the value.
        """
        if not address or not address.strip():
            raise AddressResolutionError("An address is required to find your representatives.")

        state = state_code_from(self._district_id)
        if state is None:  # pragma: no cover - only a malformed override hits this
            raise AddressResolutionError("That district is not a US state district.")

        return ResolvedAddress(
            district_id=self._district_id,
            senate_district_id=senate_division_for(self._district_id),
            state=state,
        )


__all__ = [
    "AddressResolutionError",
    "AddressResolver",
    "DistrictNotFoundError",
    "ResolvedAddress",
    "StubAddressResolver",
    "senate_division_for",
    "state_code_from",
]
