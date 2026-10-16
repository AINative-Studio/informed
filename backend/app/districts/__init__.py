"""Address-to-district resolution for Informed.

Today this package holds only a **stub** (`StubAddressResolver`) that returns
one fixed district for every address, so story 1.5's walking skeleton has
something to resolve against. The real implementation — the US Census Geocoder,
which prd.md lists as "Free, keyless" — is story 2.3 and lands behind the
`AddressResolver` protocol defined here, replacing the stub without changing a
single caller.

See `resolver.py` for what the stub does and does not do, and why it is a stub
rather than a half-built geocoder.
"""

from app.districts.resolver import (
    AddressResolutionError,
    AddressResolver,
    DistrictNotFoundError,
    ResolvedAddress,
    StubAddressResolver,
)

__all__ = [
    "AddressResolutionError",
    "AddressResolver",
    "DistrictNotFoundError",
    "ResolvedAddress",
    "StubAddressResolver",
]
