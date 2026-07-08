"""PostgreSQL configuration schemas.

Pure data containers plus render methods. Parameter groupings follow the
postgresql.conf chapter structure. Authoritative sources:

- Memory: PostgreSQL docs "Resource Consumption"
  (https://www.postgresql.org/docs/16/runtime-config-resource.html);
  pgtune recommends shared_buffers ~= 25% of RAM.
- WAL: PostgreSQL docs "Write Ahead Log"
  (https://www.postgresql.org/docs/16/runtime-config-wal.html).
- Security/logging: CIS PostgreSQL Benchmark Level 1.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: Memory size string like "4GB", "512MB", "64kB", "1.5GB".
MEMORY_PATTERN = r"^\d+(\.\d+)?\s?(kB|MB|GB|TB)$"


class PostgresMemoryParams(BaseModel):
    """Memory-related settings.

    Source: PostgreSQL docs, Resource Consumption chapter. pgtune
    recommends shared_buffers = 25% of RAM, effective_cache_size = 75%.
    """

    shared_buffers: str = Field(pattern=MEMORY_PATTERN, description='e.g. "4GB"')
    effective_cache_size: str = Field(pattern=MEMORY_PATTERN)
    work_mem: str = Field(pattern=MEMORY_PATTERN)
    maintenance_work_mem: str = Field(pattern=MEMORY_PATTERN)


class PostgresConnectionParams(BaseModel):
    """Connection limits. Source: PostgreSQL docs, Connections and
    Authentication chapter."""

    max_connections: int = Field(gt=0, le=10000)
    superuser_reserved_connections: int = Field(ge=0, le=100)


class PostgresWALParams(BaseModel):
    """Write-ahead-log settings. Source: PostgreSQL docs, WAL chapter."""

    wal_level: Literal["minimal", "replica", "logical"]
    checkpoint_completion_target: float = Field(ge=0.0, le=1.0)
    max_wal_size: str = Field(pattern=MEMORY_PATTERN)


class PostgresSecurityParams(BaseModel):
    """Security settings. Source: CIS PostgreSQL Benchmark L1
    (authentication and connection logging controls)."""

    ssl: bool
    password_encryption: Literal["scram-sha-256", "md5"]
    log_connections: bool
    log_disconnections: bool
    ssl_min_protocol_version: Literal["TLSv1.2", "TLSv1.3"]


class PostgresLoggingParams(BaseModel):
    """Logging settings. Source: CIS PostgreSQL Benchmark L1 section 2
    (logging and auditing)."""

    log_destination: Literal["stderr", "csvlog", "syslog"]
    log_statement: Literal["none", "ddl", "mod", "all"]
    log_min_duration_statement: int = Field(
        ge=-1, description="ms; -1 disables, 0 logs everything"
    )


def _pg_bool(value: bool) -> str:
    return "on" if value else "off"


class PostgresConfig(BaseModel):
    """Complete postgresql.conf specification."""

    memory: PostgresMemoryParams
    connections: PostgresConnectionParams
    wal: PostgresWALParams
    security: PostgresSecurityParams
    logging: PostgresLoggingParams
    listen_addresses: str = Field(
        default="*",
        description="CIS recommends not '*'; docker-internal networks may need it",
    )

    def render_conf(self) -> str:
        """Render a postgresql.conf text body."""
        lines = [
            "# postgresql.conf - generated, do not edit by hand",
            "",
            "# --- Connections ---",
            f"listen_addresses = '{self.listen_addresses}'",
            f"max_connections = {self.connections.max_connections}",
            "superuser_reserved_connections = "
            f"{self.connections.superuser_reserved_connections}",
            "",
            "# --- Memory (pgtune: shared_buffers ~25% RAM) ---",
            f"shared_buffers = {self.memory.shared_buffers}",
            f"effective_cache_size = {self.memory.effective_cache_size}",
            f"work_mem = {self.memory.work_mem}",
            f"maintenance_work_mem = {self.memory.maintenance_work_mem}",
            "",
            "# --- WAL ---",
            f"wal_level = {self.wal.wal_level}",
            f"checkpoint_completion_target = {self.wal.checkpoint_completion_target}",
            f"max_wal_size = {self.wal.max_wal_size}",
            "",
            "# --- Security (CIS PostgreSQL Benchmark L1) ---",
            f"ssl = {_pg_bool(self.security.ssl)}",
            f"password_encryption = {self.security.password_encryption}",
            f"ssl_min_protocol_version = '{self.security.ssl_min_protocol_version}'",
            "",
            "# --- Logging (CIS PostgreSQL Benchmark L1 section 2) ---",
            f"log_destination = '{self.logging.log_destination}'",
            f"log_statement = '{self.logging.log_statement}'",
            f"log_min_duration_statement = {self.logging.log_min_duration_statement}",
            f"log_connections = {_pg_bool(self.security.log_connections)}",
            f"log_disconnections = {_pg_bool(self.security.log_disconnections)}",
            "",
        ]
        return "\n".join(lines)


class PostgresHbaRule(BaseModel):
    """One pg_hba.conf rule.

    Source: PostgreSQL docs, "The pg_hba.conf File". CIS L1 requires
    scram-sha-256 (not md5/trust) for non-local connections.
    """

    type: Literal["local", "host", "hostssl", "hostnossl"]
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    address: str | None = Field(
        default=None, description="CIDR or hostname; None for local rules"
    )
    auth_method: Literal["trust", "reject", "scram-sha-256", "md5", "peer", "cert"]

    def render(self) -> str:
        parts = [self.type, self.database, self.user]
        if self.address is not None:
            parts.append(self.address)
        parts.append(self.auth_method)
        return "\t".join(parts)


class PostgresHbaConfig(BaseModel):
    """Complete pg_hba.conf specification."""

    rules: list[PostgresHbaRule] = Field(min_length=1)

    def render_hba(self) -> str:
        """Render a pg_hba.conf text body."""
        header = [
            "# pg_hba.conf - generated, do not edit by hand",
            "# TYPE\tDATABASE\tUSER\tADDRESS\tMETHOD",
        ]
        return "\n".join(header + [rule.render() for rule in self.rules]) + "\n"
