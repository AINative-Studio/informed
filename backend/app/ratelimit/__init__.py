"""Rate limiting and bot protection for the public API (story 14.5).

Two modules, one seam each:

* `policy` — *which* endpoint gets *what* allowance, and why. Read this one
  first; it is the content of the story. It also holds the honest statement
  of what "per-account limits" and "challenge on abuse" do and do not mean
  here.
* `middleware` — the sliding-window arithmetic and the ASGI plumbing,
  including why this is hand-rolled rather than `slowapi` and exactly what
  "in-process" costs when the deployment grows past one instance.
"""

from app.ratelimit.middleware import (
    REFUSAL_DETAIL,
    RateLimitDecision,
    RateLimitMiddleware,
)
from app.ratelimit.policy import (
    CORRECTION_WRITE,
    POLICIES,
    PRIVATE_READ,
    PUBLIC_READ,
    PUBLIC_WRITE,
    UNLIMITED_PATHS,
    RateLimitPolicy,
    account_key,
    client_key,
    policy_for,
)

__all__ = [
    "CORRECTION_WRITE",
    "POLICIES",
    "PRIVATE_READ",
    "PUBLIC_READ",
    "PUBLIC_WRITE",
    "REFUSAL_DETAIL",
    "UNLIMITED_PATHS",
    "RateLimitDecision",
    "RateLimitMiddleware",
    "RateLimitPolicy",
    "account_key",
    "client_key",
    "policy_for",
]
