"""Tests for the RabbitMQConfig schema and its rabbitmq.conf renderer.

The rendered output is fed to a real broker in the integration tests;
these cover the shape of it without needing Docker.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.rabbitmq import (
    RabbitMQConfig,
    RabbitMQManagementParams,
    RabbitMQNetworkingParams,
    RabbitMQResourceParams,
    RabbitMQSecurityParams,
)
from tests.test_validator.test_docker_runner import make_rabbitmq_config


def _lines(config: RabbitMQConfig) -> list[str]:
    return [
        line.strip()
        for line in config.render_conf().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestRenderConf:
    def test_sysctl_format_not_erlang_terms(self) -> None:
        conf = make_rabbitmq_config().render_conf()
        # The classic format is a list of Erlang tuples; the modern one is
        # key = value. Getting this wrong is silently accepted by the image
        # and then ignored.
        assert "[{rabbit," not in conf
        for line in _lines(make_rabbitmq_config()):
            assert " = " in line, line

    def test_listener_and_heartbeat(self) -> None:
        config = make_rabbitmq_config()
        rendered = _lines(config)
        assert "listeners.tcp.default = 0.0.0.0:5672" in rendered
        assert "heartbeat = 60" in rendered

    def test_uses_max_connections_not_the_renamed_key(self) -> None:
        """RabbitMQ 4 accepts connection_max but logs a rename warning on
        every boot, which pollutes the container logs the agent reads."""
        rendered = _lines(make_rabbitmq_config())
        assert "max_connections = 500" in rendered
        assert not any(line.startswith("connection_max") for line in rendered)

    def test_omits_max_connections_when_unset(self) -> None:
        config = make_rabbitmq_config().model_copy(
            update={"networking": RabbitMQNetworkingParams(max_connections=None)}
        )
        assert not any(
            line.startswith("max_connections") for line in _lines(config)
        )

    def test_resource_alarms(self) -> None:
        rendered = _lines(make_rabbitmq_config())
        assert "vm_memory_high_watermark.relative = 0.4" in rendered
        assert "vm_memory_high_watermark_paging_ratio = 0.5" in rendered
        assert "disk_free_limit.absolute = 200MB" in rendered

    def test_default_account(self) -> None:
        rendered = _lines(make_rabbitmq_config())
        assert "default_user = appuser" in rendered
        assert "default_pass = harness-rabbit-pass" in rendered

    def test_loopback_users_emitted_per_user(self) -> None:
        config = make_rabbitmq_config().model_copy(
            update={
                "security": RabbitMQSecurityParams(
                    default_user="appuser",
                    default_pass="harness-rabbit-pass",
                    loopback_users=["guest", "admin"],
                )
            }
        )
        rendered = _lines(config)
        assert "loopback_users.guest = true" in rendered
        assert "loopback_users.admin = true" in rendered

    def test_empty_loopback_users_emits_none_explicitly(self) -> None:
        """Silence would leave RabbitMQ's built-in ["guest"] in force, so an
        empty list has to be rendered as `loopback_users = none`."""
        config = make_rabbitmq_config().model_copy(
            update={
                "security": RabbitMQSecurityParams(
                    default_user="appuser",
                    default_pass="harness-rabbit-pass",
                    loopback_users=[],
                )
            }
        )
        assert "loopback_users = none" in _lines(config)


class TestTLSRendering:
    def test_tls_off_emits_no_ssl_listener(self) -> None:
        rendered = _lines(make_rabbitmq_config(tls=False))
        assert not any(line.startswith("listeners.ssl") for line in rendered)
        assert not any(line.startswith("ssl_options") for line in rendered)

    def test_tls_on_emits_listener_and_material(self) -> None:
        rendered = _lines(make_rabbitmq_config(tls=True))
        assert "listeners.ssl.default = 5671" in rendered
        assert "ssl_options.certfile = /etc/rabbitmq/certs/server.crt" in rendered
        assert "ssl_options.keyfile = /etc/rabbitmq/certs/server.key" in rendered
        assert "ssl_options.cacertfile = /etc/rabbitmq/certs/ca.crt" in rendered

    def test_verify_none_by_default(self) -> None:
        rendered = _lines(make_rabbitmq_config(tls=True))
        assert "ssl_options.verify = verify_none" in rendered
        assert "ssl_options.fail_if_no_peer_cert = false" in rendered

    def test_verify_peer_sets_both_settings_together(self) -> None:
        """verify_peer without fail_if_no_peer_cert still lets a client
        presenting nothing connect, so they move as a pair."""
        config = make_rabbitmq_config(tls=True).model_copy(
            update={
                "security": RabbitMQSecurityParams(
                    default_user="appuser",
                    default_pass="harness-rabbit-pass",
                    tls_enabled=True,
                    tls_verify_peer=True,
                )
            }
        )
        rendered = _lines(config)
        assert "ssl_options.verify = verify_peer" in rendered
        assert "ssl_options.fail_if_no_peer_cert = true" in rendered


class TestManagementRendering:
    def test_listener_ip_and_port(self) -> None:
        rendered = _lines(make_rabbitmq_config())
        assert "management.tcp.ip = 127.0.0.1" in rendered
        assert "management.tcp.port = 15672" in rendered

    def test_disabled_emits_nothing(self) -> None:
        config = make_rabbitmq_config().model_copy(
            update={"management": RabbitMQManagementParams(enabled=False)}
        )
        assert not any(
            line.startswith("management.") for line in _lines(config)
        )


class TestValidation:
    def test_watermark_must_be_a_fraction(self) -> None:
        for bad in (0, 1.5, -0.1):
            with pytest.raises(ValidationError):
                RabbitMQResourceParams(
                    vm_memory_high_watermark=bad, disk_free_limit="1GB"
                )

    def test_watermark_of_one_is_allowed_by_the_schema(self) -> None:
        """1.0 is representable — it is control 4.1 that fails it. The
        schema must not pre-empt a control the agent is scored on."""
        params = RabbitMQResourceParams(
            vm_memory_high_watermark=1.0, disk_free_limit="1GB"
        )
        assert params.vm_memory_high_watermark == 1.0

    def test_disk_free_limit_needs_a_size(self) -> None:
        with pytest.raises(ValidationError):
            RabbitMQResourceParams(
                vm_memory_high_watermark=0.4, disk_free_limit="lots"
            )

    def test_short_password_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RabbitMQSecurityParams(default_user="appuser", default_pass="short")

    def test_default_user_may_not_contain_whitespace(self) -> None:
        with pytest.raises(ValidationError):
            RabbitMQSecurityParams(
                default_user="app user", default_pass="a-long-password"
            )

    def test_guest_is_representable(self) -> None:
        """The insecure choice must be expressible, or controls 2.1-2.4
        could never fail and would be measuring nothing."""
        params = RabbitMQSecurityParams(default_user="guest", default_pass="guest123")
        assert params.default_user == "guest"

    def test_colliding_listener_ports_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must all differ"):
            RabbitMQConfig(
                resources=RabbitMQResourceParams(
                    vm_memory_high_watermark=0.4, disk_free_limit="1GB"
                ),
                networking=RabbitMQNetworkingParams(listener_port=5672),
                security=RabbitMQSecurityParams(
                    default_user="appuser",
                    default_pass="a-long-password",
                    tls_enabled=True,
                    tls_port=5672,
                ),
                management=RabbitMQManagementParams(),
            )

    def test_tls_port_may_equal_listener_port_when_tls_is_off(self) -> None:
        """An unused tls_port is not a conflict; only a bound one is."""
        config = RabbitMQConfig(
            resources=RabbitMQResourceParams(
                vm_memory_high_watermark=0.4, disk_free_limit="1GB"
            ),
            networking=RabbitMQNetworkingParams(listener_port=5671),
            security=RabbitMQSecurityParams(
                default_user="appuser",
                default_pass="a-long-password",
                tls_enabled=False,
                tls_port=5671,
            ),
            management=RabbitMQManagementParams(),
        )
        assert config.networking.listener_port == 5671
