"""CIS PostgreSQL Benchmark Level 1 checks.

Control IDs are adapted from the CIS PostgreSQL 16 Benchmark. Each check
runs SQL via ``psql`` inside the deployed container (or reads pg_hba.conf)
and returns a :class:`~src.schemas.validator_report.CISCheckResult` with
raw output as evidence.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from src.schemas.validator_report import CISCheckResult
from src.validator.docker_runner import StackRunner

logger = structlog.get_logger(__name__)

SERVICE = "postgres"

#: log_statement values that satisfy "at least ddl".
_ACCEPTABLE_LOG_STATEMENT = ("ddl", "mod", "all")


class PostgresCISChecker:
    """Runs all PostgreSQL CIS L1 checks against a deployed stack."""

    def __init__(self, runner: StackRunner) -> None:
        self._runner = runner

    # -- helpers ---------------------------------------------------------

    def _psql(self, sql: str) -> tuple[str, bool]:
        """Run SQL via psql -tAc; returns (stripped stdout, success)."""
        result = self._runner.exec_in(
            SERVICE, ["psql", "-U", "postgres", "-tAc", sql]
        )
        return result.stdout.strip(), result.exit_code == 0

    def _show(self, parameter: str) -> str:
        value, ok = self._psql(f"SHOW {parameter}")
        return value if ok else ""

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

    # -- 1.x authentication ----------------------------------------------

    def check_hba_scram(self) -> CISCheckResult:
        """CIS PG 1.x: scram-sha-256 enforced for host rules; md5 absent."""
        result = self._runner.exec_in(
            SERVICE, ["cat", "/etc/postgresql/pg_hba.conf"]
        )
        hba = result.stdout
        active = [
            line.strip()
            for line in hba.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        md5_present = any("md5" in line for line in active)
        bad_host_rules = [
            line
            for line in active
            if line.split()[0] in ("host", "hostssl", "hostnossl")
            and line.split()[-1] not in ("scram-sha-256", "cert", "reject")
        ]
        passed = result.exit_code == 0 and not md5_present and not bad_host_rules
        evidence = "; ".join(bad_host_rules) or hba[:500]
        return self._result(
            "1.1", "pg_hba host rules use scram-sha-256 (md5 absent)", passed, evidence
        )

    def check_password_encryption(self) -> CISCheckResult:
        """CIS PG 1.x: password_encryption is scram-sha-256."""
        value = self._show("password_encryption")
        return self._result(
            "1.2", "password_encryption is scram-sha-256",
            value == "scram-sha-256", f"password_encryption = {value}",
        )

    # -- 2.x logging / auditing -------------------------------------------

    def check_log_destination(self) -> CISCheckResult:
        """CIS PG 2.x: log_destination is set."""
        value = self._show("log_destination")
        return self._result(
            "2.1", "log_destination set",
            value in ("stderr", "csvlog", "syslog", "jsonlog"),
            f"log_destination = {value}",
        )

    def check_log_statement(self) -> CISCheckResult:
        """CIS PG 2.x: log_statement at least 'ddl'."""
        value = self._show("log_statement")
        return self._result(
            "2.2", "log_statement at least ddl",
            value in _ACCEPTABLE_LOG_STATEMENT, f"log_statement = {value}",
        )

    def check_log_connections(self) -> CISCheckResult:
        """CIS PG 2.x: log_connections on."""
        value = self._show("log_connections")
        return self._result(
            "2.3", "log_connections on", value == "on", f"log_connections = {value}"
        )

    def check_log_disconnections(self) -> CISCheckResult:
        """CIS PG 2.x: log_disconnections on."""
        value = self._show("log_disconnections")
        return self._result(
            "2.4", "log_disconnections on", value == "on",
            f"log_disconnections = {value}",
        )

    def check_pgaudit_enabled(self) -> CISCheckResult:
        """CIS PG 2.x: pgAudit extension installed and active."""
        value, ok = self._psql("SELECT extname FROM pg_extension")
        passed = ok and "pgaudit" in value
        return self._result(
            "2.5", "pgAudit extension enabled", passed, f"extensions: {value}"
        )

    # -- 3.x network -------------------------------------------------------

    def check_listen_addresses(self) -> CISCheckResult:
        """CIS PG 3.x: listen_addresses not wildcard '*'."""
        value = self._show("listen_addresses")
        return self._result(
            "3.1", "listen_addresses not '*'",
            bool(value) and value != "*", f"listen_addresses = {value}",
        )

    def check_ssl_on(self) -> CISCheckResult:
        """CIS PG 3.x: ssl on."""
        value = self._show("ssl")
        return self._result("3.2", "ssl on", value == "on", f"ssl = {value}")

    def check_ssl_min_protocol(self) -> CISCheckResult:
        """CIS PG 3.x: ssl_min_protocol_version >= TLSv1.2."""
        value = self._show("ssl_min_protocol_version")
        return self._result(
            "3.3", "ssl_min_protocol_version >= TLSv1.2",
            value in ("TLSv1.2", "TLSv1.3"),
            f"ssl_min_protocol_version = {value}",
        )

    # -- 4.x permissions ----------------------------------------------------

    def check_db_ownership(self) -> CISCheckResult:
        """CIS PG 4.x: postgres superuser owns no application databases."""
        value, ok = self._psql(
            "SELECT datname FROM pg_database d JOIN pg_roles r ON d.datdba = r.oid "
            "WHERE r.rolname = 'postgres' "
            "AND datname NOT IN ('postgres', 'template0', 'template1')"
        )
        return self._result(
            "4.1", "postgres owns no application databases",
            ok and value == "", f"postgres-owned app databases: {value or 'none'}",
        )

    def check_no_extra_superusers(self) -> CISCheckResult:
        """CIS PG 4.x: no superuser roles besides postgres."""
        value, ok = self._psql(
            "SELECT rolname FROM pg_roles WHERE rolsuper AND rolname <> 'postgres'"
        )
        return self._result(
            "4.2", "no superuser roles besides postgres",
            ok and value == "", f"extra superusers: {value or 'none'}",
        )

    # -- 5.x runtime --------------------------------------------------------

    def check_log_min_duration(self) -> CISCheckResult:
        """CIS PG 5.x: log_min_duration_statement > 0 (slow-query logging)."""
        value = self._show("log_min_duration_statement")
        try:
            passed = int(value.rstrip("ms").strip() or "-1") > 0
        except ValueError:
            passed = False
        return self._result(
            "5.1", "log_min_duration_statement > 0", passed,
            f"log_min_duration_statement = {value}",
        )

    def check_statement_timeout(self) -> CISCheckResult:
        """CIS PG 5.x: statement_timeout set (non-zero)."""
        value = self._show("statement_timeout")
        return self._result(
            "5.2", "statement_timeout set", value not in ("", "0"),
            f"statement_timeout = {value}",
        )

    # -- entry point ----------------------------------------------------------

    def run_all(self) -> list[CISCheckResult]:
        checks: list[Callable[[], CISCheckResult]] = [
            self.check_hba_scram,
            self.check_password_encryption,
            self.check_log_destination,
            self.check_log_statement,
            self.check_log_connections,
            self.check_log_disconnections,
            self.check_pgaudit_enabled,
            self.check_listen_addresses,
            self.check_ssl_on,
            self.check_ssl_min_protocol,
            self.check_db_ownership,
            self.check_no_extra_superusers,
            self.check_log_min_duration,
            self.check_statement_timeout,
        ]
        results = []
        for check in checks:
            try:
                result = check()
            except Exception as exc:  # noqa: BLE001 — a crashed check is a failed check
                result = self._result(
                    "?", f"{check.__name__} crashed", False, str(exc)
                )
            logger.info(
                "cis_check_complete", service=SERVICE,
                control=result.control_id, passed=result.passed,
            )
            results.append(result)
        return results
