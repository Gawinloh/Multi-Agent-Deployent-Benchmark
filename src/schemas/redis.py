"""Redis configuration schemas.

Pure data containers plus a render method. Authoritative sources:

- Memory/eviction: Redis docs "Key eviction"
  (https://redis.io/docs/latest/develop/reference/eviction/); Antirez's
  notes on LRU/LFU eviction.
- Persistence: Redis docs "Persistence" (RDB vs AOF trade-offs).
- Security: Redis docs "Security"; CIS Redis Benchmark L1 (protected
  mode, authentication, dangerous command renaming).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

MaxmemoryPolicy = (
    "noeviction",
    "allkeys-lru",
    "allkeys-lfu",
    "volatile-lru",
    "volatile-lfu",
    "volatile-ttl",
    "allkeys-random",
    "volatile-random",
)


class RedisMemoryParams(BaseModel):
    """Memory and eviction. Source: Redis key-eviction docs; Antirez
    recommends allkeys-lru/lfu for pure-cache workloads."""

    maxmemory: str = Field(
        pattern=r"^\d+(\.\d+)?\s?(kb|mb|gb|KB|MB|GB|b|B)?$", description='e.g. "2GB"'
    )
    maxmemory_policy: str
    maxmemory_samples: int = Field(ge=1, le=64)

    @field_validator("maxmemory_policy")
    @classmethod
    def _check_policy(cls, v: str) -> str:
        if v not in MaxmemoryPolicy:
            raise ValueError(f"maxmemory_policy must be one of {MaxmemoryPolicy}")
        return v


class RedisPersistenceParams(BaseModel):
    """Persistence. Source: Redis persistence docs - AOF everysec is the
    recommended durability/performance balance."""

    save: list[str] = Field(description='RDB save points like ["3600 1", "300 100"]')
    appendonly: bool
    appendfsync: str = Field(description='"always" | "everysec" | "no"')

    @field_validator("save")
    @classmethod
    def _check_save(cls, v: list[str]) -> list[str]:
        for entry in v:
            if not re.fullmatch(r"\d+ \d+", entry):
                raise ValueError(f'save entry {entry!r} must look like "3600 1"')
        return v

    @field_validator("appendfsync")
    @classmethod
    def _check_appendfsync(cls, v: str) -> str:
        if v not in ("always", "everysec", "no"):
            raise ValueError('appendfsync must be "always", "everysec", or "no"')
        return v


class RedisSecurityParams(BaseModel):
    """Security. Source: Redis security docs; CIS Redis Benchmark L1
    (protected mode, requirepass, renaming dangerous commands)."""

    protected_mode: bool
    requirepass: str | None = Field(default=None, min_length=8)
    rename_commands: dict[str, str] = Field(
        default_factory=dict,
        description='e.g. {"FLUSHALL": "", "CONFIG": "CONFIG_a1b2"} - empty renames disable',
    )


class RedisNetworkingParams(BaseModel):
    """Networking. Source: Redis security docs - bind to specific
    interfaces, never expose 6379 publicly."""

    bind: list[str] = Field(min_length=1, description='e.g. ["127.0.0.1"]')
    port: int = Field(gt=0, le=65535)
    tls_port: int | None = Field(default=None, gt=0, le=65535)


class RedisConfig(BaseModel):
    """Complete redis.conf specification."""

    memory: RedisMemoryParams
    persistence: RedisPersistenceParams
    security: RedisSecurityParams
    networking: RedisNetworkingParams

    def render_conf(self) -> str:
        """Render a redis.conf text body."""
        lines = [
            "# redis.conf - generated, do not edit by hand",
            "",
            "# --- Networking (Redis security docs: bind explicitly) ---",
            f"bind {' '.join(self.networking.bind)}",
            f"port {self.networking.port}",
        ]
        if self.networking.tls_port is not None:
            lines.append(f"tls-port {self.networking.tls_port}")
        lines += [
            "",
            "# --- Memory / eviction (Redis key-eviction docs) ---",
            f"maxmemory {self.memory.maxmemory}",
            f"maxmemory-policy {self.memory.maxmemory_policy}",
            f"maxmemory-samples {self.memory.maxmemory_samples}",
            "",
            "# --- Persistence (Redis persistence docs) ---",
        ]
        lines += [f"save {entry}" for entry in self.persistence.save]
        lines += [
            f"appendonly {'yes' if self.persistence.appendonly else 'no'}",
            f"appendfsync {self.persistence.appendfsync}",
            "",
            "# --- Security (CIS Redis Benchmark L1) ---",
            f"protected-mode {'yes' if self.security.protected_mode else 'no'}",
        ]
        if self.security.requirepass is not None:
            lines.append(f"requirepass {self.security.requirepass}")
        for command, renamed in self.security.rename_commands.items():
            lines.append(f'rename-command {command} "{renamed}"')
        lines.append("")
        return "\n".join(lines)
