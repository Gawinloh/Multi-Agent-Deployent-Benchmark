"""Unit tests for the CIS checkers, using a mocked StackRunner with
canned exec_in responses. One integration test runs the real checkers
against a deployed stack (requires Docker)."""

from __future__ import annotations

import uuid

import pytest

from src.schemas.validator_report import CISCheckResult
from src.validator.cis_checks.nginx import NginxCISChecker
from src.validator.cis_checks.postgres import PostgresCISChecker
from src.validator.cis_checks.redis import RedisCISChecker
from src.validator.docker_runner import CommandResult


class FakeRunner:
    """Stands in for StackRunner. Responses are (substring, output) pairs
    matched against the joined command — FIRST MATCH WINS, so put more
    specific patterns (e.g. 'SHOW ssl_min_protocol_version') before less
    specific ones ('SHOW ssl')."""

    def __init__(self, responses: list[tuple[str, str | CommandResult]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def exec_in(self, service: str, command: list[str]) -> CommandResult:
        joined = " ".join(command)
        self.calls.append((service, joined))
        for pattern, response in self._responses:
            if pattern in joined:
                if isinstance(response, CommandResult):
                    return response
                return CommandResult(stdout=response, stderr="", exit_code=0, duration_s=0.01)
        return CommandResult(
            stdout="", stderr=f"no canned response for: {joined}",
            exit_code=1, duration_s=0.01,
        )


def by_control(results: list[CISCheckResult]) -> dict[str, CISCheckResult]:
    return {r.control_id: r for r in results}


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

GOOD_HBA = """\
# TYPE  DATABASE  USER  ADDRESS  METHOD
local   all       all   peer
hostssl all       all   10.0.0.0/8   scram-sha-256
"""

BAD_HBA = """\
local   all       all   trust
host    all       all   0.0.0.0/0    md5
"""


def good_postgres_runner() -> FakeRunner:
    return FakeRunner(
        [
            # specific before general: 'SHOW ssl' is a prefix of this one
            ("SHOW ssl_min_protocol_version", "TLSv1.2"),
            ("SHOW ssl", "on"),
            ("SHOW password_encryption", "scram-sha-256"),
            ("SHOW log_destination", "stderr"),
            ("SHOW log_statement", "ddl"),
            ("SHOW log_connections", "on"),
            ("SHOW log_disconnections", "on"),
            ("SHOW listen_addresses", "10.0.0.5"),
            ("SHOW log_min_duration_statement", "1000"),
            ("SHOW statement_timeout", "30s"),
            ("pg_extension", "plpgsql\npgaudit"),
            ("pg_database", ""),
            ("rolsuper", ""),
            ("cat /etc/postgresql/pg_hba.conf", GOOD_HBA),
        ]
    )


class TestPostgresCISChecker:
    def test_known_good_passes_all(self) -> None:
        results = PostgresCISChecker(good_postgres_runner()).run_all()
        failed = [r for r in results if not r.passed]
        assert failed == [], [f"{r.control_id}: {r.evidence}" for r in failed]
        assert len(results) >= 12

    def test_md5_hba_fails_auth_check(self) -> None:
        runner = good_postgres_runner()
        runner._responses[13] = ("cat /etc/postgresql/pg_hba.conf", BAD_HBA)
        results = by_control(PostgresCISChecker(runner).run_all())
        assert not results["1.1"].passed
        assert "md5" in results["1.1"].evidence

    def test_ssl_off_fails(self) -> None:
        runner = good_postgres_runner()
        runner._responses[1] = ("SHOW ssl", "off")
        results = by_control(PostgresCISChecker(runner).run_all())
        assert not results["3.2"].passed
        assert results["3.3"].passed  # min protocol check unaffected

    def test_wildcard_listen_addresses_fails(self) -> None:
        runner = good_postgres_runner()
        runner._responses[7] = ("SHOW listen_addresses", "*")
        results = by_control(PostgresCISChecker(runner).run_all())
        assert not results["3.1"].passed

    def test_log_statement_none_fails(self) -> None:
        runner = good_postgres_runner()
        runner._responses[4] = ("SHOW log_statement", "none")
        results = by_control(PostgresCISChecker(runner).run_all())
        assert not results["2.2"].passed

    def test_missing_pgaudit_fails(self) -> None:
        runner = good_postgres_runner()
        runner._responses[10] = ("pg_extension", "plpgsql")
        results = by_control(PostgresCISChecker(runner).run_all())
        assert not results["2.5"].passed

    def test_psql_failure_is_failed_check_not_crash(self) -> None:
        runner = FakeRunner([])  # everything errors
        results = PostgresCISChecker(runner).run_all()
        assert all(not r.passed for r in results)
        assert len(results) >= 12


# ---------------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------------

GOOD_NGINX_CONF = """\
worker_processes auto;
events { worker_connections 1024; }
http {
    server_tokens off;
    autoindex off;
    client_max_body_size 10m;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    access_log /var/log/nginx/access.log;
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header Strict-Transport-Security "max-age=31536000";
    server {
        listen 80;
        return 301 https://$host$request_uri;
    }
}
"""

BAD_NGINX_CONF = """\
worker_processes auto;
events { worker_connections 1024; }
http {
    autoindex on;
    ssl_protocols TLSv1 TLSv1.1 TLSv1.2;
    access_log off;
    server { listen 80; }
}
"""

GOOD_CURL = "HTTP/1.1 200 OK\r\nServer: nginx\r\nContent-Type: text/html\r\n"
BAD_CURL = "HTTP/1.1 200 OK\r\nServer: nginx/1.27.0\r\nContent-Type: text/html\r\n"


class TestNginxCISChecker:
    def _runner(self, conf: str, curl: str) -> FakeRunner:
        return FakeRunner([("cat /etc/nginx/nginx.conf", conf), ("curl", curl)])

    def test_known_good_passes_all(self) -> None:
        results = NginxCISChecker(self._runner(GOOD_NGINX_CONF, GOOD_CURL)).run_all()
        failed = [r for r in results if not r.passed]
        assert failed == [], [f"{r.control_id}: {r.evidence}" for r in failed]
        assert len(results) >= 8

    def test_known_bad_fails_specific_checks(self) -> None:
        results = by_control(
            NginxCISChecker(self._runner(BAD_NGINX_CONF, BAD_CURL)).run_all()
        )
        assert not results["2.5.1"].passed  # version disclosed in Server header
        assert not results["2.5.2"].passed  # autoindex on
        assert not results["4.1.1"].passed  # TLSv1/1.1 enabled
        assert not results["4.1.2"].passed  # prefer_server_ciphers missing
        assert not results["5.3.1"].passed  # X-Frame-Options missing
        assert not results["4.1.3"].passed  # no 301 redirect
        assert not results["3.1"].passed  # access_log off

    def test_commented_directives_ignored(self) -> None:
        conf = GOOD_NGINX_CONF.replace("autoindex off;", "autoindex off; # autoindex on")
        results = by_control(NginxCISChecker(self._runner(conf, GOOD_CURL)).run_all())
        assert results["2.5.2"].passed

    def test_header_satisfied_by_runtime_probe(self) -> None:
        conf = GOOD_NGINX_CONF.replace("add_header X-Frame-Options DENY;", "")
        curl = GOOD_CURL + "X-Frame-Options: DENY\r\n"
        results = by_control(NginxCISChecker(self._runner(conf, curl)).run_all())
        assert results["5.3.1"].passed


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

GOOD_REDIS_CONF = """\
bind 10.0.0.5
port 6379
protected-mode yes
requirepass long-secret-password
rename-command FLUSHALL ""
rename-command FLUSHDB ""
rename-command CONFIG "CONFIG_x9f2"
rename-command EVAL ""
rename-command DEBUG ""
rename-command SHUTDOWN ""
save 3600 1
appendonly yes
appendfsync everysec
maxmemory 2gb
maxmemory-policy allkeys-lru
"""

BAD_REDIS_CONF = """\
bind 0.0.0.0
port 6379
protected-mode no
appendonly no
maxmemory 0
maxmemory-policy noeviction
"""


class TestRedisCISChecker:
    def _runner(self, conf: str, ping: str) -> FakeRunner:
        return FakeRunner(
            [
                ("cat /usr/local/etc/redis/redis.conf", conf),
                ("redis-cli ping", ping),
            ]
        )

    def test_known_good_passes_all(self) -> None:
        runner = self._runner(GOOD_REDIS_CONF, "NOAUTH Authentication required.")
        results = RedisCISChecker(runner).run_all()
        failed = [r for r in results if not r.passed]
        assert failed == [], [f"{r.control_id}: {r.evidence}" for r in failed]
        assert len(results) == 8

    def test_known_bad_fails_specific_checks(self) -> None:
        results = by_control(
            RedisCISChecker(self._runner(BAD_REDIS_CONF, "PONG")).run_all()
        )
        assert not results["1.1"].passed  # protected-mode no
        assert not results["1.2"].passed  # bound to 0.0.0.0
        assert not results["2.1"].passed  # no auth configured
        assert not results["2.2"].passed  # unauthenticated ping answered PONG
        assert not results["3.1"].passed  # dangerous commands not renamed
        assert not results["4.1"].passed  # no AOF, no save schedule
        assert not results["4.2"].passed  # maxmemory 0
        assert not results["4.3"].passed  # noeviction

    def test_partial_rename_lists_missing_commands(self) -> None:
        conf = GOOD_REDIS_CONF.replace('rename-command EVAL ""\n', "")
        runner = self._runner(conf, "NOAUTH Authentication required.")
        results = by_control(RedisCISChecker(runner).run_all())
        assert not results["3.1"].passed
        assert "EVAL" in results["3.1"].evidence

    def test_acl_users_satisfy_auth_check(self) -> None:
        conf = BAD_REDIS_CONF + "\nuser appuser on >password ~* +@all\n"
        runner = self._runner(conf, "NOAUTH Authentication required.")
        results = by_control(RedisCISChecker(runner).run_all())
        assert results["2.1"].passed


# ---------------------------------------------------------------------------
# Integration — real stack (requires Docker)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIntegration:
    def test_checkers_run_against_deployed_stack(self) -> None:
        from src.validator.docker_runner import StackRunner
        from tests.test_validator.test_docker_runner import make_spec

        with StackRunner(run_id=uuid.uuid4().hex[:12]) as runner:
            runner.up(make_spec())
            for checker_cls in (PostgresCISChecker, NginxCISChecker, RedisCISChecker):
                results = checker_cls(runner).run_all()
                assert results, checker_cls.__name__
                assert all(isinstance(r, CISCheckResult) for r in results)
                # the harness test spec is deliberately imperfect; just
                # assert determinism of shape, not a pass rate
                assert all(r.evidence for r in results)
