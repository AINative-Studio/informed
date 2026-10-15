"""ZeroDB connection settings, read from the environment (story 1.2).

The settings object reads environment variables and nothing else. It never
opens `local.env`, `.env` or any other file itself, so the same code serves
local development (where a shell sources `local.env`), CI (where the values are
repository secrets) and the three deployed environments of story 1.6.

The API key is a `SecretStr`: a settings object that lands in a traceback or a
log line must not take the credential with it (prd.md, privacy first).
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_BASE_URL = "https://api.ainative.studio/api/v1"


class ZeroDBSettings(BaseSettings):
    """Everything needed to reach one ZeroDB project.

    Attributes:
        api_key: Bearer token for the AINative API. Required.
        project_id: The ZeroDB project holding Informed's tables. Required.
        workspace_id: The owning workspace. Carried for story 1.6's
            per-environment projects and for support requests; the per-project
            REST routes do not take it.
        base_url: API root, overridable for staging or a local mock.
        timeout_seconds: Per-attempt timeout, not a budget for all retries.
        max_attempts: Total attempts per request, first try included.
    """

    model_config = SettingsConfigDict(
        env_prefix="ZERODB_",
        extra="ignore",
        case_sensitive=False,
    )

    api_key: SecretStr = Field(..., description="AINative API bearer token")
    project_id: str = Field(..., description="ZeroDB project id for this environment")
    workspace_id: str | None = Field(default=None, description="Owning AINative workspace id")
    base_url: str = Field(default=DEFAULT_BASE_URL, description="AINative API root")
    timeout_seconds: float = Field(default=30.0, gt=0, description="Per-attempt timeout")
    max_attempts: int = Field(default=4, ge=1, le=10, description="Attempts per request")


__all__ = ["DEFAULT_BASE_URL", "ZeroDBSettings"]
