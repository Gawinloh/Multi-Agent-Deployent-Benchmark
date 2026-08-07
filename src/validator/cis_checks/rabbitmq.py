"""Security baseline checks for RabbitMQ.

**There is no published CIS Benchmark for RabbitMQ.** Every other
service in this project scores against a CIS document; RabbitMQ has
none, so the controls below are derived from RabbitMQ's own
documentation and each check's docstring names the specific page and
section it implements. This is the documented fallback in the
methodology: "security-baseline checks derived from vendor hardening
guides", and the distinction must be stated wherever RabbitMQ's score is
reported next to a CIS-derived one.

Sources, cited per control:

- **PC** — Production Checklist, https://www.rabbitmq.com/docs/production-checklist
- **AC** — Access Control, https://www.rabbitmq.com/docs/access-control
- **NET** — Networking, https://www.rabbitmq.com/docs/networking
- **MGMT** — Management Plugin, https://www.rabbitmq.com/docs/management
- **TLS** — TLS Support, https://www.rabbitmq.com/docs/ssl
- **MEM** — Memory Alarms, https://www.rabbitmq.com/docs/memory
- **DISK** — Disk Alarms, https://www.rabbitmq.com/docs/disk-alarms

Control IDs are project-assigned (1.x network, 2.x authentication and
access, 3.x management surface, 4.x resource limits, 5.x TLS, 6.x
inter-node), grouped to read like the CIS-derived checkers. They are not
CIS numbers and must not be cited as such.

Strategy mirrors the Redis checker: parse the rabbitmq.conf actually
loaded inside the container, plus runtime probes where the config alone
cannot prove the property. Every CLI call goes through ``gosu rabbitmq``
— see :data:`_CLI_PREFIX`.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import structlog

from src.schemas.validator_report import CISCheckResult
from src.validator.docker_runner import StackRunner

logger = structlog.get_logger(__name__)

SERVICE = "rabbitmq"

CONFIG_PATH = "/etc/rabbitmq/rabbitmq.conf"

#: RabbitMQ's CLI tools are Erlang nodes: they read (and, if absent,
#: *create*) $HOME/.erlang.cookie. ``docker exec`` runs as root, so a
#: root-run CLI call on a node that has not yet written its cookie
#: creates a root-owned 0400 file that the broker itself — running as
#: `rabbitmq` — then cannot read, and the node dies with
#: "Error when reading /var/lib/rabbitmq/.erlang.cookie: eacces".
#: Observed repeatedly while building this checker. Dropping to the
#: rabbitmq user makes every call safe regardless of timing.
_CLI_PREFIX = ("gosu", "rabbitmq")

#: The well-known default credentials RabbitMQ ships with.
DEFAULT_USERNAME = "guest"
DEFAULT_PASSWORD = "guest"


class RabbitMQCISChecker:
    """Runs all RabbitMQ security-baseline checks against a deployed stack.

    Named for symmetry with the CIS checkers it sits alongside in the
    catalog; the controls are vendor-derived, not CIS. See the module
    docstring.
    """

    def __init__(self, runner: StackRunner) -> None:
        self._runner = runner
        self._config_cache: str | None = None
        self._cli_cache: dict[tuple[str, ...], tuple[str, int]] = {}

    # -- helpers ------------------------------------------------------------

    def _cli(self, *args: str) -> tuple[str, int]:
        """Run a RabbitMQ CLI tool as the rabbitmq user."""
        result = self._runner.exec_in(SERVICE, [*_CLI_PREFIX, *args])
        return (result.stdout + result.stderr).strip(), result.exit_code

    def _cli_cached(self, *args: str) -> tuple[str, int]:
        """As :meth:`_cli`, but run each distinct command only once.

        Every RabbitMQ CLI invocation starts a short-lived Erlang node and
        connects to the broker, which costs 1.5-3s — two orders of
        magnitude more than the ``psql`` and ``redis-cli`` calls the other
        checkers make. ``listeners`` backs three controls and
        ``list_users`` two, so caching removes roughly a third of this
        checker's wall-clock. Only for commands that read state and do not
        change it; ``authenticate_user`` is deliberately not cached.
        """
        if args not in self._cli_cache:
            self._cli_cache[args] = self._cli(*args)
        return self._cli_cache[args]

    def _config(self) -> str:
        """The loaded rabbitmq.conf, comments stripped (cached per run)."""
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

    def _setting(self, key: str) -> str | None:
        """Value of a ``key = value`` setting, or None if absent."""
        match = re.search(
            rf"^{re.escape(key)}\s*=\s*(.*)$", self._config(), re.MULTILINE
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

    # -- 1.x network ---------------------------------------------------------

    def check_heartbeat_enabled(self) -> CISCheckResult:
        """NET "Heartbeats" / PC "Networking": a non-zero heartbeat lets the
        broker detect dead TCP peers instead of leaking their connections."""
        value = self._setting("heartbeat")
        passed = value is not None and value.isdigit() and int(value) > 0
        return self._result(
            "1.1", "heartbeat enabled (non-zero)", passed, f"heartbeat = {value}"
        )

    def check_connection_limit(self) -> CISCheckResult:
        """PC "Resource limits": an unbounded connection count lets a
        misbehaving client exhaust file descriptors and memory."""
        value = self._setting("max_connections")
        passed = value is not None and value.isdigit() and int(value) > 0
        return self._result(
            "1.2", "max_connections bounded", passed, f"max_connections = {value}"
        )

    # -- 2.x authentication and access --------------------------------------

    def check_guest_user_absent(self) -> CISCheckResult:
        """PC "Users": the default guest/guest account must not survive into
        production. Naming any other ``default_user`` means the node never
        creates guest at all, which this verifies at runtime."""
        output, exit_code = self._cli_cached("rabbitmqctl", "-q", "list_users")
        if exit_code != 0:
            return self._result(
                "2.1", "default guest user absent", False,
                f"list_users failed: {output[:200]}",
            )
        users = {
            line.split("\t")[0].strip()
            for line in output.splitlines()
            if line.strip() and not line.startswith("Listing")
        }
        passed = DEFAULT_USERNAME not in users
        return self._result(
            "2.1", "default guest user absent", passed,
            f"users: {', '.join(sorted(users)) or '<none>'}",
        )

    def check_default_user_renamed(self) -> CISCheckResult:
        """PC "Users": config-level counterpart of 2.1 — the seeded account
        is named something other than guest."""
        value = self._setting("default_user")
        passed = value is not None and value != DEFAULT_USERNAME
        return self._result(
            "2.2", "default_user is not 'guest'", passed, f"default_user = {value}"
        )

    def check_default_password_changed(self) -> CISCheckResult:
        """PC "Users": the shipped password must not be reused, whatever the
        account is called."""
        value = self._setting("default_pass")
        passed = value is not None and value != DEFAULT_PASSWORD
        return self._result(
            "2.3", "default password not the shipped 'guest'", passed,
            "default_pass is set and differs from the default"
            if passed else f"default_pass = {value}",
        )

    def check_default_credentials_rejected(self) -> CISCheckResult:
        """AC "Authentication": runtime proof that guest/guest cannot log
        in, regardless of what the config claims."""
        output, exit_code = self._cli(
            "rabbitmqctl", "authenticate_user", DEFAULT_USERNAME, DEFAULT_PASSWORD
        )
        passed = exit_code != 0
        return self._result(
            "2.4", "guest/guest login refused at runtime", passed,
            f"authenticate_user guest guest -> exit {exit_code}: {output[:160]}",
        )

    def check_least_privilege_user(self) -> CISCheckResult:
        """AC "Authorisation": the application account should hold scoped
        permissions on a dedicated vhost rather than administrator on '/'.

        UNREACHABLE — see UNREACHABLE_CONTROLS. RabbitMQ seeds
        ``default_user`` as an administrator on '/', and RabbitMQConfig
        exposes no vhost, permission or user-tag fields.
        """
        output, exit_code = self._cli_cached("rabbitmqctl", "-q", "list_users")
        administrators = [
            line for line in output.splitlines() if "administrator" in line
        ]
        passed = exit_code == 0 and not administrators
        return self._result(
            "2.5", "no account holds blanket administrator tag", passed,
            f"administrator accounts: {len(administrators)}",
        )

    # -- 3.x management surface ---------------------------------------------

    def check_management_listener_configured(self) -> CISCheckResult:
        """MGMT "Configuration" / PC: the management HTTP API is a full
        administrative surface and should bind an internal interface. Not
        configuring it at all leaves the plugin on the wildcard, so an
        absent setting is a failure, not an exemption."""
        value = self._setting("management.tcp.ip")
        passed = value is not None and value not in ("0.0.0.0", "::", "*")
        return self._result(
            "3.1", "management listener not on a wildcard interface", passed,
            f"management.tcp.ip = {value}",
        )

    def check_management_listener_runtime(self) -> CISCheckResult:
        """MGMT: runtime counterpart of 3.1 — what the node actually bound,
        read back from rabbitmq-diagnostics rather than from the config."""
        output, exit_code = self._cli_cached("rabbitmq-diagnostics", "-q", "listeners")
        if exit_code != 0:
            return self._result(
                "3.2", "management listener bound to an internal interface",
                False, f"listeners failed: {output[:200]}",
            )
        http = [
            line for line in output.splitlines()
            if "protocol: http," in line or "HTTP API" in line
        ]
        exposed = [
            line for line in http
            if re.search(r"Interface:\s*(0\.0\.0\.0|\[::\]|\*)", line)
        ]
        passed = bool(http) and not exposed
        return self._result(
            "3.2", "management listener bound to an internal interface", passed,
            "; ".join(line.strip() for line in http)[:250] or "no HTTP listener found",
        )

    def check_management_plugin_disabled(self) -> CISCheckResult:
        """PC "Plugins": an unused management plugin should not be loaded at
        all, since it is the largest remote attack surface on the node.

        UNREACHABLE — see UNREACHABLE_CONTROLS. The harness deploys
        ``rabbitmq:4-management``, which enables rabbitmq_management at
        image build time; no specification can unload it.
        """
        output, exit_code = self._cli(
            "rabbitmq-plugins", "-q", "is_enabled", "rabbitmq_management"
        )
        passed = exit_code != 0
        return self._result(
            "3.3", "management plugin not loaded", passed,
            f"is_enabled rabbitmq_management -> exit {exit_code}: {output[:160]}",
        )

    # -- 4.x resource limits -------------------------------------------------

    def check_memory_watermark(self) -> CISCheckResult:
        """MEM "Configuring the Memory Threshold": a watermark below 1.0
        leaves headroom for the OS; at 1.0 the alarm never fires before the
        kernel OOM-kills the node."""
        value = self._setting("vm_memory_high_watermark.relative")
        try:
            passed = value is not None and 0 < float(value) < 1.0
        except ValueError:
            passed = False
        return self._result(
            "4.1", "memory high watermark below 1.0", passed,
            f"vm_memory_high_watermark.relative = {value}",
        )

    def check_disk_free_limit(self) -> CISCheckResult:
        """DISK "Configuring the Disk Free Space Limit": without a floor the
        node keeps accepting publishes until the disk fills and the
        message store is corrupted."""
        value = self._setting("disk_free_limit.absolute")
        passed = value is not None and bool(re.match(r"^\d", value.strip()))
        return self._result(
            "4.2", "disk free limit configured", passed,
            f"disk_free_limit.absolute = {value}",
        )

    # -- 5.x TLS -------------------------------------------------------------

    def check_tls_listener(self) -> CISCheckResult:
        """TLS "Enabling TLS" / PC "Security": client traffic should have an
        AMQPS listener available. Verified at runtime so a configured but
        unbound listener does not pass."""
        output, exit_code = self._cli_cached("rabbitmq-diagnostics", "-q", "listeners")
        passed = exit_code == 0 and "amqp/ssl" in output
        return self._result(
            "5.1", "TLS (AMQPS) listener enabled", passed,
            "amqp/ssl listener present" if passed else "no amqp/ssl listener",
        )

    def check_tls_peer_verification(self) -> CISCheckResult:
        """TLS "Peer Verification": ``verify_none`` accepts any client
        certificate, so a TLS listener without verification provides
        encryption but no authentication of the peer."""
        enabled = "listeners.ssl.default" in self._config()
        verify = self._setting("ssl_options.verify")
        passed = enabled and verify == "verify_peer"
        return self._result(
            "5.2", "TLS peer verification enabled", passed,
            f"tls listener {'configured' if enabled else 'absent'}; "
            f"ssl_options.verify = {verify}",
        )

    # -- 6.x inter-node ------------------------------------------------------

    def check_distribution_port_restricted(self) -> CISCheckResult:
        """NET "Erlang Distribution": the clustering / CLI port (25672)
        grants full node control to anyone holding the cookie and should
        not be reachable beyond the cluster's own network.

        UNREACHABLE — see UNREACHABLE_CONTROLS. RabbitMQConfig exposes no
        ``distribution.listener.*`` fields, so no specification can move
        or restrict it.
        """
        output, exit_code = self._cli_cached("rabbitmq-diagnostics", "-q", "listeners")
        clustering = [line for line in output.splitlines() if "clustering" in line]
        exposed = [
            line for line in clustering
            if re.search(r"Interface:\s*(0\.0\.0\.0|\[::\]|\*)", line)
        ]
        passed = exit_code == 0 and bool(clustering) and not exposed
        return self._result(
            "6.1", "Erlang distribution port not on a wildcard interface", passed,
            "; ".join(line.strip() for line in clustering)[:200] or "none found",
        )

    # -- entry point ---------------------------------------------------------

    def run_all(self) -> list[CISCheckResult]:
        checks: list[Callable[[], CISCheckResult]] = [
            self.check_heartbeat_enabled,
            self.check_connection_limit,
            self.check_guest_user_absent,
            self.check_default_user_renamed,
            self.check_default_password_changed,
            self.check_default_credentials_rejected,
            self.check_least_privilege_user,
            self.check_management_listener_configured,
            self.check_management_listener_runtime,
            self.check_management_plugin_disabled,
            self.check_memory_watermark,
            self.check_disk_free_limit,
            self.check_tls_listener,
            self.check_tls_peer_verification,
            self.check_distribution_port_restricted,
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
