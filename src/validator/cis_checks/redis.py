"""CIS Redis Benchmark Level 1 checks.

Control IDs are adapted from the CIS Redis Benchmark. Strategy: checks
parse the loaded redis.conf (read from inside the container) rather than
using ``CONFIG GET``, because one of the controls being verified is that
CONFIG itself is renamed or disabled — a hardened instance can't answer
runtime CONFIG queries. One runtime probe verifies authentication is
actually enforced.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import structlog

from src.schemas.validator_report import CISCheckResult
from src.validator.docker_runner import StackRunner

logger = structlog.get_logger(__name__)

SERVICE = "redis"

CONFIG_PATH = "/usr/local/etc/redis/redis.conf"

#: Commands CIS requires renamed or disabled.
DANGEROUS_COMMANDS = ("FLUSHALL", "FLUSHDB", "CONFIG", "EVAL", "DEBUG", "SHUTDOWN")


class RedisCISChecker:
    """Runs all Redis CIS L1 checks against a deployed stack."""

    def __init__(self, runner: StackRunner) -> None:
        self._runner = runner
        self._config_cache: str | None = None

    # -- helpers ------------------------------------------------------------

    def _config(self) -> str:
        """The loaded redis.conf, comments stripped (cached per run)."""
        if self._config_cache is None:
            result = self._runner.exec_in(SERVICE, ["cat", CONFIG_PATH])
            if result.exit_code == 0:
                self._config_cache = "\n".join(
                    line for line in result.stdout.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                )
            else:
                self._config_cache = ""
        return self._config_cache

    def _directive(self, name: str) -> str | None:
        """First value of a directive, or None if absent."""
        match = re.search(
            rf"^{re.escape(name)}\s+(.*)$", self._config(), re.MULTILINE
        )
        return match.group(1).strip() if match else None

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

    # -- network exposure -------------------------------------------------------

    def check_protected_mode(self) -> CISCheckResult:
        """CIS Redis: protected-mode yes."""
        value = self._directive("protected-mode")
        return self._result(
            "1.1", "protected-mode on", value == "yes", f"protected-mode {value}"
        )

    def check_bind_not_wildcard(self) -> CISCheckResult:
        """CIS Redis: not bound to 0.0.0.0 / * on the default port."""
        value = self._directive("bind")
        passed = value is not None and "0.0.0.0" not in value and "*" not in value
        return self._result(
            "1.2", "not bound to 0.0.0.0", passed, f"bind {value}"
        )

    # -- authentication ------------------------------------------------------------

    def check_auth_configured(self) -> CISCheckResult:
        """CIS Redis: requirepass set OR ACL users configured."""
        requirepass = self._directive("requirepass")
        acl_users = re.search(r"^user\s+\S+", self._config(), re.MULTILINE)
        aclfile = self._directive("aclfile")
        passed = bool(requirepass or acl_users or aclfile)
        evidence = (
            "requirepass set" if requirepass
            else (acl_users.group(0) if acl_users
                  else (f"aclfile {aclfile}" if aclfile else "no auth configured"))
        )
        return self._result("2.1", "requirepass or ACLs configured", passed, evidence)

    def check_auth_enforced_runtime(self) -> CISCheckResult:
        """CIS Redis: unauthenticated PING is rejected at runtime."""
        result = self._runner.exec_in(SERVICE, ["redis-cli", "ping"])
        output = (result.stdout + result.stderr).strip()
        passed = "NOAUTH" in output or "WRONGPASS" in output
        return self._result(
            "2.2", "authentication enforced at runtime", passed,
            f"unauthenticated ping -> {output[:200]}",
        )

    # -- dangerous commands -----------------------------------------------------------

    def check_dangerous_commands(self) -> CISCheckResult:
        """CIS Redis: FLUSHALL/FLUSHDB/CONFIG/EVAL/DEBUG/SHUTDOWN renamed
        or disabled via rename-command."""
        config = self._config()
        renamed = {
            m.group(1).upper()
            for m in re.finditer(r"^rename-command\s+(\S+)", config, re.MULTILINE)
        }
        missing = [cmd for cmd in DANGEROUS_COMMANDS if cmd not in renamed]
        return self._result(
            "3.1", "dangerous commands renamed or disabled", not missing,
            f"not renamed: {', '.join(missing) or 'none'}",
        )

    # -- durability / memory ---------------------------------------------------------------

    def check_durability(self) -> CISCheckResult:
        """CIS Redis: appendonly on OR an RDB save schedule configured."""
        appendonly = self._directive("appendonly")
        save = re.search(r"^save\s+\d+\s+\d+", self._config(), re.MULTILINE)
        passed = appendonly == "yes" or bool(save)
        evidence = f"appendonly {appendonly}; save {'present' if save else 'absent'}"
        return self._result("4.1", "durability configured (AOF or RDB)", passed, evidence)

    def check_maxmemory_set(self) -> CISCheckResult:
        """CIS Redis: maxmemory set non-zero (prevents unbounded growth)."""
        value = self._directive("maxmemory")
        passed = value is not None and value.strip() not in ("", "0")
        return self._result("4.2", "maxmemory set (non-zero)", passed, f"maxmemory {value}")

    def check_maxmemory_policy(self) -> CISCheckResult:
        """CIS Redis: maxmemory-policy not noeviction (for cache use,
        noeviction turns memory pressure into write errors)."""
        value = self._directive("maxmemory-policy")
        passed = value is not None and value != "noeviction"
        return self._result(
            "4.3", "maxmemory-policy not noeviction", passed,
            f"maxmemory-policy {value}",
        )

    # -- entry point --------------------------------------------------------------------------

    def run_all(self) -> list[CISCheckResult]:
        checks: list[Callable[[], CISCheckResult]] = [
            self.check_protected_mode,
            self.check_bind_not_wildcard,
            self.check_auth_configured,
            self.check_auth_enforced_runtime,
            self.check_dangerous_commands,
            self.check_durability,
            self.check_maxmemory_set,
            self.check_maxmemory_policy,
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
