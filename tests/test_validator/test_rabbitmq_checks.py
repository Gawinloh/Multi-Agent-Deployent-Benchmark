"""Tests for the RabbitMQ security-baseline checker and perf-test parser.

Both are driven through a fake runner, so no Docker is needed. The
controls are exercised in both directions: a hardened config passes and
an insecure one fails. A control that can only ever return one answer is
not measuring anything.
"""

from __future__ import annotations

import pytest

from src.schemas.validator_report import BenchmarkResult
from src.validator.benchmarks.rabbitmq_perf import (
    parse_perf_test_output,
    run_rabbitmq_perf_test,
)
from src.validator.cis_checks import UNREACHABLE_CONTROLS, is_actionable
from src.validator.cis_checks.rabbitmq import RabbitMQCISChecker
from src.validator.docker_runner import CommandResult
from tests.test_validator.test_docker_runner import make_rabbitmq_config

#: Every control a specification can actually satisfy. The three
#: unreachable ones (2.5, 3.3, 6.1) are listed in UNREACHABLE_CONTROLS.
REACHABLE_CONTROLS = (
    "1.1", "1.2", "2.1", "2.2", "2.3", "2.4",
    "3.1", "3.2", "4.1", "4.2", "5.1", "5.2",
)
UNREACHABLE = ("2.5", "3.3", "6.1")

HARDENED_CONF = """\
listeners.tcp.default = 0.0.0.0:5672
heartbeat = 60
max_connections = 500
vm_memory_high_watermark.relative = 0.4
vm_memory_high_watermark_paging_ratio = 0.5
disk_free_limit.absolute = 200MB
default_user = appuser
default_pass = harness-rabbit-pass
loopback_users.guest = true
listeners.ssl.default = 5671
ssl_options.verify = verify_peer
ssl_options.fail_if_no_peer_cert = true
management.tcp.ip = 127.0.0.1
management.tcp.port = 15672
"""

INSECURE_CONF = """\
listeners.tcp.default = 0.0.0.0:5672
heartbeat = 0
vm_memory_high_watermark.relative = 1.0
default_user = guest
default_pass = guest
"""

#: Copied from a real `rabbitmq-diagnostics listeners` on rabbitmq:4-management.
#: Note the clustering listener on [::]: the image always binds Erlang
#: distribution to the wildcard and no schema field can move it, which is
#: exactly why control 6.1 is registered unreachable. A fixture that showed
#: it on a specific interface would make 6.1 look satisfiable.
HARDENED_LISTENERS = """\
Interface: 127.0.0.1, port: 15672, protocol: http, purpose: HTTP API
Interface: [::], port: 25672, protocol: clustering, purpose: inter-node
Interface: 0.0.0.0, port: 5672, protocol: amqp, purpose: AMQP 0-9-1 and AMQP 1.0
Interface: [::], port: 5671, protocol: amqp/ssl, purpose: AMQP over TLS
"""

INSECURE_LISTENERS = """\
Interface: [::], port: 15672, protocol: http, purpose: HTTP API
Interface: [::], port: 25672, protocol: clustering, purpose: inter-node
Interface: 0.0.0.0, port: 5672, protocol: amqp, purpose: AMQP 0-9-1 and AMQP 1.0
"""


class FakeRunner:
    """Answers the exact commands the checker issues."""

    def __init__(
        self,
        *,
        config: str = HARDENED_CONF,
        listeners: str = HARDENED_LISTENERS,
        users: str = "appuser\t[administrator]",
        guest_auth_ok: bool = False,
        plugin_enabled: bool = True,
    ) -> None:
        self.config = config
        self.listeners = listeners
        self.users = users
        self.guest_auth_ok = guest_auth_ok
        self.plugin_enabled = plugin_enabled
        self.calls: list[list[str]] = []

    def exec_in(self, service, command, environment=None) -> CommandResult:
        self.calls.append(command)
        joined = " ".join(command)
        if command[0] == "cat":
            return CommandResult(self.config, "", 0, 0.01)
        if "list_users" in joined:
            return CommandResult(f"user\ttags\n{self.users}", "", 0, 0.01)
        if "authenticate_user" in joined:
            return CommandResult("", "", 0 if self.guest_auth_ok else 70, 0.01)
        if "listeners" in joined:
            return CommandResult(self.listeners, "", 0, 0.01)
        if "is_enabled" in joined:
            return CommandResult("", "", 0 if self.plugin_enabled else 69, 0.01)
        return CommandResult("", "unexpected command", 1, 0.01)


def results_by_id(runner) -> dict[str, bool]:
    return {r.control_id: r.passed for r in RabbitMQCISChecker(runner).run_all()}


class TestHardenedConfiguration:
    @pytest.fixture
    def passed(self) -> dict[str, bool]:
        return results_by_id(FakeRunner())

    @pytest.mark.parametrize("control", REACHABLE_CONTROLS)
    def test_reachable_controls_pass(self, passed: dict[str, bool], control: str) -> None:
        assert passed[control] is True

    @pytest.mark.parametrize("control", UNREACHABLE)
    def test_unreachable_controls_still_fail(
        self, passed: dict[str, bool], control: str
    ) -> None:
        """They are excluded from the actionable denominator, not passed."""
        assert passed[control] is False


class TestInsecureConfiguration:
    @pytest.fixture
    def passed(self) -> dict[str, bool]:
        return results_by_id(
            FakeRunner(
                config=INSECURE_CONF,
                listeners=INSECURE_LISTENERS,
                users="guest\t[administrator]",
                guest_auth_ok=True,
            )
        )

    @pytest.mark.parametrize("control", REACHABLE_CONTROLS)
    def test_every_reachable_control_fails(
        self, passed: dict[str, bool], control: str
    ) -> None:
        assert passed[control] is False


class TestIndividualControls:
    def test_guest_user_detected_at_runtime(self) -> None:
        runner = FakeRunner(users="guest\t[administrator]\nappuser\t[]")
        assert results_by_id(runner)["2.1"] is False

    def test_guest_absent_passes_even_with_other_users(self) -> None:
        runner = FakeRunner(users="appuser\t[]\nmonitor\t[monitoring]")
        assert results_by_id(runner)["2.1"] is True

    def test_list_users_failure_is_a_failed_control_not_a_crash(self) -> None:
        class Broken(FakeRunner):
            def exec_in(self, service, command, environment=None):
                if "list_users" in " ".join(command):
                    return CommandResult("", "node down", 69, 0.01)
                return super().exec_in(service, command, environment)

        assert results_by_id(Broken())["2.1"] is False

    def test_management_on_wildcard_fails_at_runtime(self) -> None:
        runner = FakeRunner(listeners=INSECURE_LISTENERS)
        assert results_by_id(runner)["3.2"] is False

    def test_absent_management_setting_fails_rather_than_exempts(self) -> None:
        """Not configuring the listener leaves the plugin on the wildcard,
        so silence is a failure."""
        runner = FakeRunner(config=HARDENED_CONF.replace("management.tcp.ip = 127.0.0.1", ""))
        assert results_by_id(runner)["3.1"] is False

    def test_tls_listener_read_from_runtime_not_config(self) -> None:
        """A configured listener that failed to bind must not pass."""
        runner = FakeRunner(listeners=INSECURE_LISTENERS)  # config has ssl, runtime does not
        assert results_by_id(runner)["5.1"] is False

    def test_verify_none_fails_peer_verification(self) -> None:
        runner = FakeRunner(
            config=HARDENED_CONF.replace(
                "ssl_options.verify = verify_peer", "ssl_options.verify = verify_none"
            )
        )
        assert results_by_id(runner)["5.2"] is False

    def test_watermark_of_one_fails(self) -> None:
        runner = FakeRunner(
            config=HARDENED_CONF.replace(
                "vm_memory_high_watermark.relative = 0.4",
                "vm_memory_high_watermark.relative = 1.0",
            )
        )
        assert results_by_id(runner)["4.1"] is False

    def test_every_cli_call_drops_to_the_rabbitmq_user(self) -> None:
        """A root-run Erlang CLI call can create a root-owned .erlang.cookie
        that the broker cannot read, killing the node."""
        runner = FakeRunner()
        RabbitMQCISChecker(runner).run_all()
        cli_calls = [c for c in runner.calls if c[0] != "cat"]
        assert cli_calls
        for command in cli_calls:
            assert command[:2] == ["gosu", "rabbitmq"], command

    def test_all_results_are_tagged_rabbitmq(self) -> None:
        for result in RabbitMQCISChecker(FakeRunner()).run_all():
            assert result.service == "rabbitmq"

    def test_read_only_queries_run_once_each(self) -> None:
        """Each Erlang CLI call costs 1.5-3s. `listeners` backs three
        controls and `list_users` two; without caching this checker alone
        roughly doubles a Study 2 run's validation time."""
        runner = FakeRunner()
        RabbitMQCISChecker(runner).run_all()
        joined = [" ".join(c) for c in runner.calls]
        assert sum("listeners" in c for c in joined) == 1
        assert sum("list_users" in c for c in joined) == 1
        assert sum(c.startswith("cat ") for c in joined) == 1

    def test_authentication_probe_is_not_cached(self) -> None:
        """It changes no state, but it is the one control whose whole point
        is a live answer, so it stays a real call."""
        runner = FakeRunner()
        RabbitMQCISChecker(runner).run_all()
        joined = [" ".join(c) for c in runner.calls]
        assert sum("authenticate_user" in c for c in joined) == 1


class TestUnreachableRegistration:
    @pytest.mark.parametrize("control", UNREACHABLE)
    def test_registered_with_a_reason(self, control: str) -> None:
        key = f"rabbitmq {control}"
        assert key in UNREACHABLE_CONTROLS
        assert len(UNREACHABLE_CONTROLS[key]) > 40
        assert not is_actionable("rabbitmq", control)

    @pytest.mark.parametrize("control", REACHABLE_CONTROLS)
    def test_reachable_controls_stay_actionable(self, control: str) -> None:
        assert is_actionable("rabbitmq", control)

    def test_every_emitted_control_is_classified(self) -> None:
        """No control may be silently absent from both registries."""
        emitted = {r.control_id for r in RabbitMQCISChecker(FakeRunner()).run_all()}
        unreachable = {
            key.split(" ", 1)[1]
            for key in UNREACHABLE_CONTROLS
            if key.startswith("rabbitmq ")
        }
        assert unreachable <= emitted, "an unreachable control is never emitted"
        assert "?" not in emitted, "a check crashed"


PERF_OUTPUT = """\
id: test-032043-932, time 8.001 s, sent: 82407 msg/s, received: 82857 msg/s
test stopped (Reached time limit)
id: test-032043-932, sending rate avg: 48299 msg/s
id: test-032043-932, receiving rate avg: 48239 msg/s
id: test-032043-932, consumer latency min/median/75th/95th/99th/max \
4806/71105/144776/342223/377734/418935 µs
"""


class TestPerfTestParser:
    def test_throughput_from_the_receiving_rate(self) -> None:
        result = parse_perf_test_output(PERF_OUTPUT)
        assert result.error_message is None
        assert result.throughput == pytest.approx(48239.0)

    def test_latency_converted_to_milliseconds(self) -> None:
        result = parse_perf_test_output(PERF_OUTPUT)
        assert result.latency_p50 == pytest.approx(71.105)
        assert result.latency_p99 == pytest.approx(377.734)

    def test_p90_left_unset_because_perf_test_does_not_report_it(self) -> None:
        assert parse_perf_test_output(PERF_OUTPUT).latency_p90 is None

    def test_summary_without_latency_still_yields_throughput(self) -> None:
        text = "id: t, receiving rate avg: 100 msg/s\n"
        result = parse_perf_test_output(text)
        assert result.throughput == pytest.approx(100.0)
        assert result.latency_p50 is None

    def test_unparseable_output_is_an_error_not_a_crash(self) -> None:
        result = parse_perf_test_output("connection refused")
        assert result.throughput is None
        assert "could not parse" in result.error_message


class TestPerfTestRunner:
    def _runner(self):
        class R:
            network_name = "stack_test_net"

        return R()

    def test_credentials_come_from_the_spec(self, monkeypatch) -> None:
        captured = {}

        def fake_sidecar(network, uri, duration_s):
            captured["uri"] = uri
            return PERF_OUTPUT, 0

        monkeypatch.setattr(
            "src.validator.benchmarks.rabbitmq_perf._run_sidecar", fake_sidecar
        )
        config = make_rabbitmq_config()
        result = run_rabbitmq_perf_test(
            self._runner(),
            duration_s=5,
            username=config.security.default_user,
            password=config.security.default_pass,
            port=config.networking.listener_port,
        )
        assert result.throughput == pytest.approx(48239.0)
        assert captured["uri"] == (
            "amqp://appuser:harness-rabbit-pass@rabbitmq:5672"
        )

    def test_special_characters_in_the_password_are_escaped(self, monkeypatch) -> None:
        captured = {}

        def fake_sidecar(network, uri, duration_s):
            captured["uri"] = uri
            return PERF_OUTPUT, 0

        monkeypatch.setattr(
            "src.validator.benchmarks.rabbitmq_perf._run_sidecar", fake_sidecar
        )
        run_rabbitmq_perf_test(
            self._runner(), username="user", password="p@ss/word:1"
        )
        assert "p%40ss%2Fword%3A1" in captured["uri"]

    def test_access_refused_is_reported_not_swallowed(self, monkeypatch) -> None:
        """A spec that restricts its own user to loopback genuinely makes the
        broker unreachable; that is a benchmark failure, not a skip."""
        monkeypatch.setattr(
            "src.validator.benchmarks.rabbitmq_perf._run_sidecar",
            lambda network, uri, duration_s: ("ACCESS_REFUSED - Login was refused", 1),
        )
        result = run_rabbitmq_perf_test(self._runner())
        assert result.throughput is None
        assert "ACCESS_REFUSED" in result.error_message

    def test_sidecar_exception_becomes_an_error_result(self, monkeypatch) -> None:
        def boom(network, uri, duration_s):
            raise RuntimeError("docker gone")

        monkeypatch.setattr(
            "src.validator.benchmarks.rabbitmq_perf._run_sidecar", boom
        )
        result = run_rabbitmq_perf_test(self._runner())
        assert isinstance(result, BenchmarkResult)
        assert "docker gone" in result.error_message

    def test_unsupported_architecture_skips(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "src.validator.benchmarks.rabbitmq_perf.platform.machine",
            lambda: "s390x",
        )
        result = run_rabbitmq_perf_test(self._runner())
        assert "skipped" in result.error_message

    @pytest.mark.parametrize("arch", ["arm64", "aarch64", "x86_64", "amd64"])
    def test_arm64_is_not_skipped(self, monkeypatch, arch: str) -> None:
        """Unlike wrk, perf-test publishes a native arm64 image, so Apple
        Silicon runs a real benchmark rather than a skip."""
        monkeypatch.setattr(
            "src.validator.benchmarks.rabbitmq_perf.platform.machine", lambda: arch
        )
        monkeypatch.setattr(
            "src.validator.benchmarks.rabbitmq_perf._run_sidecar",
            lambda network, uri, duration_s: (PERF_OUTPUT, 0),
        )
        result = run_rabbitmq_perf_test(self._runner())
        assert result.error_message is None
        assert result.throughput == pytest.approx(48239.0)
