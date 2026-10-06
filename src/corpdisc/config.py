"""Application settings (Pydantic Settings, loaded from the environment and .env).

Defaults follow ARCHITECTURE.md Part I section 12. Relative paths are kept as
given and resolve against the current working directory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Generic tracking parameters removed during URL canonicalization. A trailing '*' is a prefix.
DEFAULT_TRACKING_QUERY_PARAMS: tuple[str, ...] = (
    "utm_*", "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "yclid", "igshid", "_hsenc", "_hsmi",
)  # fmt: skip
# Generic multi-tenant platforms. A domain in this list is never treated as a dedicated
# company website by the matcher (many unrelated entities can live under it).
DEFAULT_SHARED_PLATFORM_DOMAINS: tuple[str, ...] = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "github.com", "medium.com", "wikipedia.org", "crunchbase.com", "wordpress.com",
    "blogspot.com", "wixsite.com", "squarespace.com", "github.io", "sites.google.com",
)  # fmt: skip
_ROOT_CODE_RE = re.compile(r"^[A-Z0-9_-]+$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    # Root target. Retargeting = changing these three values.
    start_url: str = "https://www.csisoftware.com/"
    root_company: str = "Constellation Software Inc."
    root_code: str = "CSI"  # Tree ID prefix

    # Crawl limits and politeness
    max_depth: int = Field(default=8, ge=1)
    max_pages_per_domain: int = Field(default=40, ge=1)
    max_total_pages: int = Field(default=2000, ge=1)
    request_timeout: float = Field(default=20, gt=0)  # seconds
    request_delay_seconds: float = Field(default=2.0, ge=0)
    max_retries: int = Field(default=2, ge=0)
    user_agent: str = "CorpDiscoveryBot/0.1 (+contact-url-or-email)"
    respect_robots: bool = True
    allow_insecure_ssl: bool = False
    use_playwright: Literal["auto", "true", "false"] = "auto"  # config value only

    # Confidence thresholds
    confirm_threshold: float = Field(default=0.75, ge=0, le=1)
    review_threshold: float = Field(default=0.40, ge=0, le=1)
    low_confidence_threshold: float = Field(default=0.60, ge=0, le=1)
    fuzzy_match_threshold: float = Field(default=0.92, ge=0, le=1)
    recurse_into_review: bool = False
    max_links_per_entity: int = Field(default=12, ge=1)

    # M1 fetching, caching and rendering
    max_response_bytes: int = Field(default=2_000_000, ge=1024)
    fetch_max_redirects: int = Field(default=5, ge=0, le=20)
    page_cache_ttl_seconds: float = Field(default=7 * 86400.0, ge=0)  # 0 = never reuse cache
    playwright_timeout_seconds: float = Field(default=30.0, gt=0)
    playwright_min_text_chars: int = Field(default=200, ge=0)
    robots_failure_cooldown_seconds: float = Field(default=600.0, ge=0)
    allow_private_hosts: bool = False  # SSRF guard: loopback/private hosts are refused by default
    tracking_query_params: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_TRACKING_QUERY_PARAMS)
    )  # entries ending in '*' are prefixes

    # M1 matching, commit and budgets
    shared_platform_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_SHARED_PLATFORM_DOMAINS)
    )
    require_website_for_confirmed: bool = True
    llm_token_budget: int | None = Field(default=None, ge=1)  # None = unlimited
    max_llm_calls_per_task: int = Field(default=12, ge=1)
    max_llm_calls_per_run: int = Field(default=5000, ge=1)
    max_tool_calls_per_task: int = Field(default=20, ge=1)
    max_tool_calls_per_run: int = Field(default=5000, ge=1)

    # M2 agent infrastructure (ARCHITECTURE 25.2, 25.4, 27.3)
    max_provider_requests_per_invocation: int = Field(default=6, ge=1)
    llm_breaker_threshold: int = Field(default=5, ge=1)
    llm_transport_retries: int = Field(default=3, ge=0)
    llm_backoff_base_seconds: float = Field(default=1.0, ge=0)
    llm_backoff_max_seconds: float = Field(default=30.0, ge=0)
    llm_temperature: float = Field(default=0.0, ge=0, le=2)
    llm_cassette_dir: Path = Path("./.cache/llm_cassettes")
    # M8 real providers
    llm_timeout_seconds: float = Field(default=300.0, gt=0)
    llm_base_url: str = ""
    llm_price_in_per_mtok: float = Field(default=0.0, ge=0)
    llm_price_out_per_mtok: float = Field(default=0.0, ge=0)
    llm_record_to: Path | None = None  # also write every successful response to this JSONL cassette
    # M7 supporting agents (ARCHITECTURE 20.3)
    advisor_enabled: bool = True
    advisor_min_conf: float = Field(default=0.60, ge=0, le=1)
    dedup_merge_conf: float = Field(default=0.85, ge=0, le=1)
    dedup_different_conf: float = Field(default=0.70, ge=0, le=1)
    validation_min_conf: float = Field(default=0.50, ge=0, le=1)
    website_min_conf: float = Field(default=0.70, ge=0, le=1)
    # Providers (blank until a later milestone). Keys are never logged.
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    search_provider: str = ""
    search_api_key: SecretStr | None = None

    # Paths and logging
    db_path: Path = Path("./data/csi_hierarchy.db")
    cache_dir: Path = Path("./.cache")
    output_xlsx: Path = Path("./CSI_Company_Hierarchy.xlsx")
    log_dir: Path = Path("./logs")
    log_level: str = "INFO"

    @field_validator("llm_api_key", "search_api_key", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, v: Any) -> Any:
        if v is None:
            return None
        raw = v.get_secret_value() if isinstance(v, SecretStr) else v
        if isinstance(raw, str) and not raw.strip():
            return None
        return v

    @field_validator("llm_record_to", mode="before")
    @classmethod
    def _blank_path_is_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("llm_token_budget", mode="before")
    @classmethod
    def _blank_budget_is_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("tracking_query_params", "shared_platform_domains", mode="before")
    @classmethod
    def _split_list(cls, v: Any) -> Any:
        """Accept a comma-separated string (env form) or a list; lowercase and dedupe."""
        if isinstance(v, str):
            v = v.split(",")
        if isinstance(v, (list, tuple, set, frozenset)):
            seen: dict[str, None] = {}
            for item in v:
                if not isinstance(item, str):
                    raise ValueError("list entries must be strings")
                item = item.strip().lower()
                if item:
                    seen.setdefault(item)
            return list(seen)
        return v

    @field_validator("use_playwright", mode="before")
    @classmethod
    def _lower_playwright(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return "true" if v else "false"
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("start_url")
    @classmethod
    def _check_start_url(cls, v: str) -> str:
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an absolute http:// or https:// URL")
        return v

    @field_validator("root_company", "user_agent")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("root_code")
    @classmethod
    def _check_root_code(cls, v: str) -> str:
        v = v.strip()
        if not _ROOT_CODE_RE.fullmatch(v):
            raise ValueError("must contain only uppercase letters, digits, '_' or '-'")
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _check_log_level(cls, v: Any) -> Any:
        if not isinstance(v, str):
            raise ValueError(f"must be one of {', '.join(LOG_LEVELS)}")
        up = v.strip().upper()
        if up not in LOG_LEVELS:
            raise ValueError(f"must be one of {', '.join(LOG_LEVELS)}")
        return up

    @model_validator(mode="after")
    def _check_threshold_order(self) -> Settings:
        if not (self.review_threshold <= self.low_confidence_threshold <= self.confirm_threshold):
            raise ValueError(
                "thresholds must satisfy REVIEW_THRESHOLD <= LOW_CONFIDENCE_THRESHOLD "
                "<= CONFIRM_THRESHOLD"
            )
        return self

    def secret_values(self) -> list[str]:
        """Non-empty secret values, for the logging redactor. Never print the result."""
        secrets = (self.llm_api_key, self.search_api_key)
        return [s.get_secret_value() for s in secrets if s is not None and s.get_secret_value()]


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Load settings. Pass env_file=None to ignore any .env file (used by tests)."""
    return Settings(_env_file=env_file)  # type: ignore[call-arg]
