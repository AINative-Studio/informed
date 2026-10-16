"""Address-to-district resolution for Informed.

`CensusAddressResolver` (story 2.3) is the real implementation: it geocodes
against the US Census Geocoder, which prd.md lists as "Free, keyless", and
builds the OCD-ID that keys `districts` and that story 2.2's
`people.terms[].district_id` values already use.

It replaced story 1.5's `StubAddressResolver`, which returned one fixed district
for every address and is gone. `AddressResolver` is the protocol both stood
behind, so the swap changed one default and no caller.

`DistrictStore` (also 2.3) is what makes sure the resolved district has a
`districts` row to be read back from.

See `resolver.py` for the live response shape this is built against, the
reconciliation with story 2.2's ids, and how the "address is never stored" rule
is enforced structurally rather than by discipline.
"""

from app.districts.resolver import (
    AddressResolutionError,
    AddressResolver,
    AmbiguousAddressError,
    CensusAddressResolver,
    DistrictNotFoundError,
    GeocoderUnavailableError,
    ResolvedAddress,
)
from app.districts.store import DistrictStore

__all__ = [
    "AddressResolutionError",
    "AddressResolver",
    "AmbiguousAddressError",
    "CensusAddressResolver",
    "DistrictNotFoundError",
    "DistrictStore",
    "GeocoderUnavailableError",
    "ResolvedAddress",
]
