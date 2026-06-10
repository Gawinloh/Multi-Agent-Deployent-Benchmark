"""CIS nginx Benchmark Level 1 checks.

Control IDs are adapted from the CIS nginx Benchmark. Strategy is a mix
of parsing the served config (``cat /etc/nginx/nginx.conf`` inside the
container — the file the server actually loaded) and runtime probing via
``curl`` from inside the container.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import structlog

from src.schemas.validator_report import CISCheckResult
from src.validator.docker_runner import StackRunner

logger = structlog.get_logger(__name__)

SERVICE = "nginx"


def _strip_comments(config: str) -> str:
    """Remove #-comments so commented-out directives don't false-match."""
    return "\n".join(line.split("#", 1)[0] for line in config.splitlines())


class NginxCISChecker:
    """Runs all nginx CIS L1 checks against a deployed stack."""

    def __init__(self, runner: StackRunner) -> None:
        self._runner = runner
        self._config_cache: str | None = None

    # -- helpers -----------------------------------------------------------

    def _config(self) -> str:
        """The served nginx.conf, comments stripped (cached per run)."""
        if self._config_cache is None:
            result = self._runner.exec_in(SERVICE, ["cat", "/etc/nginx/nginx.conf"])
            self._config_cache = _strip_comments(result.stdout) if result.exit_code == 0 else ""
        return self._config_cache

    def _curl_headers(self) -> str:
        """Response headers from a runtime probe of the served site."""
        result = self._runner.exec_in(
            SERVICE, ["curl", "-s", "-I", "http://localhost/"]
        )
        return result.stdout if result.exit_code == 0 else ""

    @staticmethod
    def _result(
        control_id: str, name: str, passed: bool, evidence: str
    ) -> CISCheckResult:
        return CISCheckResult(
            control_id=control_id,
            name=name,
            level=1,
            passed=passed,
            evidence=evidence or "<no output>",
            service=SERVICE,
        )

    # -- 2.x information disclosure -----------------------------------------

    def check_server_tokens(self) -> CISCheckResult:
        """CIS nginx 2.5.1: server_tokens off — runtime probe: the Server
        response header must not reveal an nginx version number."""
        headers = self._curl_headers()
        server_lines = [
            line for line in headers.splitlines() if line.lower().startswith("server:")
        ]
        evidence = "; ".join(server_lines) or headers[:200]
        version_disclosed = any(
            re.search(r"nginx/\d", line) for line in server_lines
        )
        passed = bool(server_lines) and not version_disclosed
        return self._result(
            "2.5.1", "server_tokens off (no version in Server header)", passed, evidence
        )

    def check_autoindex(self) -> CISCheckResult:
        """CIS nginx 2.5.2: autoindex off globally."""
        config = self._config()
        enabled = re.search(r"\bautoindex\s+on\b", config)
        return self._result(
            "2.5.2", "autoindex off",
            bool(config) and not enabled,
            enabled.group(0) if enabled else "no 'autoindex on' found",
        )

    # -- 4.x TLS ---------------------------------------------------------------

    def check_ssl_protocols(self) -> CISCheckResult:
        """CIS nginx 4.1.x: only TLSv1.2 / TLSv1.3 enabled."""
        config = self._config()
        match = re.search(r"ssl_protocols\s+([^;]+);", config)
        if not match:
            return self._result(
                "4.1.1", "ssl_protocols restricted to TLSv1.2/1.3", False,
                "no ssl_protocols directive",
            )
        protocols = match.group(1).split()
        passed = bool(protocols) and all(
            p in ("TLSv1.2", "TLSv1.3") for p in protocols
        )
        return self._result(
            "4.1.1", "ssl_protocols restricted to TLSv1.2/1.3", passed,
            f"ssl_protocols {' '.join(protocols)}",
        )

    def check_prefer_server_ciphers(self) -> CISCheckResult:
        """CIS nginx 4.1.x: ssl_prefer_server_ciphers on."""
        config = self._config()
        match = re.search(r"ssl_prefer_server_ciphers\s+(on|off)\s*;", config)
        passed = bool(match) and match.group(1) == "on"
        return self._result(
            "4.1.2", "ssl_prefer_server_ciphers on", passed,
            match.group(0) if match else "directive missing",
        )

    # -- security headers ---------------------------------------------------------

    def _check_header(
        self, control_id: str, header: str
    ) -> CISCheckResult:
        """A security header must be configured via add_header (config) or
        observed in a runtime probe."""
        config = self._config()
        in_config = re.search(
            rf"add_header\s+{re.escape(header)}\b", config, re.IGNORECASE
        )
        probed = re.search(rf"^{re.escape(header)}:", self._curl_headers(),
                           re.IGNORECASE | re.MULTILINE)
        passed = bool(in_config or probed)
        evidence = (
            in_config.group(0) if in_config
            else (probed.group(0) if probed else f"{header} not configured")
        )
        return self._result(control_id, f"{header} header configured", passed, evidence)

    def check_x_frame_options(self) -> CISCheckResult:
        """CIS nginx 5.3.1."""
        return self._check_header("5.3.1", "X-Frame-Options")

    def check_x_content_type_options(self) -> CISCheckResult:
        """CIS nginx 5.3.2."""
        return self._check_header("5.3.2", "X-Content-Type-Options")

    def check_hsts(self) -> CISCheckResult:
        """CIS nginx 5.3.3 (paired with 4.1.x TLS controls)."""
        return self._check_header("5.3.3", "Strict-Transport-Security")

    # -- request handling ----------------------------------------------------------

    def check_https_redirect(self) -> CISCheckResult:
        """CIS nginx 4.1.x: HTTP requests redirected to HTTPS with 301."""
        config = self._config()
        redirect = re.search(r"return\s+301\s+https://", config)
        return self._result(
            "4.1.3", "HTTP to HTTPS 301 redirect present", bool(redirect),
            redirect.group(0) if redirect else "no 'return 301 https://...' found",
        )

    def check_client_max_body_size(self) -> CISCheckResult:
        """CIS nginx 2.4.x: client_max_body_size set explicitly."""
        config = self._config()
        match = re.search(r"client_max_body_size\s+\S+\s*;", config)
        return self._result(
            "2.4.1", "client_max_body_size set explicitly", bool(match),
            match.group(0) if match else "directive missing",
        )

    def check_access_log(self) -> CISCheckResult:
        """CIS nginx 3.1: access logging enabled (not 'access_log off')."""
        config = self._config()
        disabled = re.search(r"access_log\s+off\s*;", config)
        enabled = re.search(r"access_log\s+\S+", config)
        passed = bool(config) and bool(enabled) and not disabled
        return self._result(
            "3.1", "access_log enabled", passed,
            (disabled or enabled).group(0) if (disabled or enabled) else "no access_log",
        )

    # -- entry point --------------------------------------------------------------------

    def run_all(self) -> list[CISCheckResult]:
        checks: list[Callable[[], CISCheckResult]] = [
            self.check_server_tokens,
            self.check_autoindex,
            self.check_ssl_protocols,
            self.check_prefer_server_ciphers,
            self.check_x_frame_options,
            self.check_x_content_type_options,
            self.check_hsts,
            self.check_https_redirect,
            self.check_client_max_body_size,
            self.check_access_log,
        ]
        results = []
        for check in checks:
            try:
                result = check()
            except Exception as exc:  # noqa: BLE001 — a crashed check is a failed check
                result = self._result("?", f"{check.__name__} crashed", False, str(exc))
            logger.info(
                "cis_check_complete", service=SERVICE,
                control=result.control_id, passed=result.passed,
            )
            results.append(result)
        return results
