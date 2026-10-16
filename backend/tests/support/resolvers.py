"""A test double for `AddressResolver` (story 2.3).

Story 1.5 had `StubAddressResolver` in **production** code (`app/districts/`),
because the app genuinely shipped without a geocoder and the stub was what it
resolved addresses with. Story 2.3 replaced it with `CensusAddressResolver`, so
a fixed-district resolver is no longer something the app does — it is only
something a *test* needs, and it belongs here rather than shipping in the
package.

The distinction matters beyond tidiness. While the stub lived in `app/`, any
caller could construct it and get confident, wrong district answers in
production. It cannot now.

Every test that wants a determinate district without driving a captured Census
response uses this. Tests of the geocoding itself drive
`tests/fixtures/census_geocoder_responses.json` through
`CensusAddressResolver` instead, because that is the code under test.
"""

from __future__ import annotations

from app.districts.resolver import (
    AddressResolutionError,
    ResolvedAddress,
    ResolvedDistrict,
    senate_division_for,
    state_code_from,
)


class FixedDistrictResolver:
    """Resolves every address to one district chosen by the test.

    Implements the `AddressResolver` protocol (`resolve` and
    `resolve_district`), so it drops into `BallotService` exactly where the
    real Census client goes.

    It keeps the two pieces of real behaviour a caller depends on, so a test
    using it is not testing a fiction:

      * a blank address is refused, because the API contract needs an empty
        `?address=` to be a 422 and not a confident answer;
      * the senate division is derived from the district id rather than
        supplied, so the "two senators for one address" query is the same query
        the real resolver's output drives.
    """

    #: The district a resolver resolves to unless the test names another.
    #: California's 19th is datamodel.md's own worked example of an OCD-ID.
    DISTRICT_ID = "ocd-division/country:us/state:ca/cd:19"

    #: A plausible name for it. Shaped like the Census Geocoder's own `NAME`
    #: ("Congressional District 19"), since that is what the real resolver
    #: hands `DistrictStore.ensure`.
    DEFAULT_NAME = "Congressional District 19"

    def __init__(self, district_id: str | None = None, *, name: str | None = None) -> None:
        self._district_id = district_id or self.DISTRICT_ID
        self._name = name or self.DEFAULT_NAME

    def resolve(self, address: str) -> ResolvedAddress:
        return self.resolve_district(address).districts

    def resolve_district(self, address: str) -> ResolvedDistrict:
        if not address or not address.strip():
            raise AddressResolutionError("An address is required to find your representatives.")

        state = state_code_from(self._district_id)
        if state is None:  # pragma: no cover - only a malformed override hits this
            raise AddressResolutionError("That district is not a US state district.")

        return ResolvedDistrict(
            districts=ResolvedAddress(
                district_id=self._district_id,
                senate_district_id=senate_division_for(self._district_id),
                state=state,
            ),
            name=self._name,
        )


__all__ = ["FixedDistrictResolver"]
