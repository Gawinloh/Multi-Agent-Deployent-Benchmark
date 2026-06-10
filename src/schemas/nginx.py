"""nginx configuration schemas.

Pure data containers plus a render method. Authoritative sources:

- Core/worker tuning: nginx docs (https://nginx.org/en/docs/ngx_core_module.html).
- TLS settings: Mozilla SSL Configuration Generator ("intermediate"
  profile) — https://ssl-config.mozilla.org/.
- Security hardening: CIS nginx Benchmark Level 1.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _onoff(value: bool) -> str:
    return "on" if value else "off"


class NginxWorkerParams(BaseModel):
    """Worker process tuning. Source: nginx core module docs;
    'auto' binds one worker per CPU core."""

    worker_processes: str | int = Field(description='"auto" or a positive int')
    worker_connections: int = Field(gt=0, le=1_000_000)

    @field_validator("worker_processes")
    @classmethod
    def _check_worker_processes(cls, v: str | int) -> str | int:
        if isinstance(v, int):
            if v <= 0:
                raise ValueError("worker_processes int must be positive")
        elif v != "auto":
            raise ValueError('worker_processes must be "auto" or a positive int')
        return v


class NginxHttpParams(BaseModel):
    """HTTP-level performance settings. Source: nginx http module docs."""

    sendfile: bool
    tcp_nopush: bool
    tcp_nodelay: bool
    keepalive_timeout: int = Field(ge=0, le=3600)
    keepalive_requests: int = Field(gt=0)
    gzip: bool


class NginxSecurityParams(BaseModel):
    """Security hardening. Source: CIS nginx Benchmark L1
    (information disclosure and request limits)."""

    server_tokens: bool = Field(description="rendered as on/off; CIS requires off")
    autoindex: bool = Field(description="CIS requires off")
    client_max_body_size: str = Field(pattern=r"^\d+[kKmMgG]?$", description='e.g. "10m"')


class NginxSSLParams(BaseModel):
    """TLS settings. Source: Mozilla SSL Configuration Generator,
    intermediate profile; CIS nginx Benchmark L1 section 4."""

    protocols: list[str] = Field(min_length=1)
    ciphers: str = Field(min_length=1)
    prefer_server_ciphers: bool
    session_cache: str = Field(description='e.g. "shared:SSL:10m"')
    session_timeout: str = Field(description='e.g. "1d"')
    stapling: bool

    @field_validator("protocols")
    @classmethod
    def _check_protocols(cls, v: list[str]) -> list[str]:
        allowed = {"TLSv1", "TLSv1.1", "TLSv1.2", "TLSv1.3"}
        for proto in v:
            if proto not in allowed:
                raise ValueError(f"Unknown TLS protocol {proto!r}; expected {allowed}")
        return v


class NginxConfig(BaseModel):
    """Complete nginx.conf specification."""

    worker: NginxWorkerParams
    http: NginxHttpParams
    security: NginxSecurityParams
    ssl: NginxSSLParams

    def render_conf(self) -> str:
        """Render an nginx.conf text body."""
        lines = [
            "# nginx.conf - generated, do not edit by hand",
            "",
            f"worker_processes {self.worker.worker_processes};",
            "",
            "events {",
            f"    worker_connections {self.worker.worker_connections};",
            "}",
            "",
            "http {",
            "    include /etc/nginx/mime.types;",
            "    default_type application/octet-stream;",
            "",
            "    # --- Performance ---",
            f"    sendfile {_onoff(self.http.sendfile)};",
            f"    tcp_nopush {_onoff(self.http.tcp_nopush)};",
            f"    tcp_nodelay {_onoff(self.http.tcp_nodelay)};",
            f"    keepalive_timeout {self.http.keepalive_timeout};",
            f"    keepalive_requests {self.http.keepalive_requests};",
            f"    gzip {_onoff(self.http.gzip)};",
            "",
            "    # --- Security (CIS nginx Benchmark L1) ---",
            f"    server_tokens {_onoff(self.security.server_tokens)};",
            f"    autoindex {_onoff(self.security.autoindex)};",
            f"    client_max_body_size {self.security.client_max_body_size};",
            "",
            "    # --- TLS (Mozilla SSL intermediate profile) ---",
            f"    ssl_protocols {' '.join(self.ssl.protocols)};",
            f"    ssl_ciphers '{self.ssl.ciphers}';",
            f"    ssl_prefer_server_ciphers {_onoff(self.ssl.prefer_server_ciphers)};",
            f"    ssl_session_cache {self.ssl.session_cache};",
            f"    ssl_session_timeout {self.ssl.session_timeout};",
            f"    ssl_stapling {_onoff(self.ssl.stapling)};",
            "",
            "    access_log /var/log/nginx/access.log;",
            "",
            "    server {",
            "        listen 80;",
            "        root /usr/share/nginx/html;",
            "        index index.html;",
            "    }",
            "}",
            "",
        ]
        return "\n".join(lines)
