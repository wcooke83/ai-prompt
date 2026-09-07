"""Router configuration: load and validate router.json, with env overrides.

A JSON file rather than env vars because an upstream is a compound thing -- name +
url + key + provider list -- and a flat AI_PROMPT_* namespace models a list of those
badly. Only the two knobs an init script or systemd unit needs are read from the
environment: AI_PROMPT_ROUTER_CONFIG (where the file is) and AI_PROMPT_ROUTER_PORT
(what to listen on).

Validation is deliberately loud. A duplicated upstream name or a typo'd url would
otherwise surface much later as a routing oddity -- two upstreams fighting over the
same session-map key, or a bridge that silently never gets traffic -- so load_config()
refuses to start instead of degrading quietly. Every failure path raises ValueError
(pydantic's ValidationError is itself a ValueError, so callers only need the one
except clause).

There is no hot-reload, matching the bridge: change the file, restart the process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# The router imports the bridge's models so the two cannot drift; the provider enum
# is the same one PromptRequest validates against, which means a `providers` entry
# that is not in the enum could never be matched by a real request anyway.
from ai_prompt_api.models import Provider

from .strategy import STRATEGY_NAMES

ENV_CONFIG = "AI_PROMPT_ROUTER_CONFIG"
ENV_PORT = "AI_PROMPT_ROUTER_PORT"

DEFAULT_CONFIG_PATH = Path.home() / ".ai-prompt" / "router.json"
DEFAULT_SESSIONS_PATH = Path.home() / ".ai-prompt" / "router-sessions.json"
# The native host already owns ai-prompt-native.sock; the router gets its own path so
# both can run on the same box without one shadowing the other.
DEFAULT_UNIX_SOCKET = "/tmp/ai-prompt-router.sock"

_VALID_PROVIDERS = tuple(p.value for p in Provider)


def _check_url(value: str, *, schemes: tuple[str, ...], label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in schemes:
        raise ValueError(
            f"{label} {value!r} must start with {' or '.join(s + '://' for s in schemes)}"
        )
    if not parsed.netloc:
        raise ValueError(f"{label} {value!r} is missing a host")
    # Trailing slashes are stripped so httpx's base_url join and the name shown in
    # /upstreams are stable no matter how the operator typed it.
    return value.rstrip("/")


class UpstreamConfig(BaseModel):
    """One bridge (or one downstream router -- the router cannot tell them apart)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    url: str
    api_key: Optional[str] = None
    # Omitted means "assume every provider". Consulted only when a request pins a
    # provider; static because bridges do not report which providers are logged in.
    providers: Optional[list[str]] = None
    # Keeps an upstream in the file but out of rotation (maintenance).
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _check_url(value, schemes=("http", "https"), label="upstream url")

    @field_validator("providers")
    @classmethod
    def _validate_providers(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        unknown = [p for p in value if p not in _VALID_PROVIDERS]
        if unknown:
            raise ValueError(
                f"unknown provider(s) {', '.join(sorted(unknown))}; "
                f"expected one of {', '.join(_VALID_PROVIDERS)}"
            )
        return value


class ListenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = Field(8300, ge=1, le=65535)


class ApiKeyEntry(BaseModel):
    """A per-app key. Revoking an app is deleting its entry and restarting."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    key: str = Field(..., min_length=1)


class WsTarget(BaseModel):
    """A WebSocket server the router dials into while pretending to be the extension."""

    model_config = ConfigDict(extra="forbid")

    url: str

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _check_url(value, schemes=("ws", "wss"), label="ws_target url")


class UnixSocketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = DEFAULT_UNIX_SOCKET


class RouterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listen: ListenConfig = Field(default_factory=ListenConfig)
    # Empty list = no auth on the HTTP front, the same rule the bridge uses when
    # AI_PROMPT_API_KEY is unset.
    api_keys: list[ApiKeyEntry] = Field(default_factory=list)
    ws_targets: list[WsTarget] = Field(default_factory=list)
    # null disables the unix-socket front entirely.
    unix_socket: Optional[UnixSocketConfig] = Field(default_factory=UnixSocketConfig)

    strategy: str = "round_robin"
    failover: bool = True
    max_attempts: int = Field(3, ge=1)
    health_interval_seconds: float = Field(10.0, gt=0)
    max_hops: int = Field(4, ge=1)
    # Used when a caller does not send `timeout`; mirrors the bridge's default so a
    # request behaves the same whether it went through the router or straight to a bridge.
    default_timeout: float = Field(120.0, gt=0)

    session_ttl_hours: float = Field(24.0, ge=0)
    sessions_path: Path = Field(default_factory=lambda: DEFAULT_SESSIONS_PATH)

    upstreams: list[UpstreamConfig]

    @field_validator("strategy")
    @classmethod
    def _validate_strategy(cls, value: str) -> str:
        if value not in STRATEGY_NAMES:
            raise ValueError(
                f"unknown strategy {value!r}; expected one of {', '.join(STRATEGY_NAMES)}"
            )
        return value

    @field_validator("sessions_path")
    @classmethod
    def _expand_sessions_path(cls, value: Path) -> Path:
        return Path(value).expanduser()

    @model_validator(mode="after")
    def _validate_upstreams(self) -> "RouterConfig":
        if not self.upstreams:
            raise ValueError("'upstreams' must list at least one upstream")

        seen: set[str] = set()
        duplicates: set[str] = set()
        for upstream in self.upstreams:
            if upstream.name in seen:
                duplicates.add(upstream.name)
            seen.add(upstream.name)
        if duplicates:
            # Names are the session map's key and the X-AI-Prompt-Upstream value;
            # duplicates would make both ambiguous.
            raise ValueError(
                f"duplicate upstream name(s): {', '.join(sorted(duplicates))}"
            )
        return self

    def upstream(self, name: str) -> Optional[UpstreamConfig]:
        for upstream in self.upstreams:
            if upstream.name == name:
                return upstream
        return None


def _format_validation_error(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        lines.append(f"  {location}: {item['msg']}")
    return "\n".join(lines)


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Explicit argument > $AI_PROMPT_ROUTER_CONFIG > ~/.ai-prompt/router.json."""
    if path is not None:
        return Path(path).expanduser()
    from_env = os.environ.get(ENV_CONFIG)
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_CONFIG_PATH


def load_config(path: str | Path | None = None) -> RouterConfig:
    """Read, env-override and validate router.json. Raises ValueError on any problem."""
    resolved = resolve_config_path(path)

    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(
            f"router config not found: {resolved} "
            f"(set ${ENV_CONFIG} or copy ai_prompt_router/router.example.json there)"
        ) from None
    except OSError as exc:
        raise ValueError(f"cannot read router config {resolved}: {exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"router config {resolved} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"router config {resolved} must contain a JSON object")

    # Env override is applied to the raw dict rather than the built model so that the
    # normal field validation (port range) still runs on it.
    port_override = os.environ.get(ENV_PORT)
    if port_override:
        try:
            port = int(port_override)
        except ValueError:
            raise ValueError(f"${ENV_PORT} must be an integer, got {port_override!r}") from None
        listen = dict(raw.get("listen") or {})
        listen["port"] = port
        raw["listen"] = listen

    try:
        return RouterConfig(**raw)
    except ValidationError as exc:
        raise ValueError(
            f"invalid router config {resolved}:\n{_format_validation_error(exc)}"
        ) from exc
