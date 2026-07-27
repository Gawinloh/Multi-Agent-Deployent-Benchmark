"""Tests for run provenance and the configuration correctness scorer.

No LLM, no Docker, no git dependency — subprocess is patched where git
provenance is exercised so the suite behaves identically inside and
outside a repository.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.experiment.result_logger import (
    GROUND_TRUTH_PARAMETER_MAP,
    RunResult,
    capture_git_provenance,
    compute_per_agent_tokens,
    compute_scores,
    parse_size,
    score_configuration_correctness,
)

KB = 1024
MB = 1024**2
GB = 1024**3

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def final_spec() -> dict[str, Any]:
    """A generated spec that satisfies every assertion in SMOKE_GROUND_TRUTH."""
    return {
        "requirements": {
            "workload_class": "BALANCED",
            "expected_concurrent_users": 5,
            "expected_data_size_gb": 5.0,
            "hardware": {"ram_gb": 8.0, "vcpu": 4, "disk_gb": 100.0},
            "compliance": "NONE",
            "backup_required": False,
        },
        "postgres": {
            "memory": {
                "shared_buffers": "2GB",
                "effective_cache_size": "6GB",
                "work_mem": "16MB",
                "maintenance_work_mem": "256MB",
            },
            "connections": {
                "max_connections": 30,
                "superuser_reserved_connections": 3,
            },
            "wal": {
                "wal_level": "replica",
                "checkpoint_completion_target": 0.9,
                "max_wal_size": "2GB",
            },
            "security": {
                "ssl": True,
                "password_encryption": "scram-sha-256",
                "log_connections": True,
                "log_disconnections": True,
                "ssl_min_protocol_version": "TLSv1.2",
            },
            "logging": {
                "log_destination": "stderr",
                "log_statement": "ddl",
                "log_min_duration_statement": 250,
            },
            "listen_addresses": "*",
        },
        "nginx": {
            "worker": {"worker_processes": "auto", "worker_connections": 1024},
            "http": {
                "sendfile": True,
                "tcp_nopush": True,
                "tcp_nodelay": True,
                "keepalive_timeout": 65,
                "keepalive_requests": 1000,
                "gzip": True,
            },
            "security": {
                "server_tokens": False,
                "autoindex": False,
                "client_max_body_size": "10m",
            },
            "ssl": {
                "protocols": ["TLSv1.2", "TLSv1.3"],
                "ciphers": "ECDHE-ECDSA-AES128-GCM-SHA256",
                "prefer_server_ciphers": False,
                "session_cache": "shared:SSL:10m",
                "session_timeout": "1d",
                "stapling": True,
            },
        },
        "redis": {
            "memory": {
                "maxmemory": "512MB",
                "maxmemory_policy": "allkeys-lru",
                "maxmemory_samples": 5,
            },
            "persistence": {
                "save": ["3600 1"],
                "appendonly": True,
                "appendfsync": "everysec",
            },
            "security": {
                "protected_mode": True,
                "requirepass": "a-long-enough-password",
                "rename_commands": {},
            },
            "networking": {"bind": ["127.0.0.1"], "port": 6379, "tls_port": None},
        },
        "pg_hba": {
            "rules": [
                {
                    "type": "host",
                    "database": "all",
                    "user": "all",
                    "address": "172.16.0.0/12",
                    "auth_method": "scram-sha-256",
                }
            ]
        },
    }


@pytest.fixture
def smoke_ground_truth() -> dict[str, Any]:
    """The ground-truth block from benchmark/scenarios/smoke_test.yaml."""
    return {
        "workload_class": "BALANCED",
        "compliance": "NONE",
        "expected_postgres": {
            "shared_buffers_range": ["1GB", "2GB"],
            "max_connections_range": [20, 50],
            "ssl": True,
            "password_encryption": "scram-sha-256",
        },
        "expected_nginx": {
            "server_tokens": False,
            "ssl_protocols_min": "TLSv1.2",
        },
        "expected_redis": {
            "maxmemory_set": True,
            "maxmemory_policy_acceptable": [
                "allkeys-lru",
                "allkeys-lfu",
                "noeviction",
            ],
            "requirepass_or_acl": True,
        },
        "expected_cis_minimum_pass_rate": 0.75,
    }


def _detail(scored: dict[str, Any], parameter: str) -> dict[str, Any]:
    """Pull one entry out of parameter_details by name."""
    matches = [d for d in scored["parameter_details"] if d["parameter"] == parameter]
    assert matches, f"{parameter} not present in parameter_details"
    return matches[0]


# ---------------------------------------------------------------------------
# parse_size
# ---------------------------------------------------------------------------


class TestParseSize:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2GB", 2 * GB),
            ("2048MB", 2 * GB),
            ("1GB", GB),
            ("512MB", 512 * MB),
            ("64kB", 64 * KB),
            ("64KB", 64 * KB),
            ("64kb", 64 * KB),
            ("10m", 10 * MB),  # nginx client_max_body_size style
            ("1TB", 1024 * GB),
            ("4096", 4096),  # bare number means bytes
            ("512B", 512),
        ],
    )
    def test_units(self, text: str, expected: int) -> None:
        assert parse_size(text) == expected

    def test_decimals(self) -> None:
        # Schemas permit "1.2GB" — must not be truncated to 1GB.
        assert parse_size("1.2GB") == round(1.2 * GB)
        assert parse_size("1.5GB") == parse_size("1536MB")

    @pytest.mark.parametrize("text", ["1.5 GB", " 1.5GB ", "1.5  GB", "  2GB"])
    def test_optional_whitespace(self, text: str) -> None:
        assert parse_size(text) == parse_size("1536MB") or parse_size(text) == 2 * GB

    def test_unit_equivalence_is_exact(self) -> None:
        assert parse_size("2GB") == parse_size("2048MB") == parse_size("2097152kB")

    def test_numeric_input_is_bytes(self) -> None:
        assert parse_size(2048) == 2048
        assert parse_size(2048.0) == 2048

    @pytest.mark.parametrize(
        "bad", ["", "   ", "abc", "GB", "5XB", "2 giga", "-1GB", "1,5GB", "2GB extra"]
    )
    def test_invalid_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_size(bad)

    def test_negative_number_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_size(-1)

    def test_boolean_raises(self) -> None:
        # bool is a subclass of int; silently scoring True as 1 byte would
        # make a presence flag look like a size.
        with pytest.raises(ValueError):
            parse_size(True)

    def test_none_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_size(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Correctness scoring — comparison types
# ---------------------------------------------------------------------------


class TestRangeComparison:
    def test_size_range_passes(
        self, final_spec: dict[str, Any]
    ) -> None:
        gt = {"expected_postgres": {"shared_buffers_range": ["1GB", "2GB"]}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None
        assert scored["correctness"] == 1.0
        assert _detail(scored, "postgres.shared_buffers_range")["comparison"] == "range"

    def test_size_range_fails_when_above_upper_bound(
        self, final_spec: dict[str, Any]
    ) -> None:
        final_spec["postgres"]["memory"]["shared_buffers"] = "4GB"
        gt = {"expected_postgres": {"shared_buffers_range": ["1GB", "2GB"]}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None
        assert scored["correctness"] == 0.0
        assert _detail(scored, "postgres.shared_buffers_range")["passed"] is False

    def test_size_range_bounds_are_inclusive(
        self, final_spec: dict[str, Any]
    ) -> None:
        final_spec["postgres"]["memory"]["shared_buffers"] = "1GB"
        gt = {"expected_postgres": {"shared_buffers_range": ["1GB", "2GB"]}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 1.0

    def test_numeric_range_passes(self, final_spec: dict[str, Any]) -> None:
        gt = {"expected_postgres": {"max_connections_range": [20, 50]}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 1.0

    def test_numeric_range_fails_below_lower_bound(
        self, final_spec: dict[str, Any]
    ) -> None:
        final_spec["postgres"]["connections"]["max_connections"] = 10
        gt = {"expected_postgres": {"max_connections_range": [20, 50]}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 0.0

    def test_size_range_compares_in_bytes_not_strings(
        self, final_spec: dict[str, Any]
    ) -> None:
        # "2048MB" sorts below "1GB" as a string but is in range as a size.
        final_spec["postgres"]["memory"]["shared_buffers"] = "2048MB"
        gt = {"expected_postgres": {"shared_buffers_range": ["1GB", "2GB"]}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 1.0


class TestMembershipComparison:
    def test_membership_passes(self, final_spec: dict[str, Any]) -> None:
        gt = {
            "expected_redis": {
                "maxmemory_policy_acceptable": ["allkeys-lru", "noeviction"]
            }
        }
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None
        assert scored["correctness"] == 1.0
        detail = _detail(scored, "redis.maxmemory_policy_acceptable")
        assert detail["comparison"] == "membership"

    def test_membership_fails(self, final_spec: dict[str, Any]) -> None:
        final_spec["redis"]["memory"]["maxmemory_policy"] = "volatile-ttl"
        gt = {
            "expected_redis": {
                "maxmemory_policy_acceptable": ["allkeys-lru", "noeviction"]
            }
        }
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 0.0


class TestEqualityComparison:
    def test_boolean_equality_passes(self, final_spec: dict[str, Any]) -> None:
        gt = {"expected_postgres": {"ssl": True}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 1.0

    def test_boolean_equality_fails(self, final_spec: dict[str, Any]) -> None:
        final_spec["postgres"]["security"]["ssl"] = False
        gt = {"expected_postgres": {"ssl": True}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 0.0

    def test_string_equality_passes(self, final_spec: dict[str, Any]) -> None:
        gt = {"expected_postgres": {"password_encryption": "scram-sha-256"}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 1.0

    def test_string_equality_fails(self, final_spec: dict[str, Any]) -> None:
        final_spec["postgres"]["security"]["password_encryption"] = "md5"
        gt = {"expected_postgres": {"password_encryption": "scram-sha-256"}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None
        detail = _detail(scored, "postgres.password_encryption")
        assert detail["passed"] is False
        assert detail["actual"] == "md5"
        assert detail["expected"] == "scram-sha-256"

    def test_size_equality_uses_bytes(self, final_spec: dict[str, Any]) -> None:
        final_spec["redis"]["memory"]["maxmemory"] = "2048MB"
        gt = {"expected_redis": {"maxmemory": "2GB"}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None, "2048MB and 2GB must compare equal"
        assert scored["correctness"] == 1.0

    def test_size_equality_fails_on_different_quantity(
        self, final_spec: dict[str, Any]
    ) -> None:
        final_spec["redis"]["memory"]["maxmemory"] = "1024MB"
        gt = {"expected_redis": {"maxmemory": "2GB"}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 0.0


class TestAdaptedParameters:
    def test_lowest_tls_protocol_passes(self, final_spec: dict[str, Any]) -> None:
        gt = {"expected_nginx": {"ssl_protocols_min": "TLSv1.2"}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 1.0
        detail = _detail(scored, "nginx.ssl_protocols_min")
        assert detail["actual"] == "TLSv1.2"
        assert detail["actual_raw"] == ["TLSv1.2", "TLSv1.3"]

    def test_lowest_tls_protocol_fails_when_weak_protocol_offered(
        self, final_spec: dict[str, Any]
    ) -> None:
        final_spec["nginx"]["ssl"]["protocols"] = ["TLSv1.1", "TLSv1.2"]
        gt = {"expected_nginx": {"ssl_protocols_min": "TLSv1.2"}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 0.0

    def test_presence_flag_passes(self, final_spec: dict[str, Any]) -> None:
        gt = {"expected_redis": {"maxmemory_set": True, "requirepass_or_acl": True}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 1.0

    def test_presence_flag_fails_when_maxmemory_is_zero(
        self, final_spec: dict[str, Any]
    ) -> None:
        # Redis treats maxmemory 0 as "no limit", so it is not "set".
        final_spec["redis"]["memory"]["maxmemory"] = "0"
        gt = {"expected_redis": {"maxmemory_set": True}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 0.0

    def test_presence_flag_fails_when_requirepass_absent(
        self, final_spec: dict[str, Any]
    ) -> None:
        final_spec["redis"]["security"]["requirepass"] = None
        gt = {"expected_redis": {"requirepass_or_acl": True}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None
        assert scored["correctness"] == 0.0
        assert _detail(scored, "redis.requirepass_or_acl")["status"] == "missing"


# ---------------------------------------------------------------------------
# Correctness scoring — semantics
# ---------------------------------------------------------------------------


class TestCorrectnessSemantics:
    def test_missing_parameter_counts_as_failure_without_crashing(
        self, final_spec: dict[str, Any]
    ) -> None:
        del final_spec["postgres"]["security"]["ssl"]
        gt = {"expected_postgres": {"ssl": True, "password_encryption": "scram-sha-256"}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None
        assert scored["parameters_checked"] == 2
        assert scored["correctness"] == pytest.approx(0.5)
        detail = _detail(scored, "postgres.ssl")
        assert detail["passed"] is False
        assert detail["status"] == "missing"
        assert detail["actual"] is None

    def test_missing_intermediate_node_counts_as_failure(
        self, final_spec: dict[str, Any]
    ) -> None:
        del final_spec["postgres"]["security"]
        gt = {"expected_postgres": {"ssl": True}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None
        assert scored["correctness"] == 0.0
        assert _detail(scored, "postgres.ssl")["status"] == "missing"

    def test_entire_service_missing_counts_as_failure(
        self, final_spec: dict[str, Any]
    ) -> None:
        del final_spec["redis"]
        gt = {"expected_redis": {"maxmemory_set": True}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None and scored["correctness"] == 0.0

    def test_final_spec_none_yields_no_score(
        self, smoke_ground_truth: dict[str, Any]
    ) -> None:
        assert score_configuration_correctness(None, smoke_ground_truth) is None

    def test_final_spec_none_omits_correctness_key_from_scores(
        self, smoke_ground_truth: dict[str, Any]
    ) -> None:
        result = RunResult(
            scenario_id="s",
            scenario_text="t",
            architecture="single",
            model="m",
            termination_reason="max_iterations",
            final_spec=None,
        )
        scores = compute_scores(result, smoke_ground_truth)
        # Absent and wrong are different: a never-finalised run must not
        # contribute a 0.0 to the correctness mean.
        assert "correctness" not in scores
        assert "parameters_checked" not in scores
        assert "parameter_details" not in scores
        assert scores["completed"] is False

    def test_no_ground_truth_yields_no_score(
        self, final_spec: dict[str, Any]
    ) -> None:
        assert score_configuration_correctness(final_spec, None) is None
        assert score_configuration_correctness(final_spec, {}) is None

    def test_ground_truth_without_expected_blocks_yields_no_score(
        self, final_spec: dict[str, Any]
    ) -> None:
        gt = {"workload_class": "BALANCED", "expected_cis_minimum_pass_rate": 0.75}
        assert score_configuration_correctness(final_spec, gt) is None

    def test_unmapped_key_is_recorded_but_excluded_from_denominator(
        self, final_spec: dict[str, Any]
    ) -> None:
        gt = {"expected_postgres": {"ssl": True, "invented_parameter": 42}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None
        assert scored["parameters_checked"] == 1
        assert scored["correctness"] == 1.0
        unmapped = _detail(scored, "postgres.invented_parameter")
        assert unmapped["status"] == "unmapped"
        assert unmapped["passed"] is None

    def test_uncomparable_value_fails_without_crashing(
        self, final_spec: dict[str, Any]
    ) -> None:
        final_spec["postgres"]["memory"]["shared_buffers"] = "not-a-size"
        gt = {"expected_postgres": {"shared_buffers_range": ["1GB", "2GB"]}}
        scored = score_configuration_correctness(final_spec, gt)
        assert scored is not None
        assert scored["correctness"] == 0.0
        assert _detail(scored, "postgres.shared_buffers_range")["status"] == "uncomparable"

    def test_parameters_checked_varies_by_scenario(
        self, final_spec: dict[str, Any]
    ) -> None:
        small = {"expected_postgres": {"ssl": True}}
        larger = {
            "expected_postgres": {"ssl": True, "password_encryption": "scram-sha-256"},
            "expected_nginx": {"server_tokens": False},
        }
        assert score_configuration_correctness(final_spec, small)["parameters_checked"] == 1
        assert score_configuration_correctness(final_spec, larger)["parameters_checked"] == 3

    def test_full_smoke_scenario_all_pass(
        self, final_spec: dict[str, Any], smoke_ground_truth: dict[str, Any]
    ) -> None:
        scored = score_configuration_correctness(final_spec, smoke_ground_truth)
        assert scored is not None
        # 4 postgres + 2 nginx + 3 redis; the top-level workload_class,
        # compliance and CIS keys are outside the expected_* blocks.
        assert scored["parameters_checked"] == 9
        assert scored["correctness"] == 1.0
        assert all(d["passed"] for d in scored["parameter_details"])

    def test_full_smoke_scenario_partial_failure(
        self, final_spec: dict[str, Any], smoke_ground_truth: dict[str, Any]
    ) -> None:
        final_spec["postgres"]["security"]["ssl"] = False
        final_spec["nginx"]["security"]["server_tokens"] = True
        scored = score_configuration_correctness(final_spec, smoke_ground_truth)
        assert scored is not None
        assert scored["correctness"] == pytest.approx(7 / 9)

    def test_details_carry_spec_path_for_every_checked_parameter(
        self, final_spec: dict[str, Any], smoke_ground_truth: dict[str, Any]
    ) -> None:
        scored = score_configuration_correctness(final_spec, smoke_ground_truth)
        assert scored is not None
        for detail in scored["parameter_details"]:
            assert detail["spec_path"], detail
            assert "expected" in detail and "actual" in detail

    def test_scorer_does_not_mutate_inputs(
        self, final_spec: dict[str, Any], smoke_ground_truth: dict[str, Any]
    ) -> None:
        spec_before = copy.deepcopy(final_spec)
        gt_before = copy.deepcopy(smoke_ground_truth)
        score_configuration_correctness(final_spec, smoke_ground_truth)
        assert final_spec == spec_before
        assert smoke_ground_truth == gt_before

    def test_compute_scores_includes_correctness(
        self, final_spec: dict[str, Any], smoke_ground_truth: dict[str, Any]
    ) -> None:
        result = RunResult(
            scenario_id="s",
            scenario_text="t",
            architecture="single",
            model="m",
            termination_reason="finalised",
            final_spec=final_spec,
        )
        scores = compute_scores(result, smoke_ground_truth)
        assert scores["correctness"] == 1.0
        assert scores["parameters_checked"] == 9
        assert len(scores["parameter_details"]) == 9


class TestParameterMap:
    def test_every_smoke_test_key_is_mapped(
        self, smoke_ground_truth: dict[str, Any]
    ) -> None:
        # The shipped scenario must not silently lose parameters.
        for block in GROUND_TRUTH_PARAMETER_MAP:
            for key in smoke_ground_truth.get(block, {}):
                base = key.removesuffix("_acceptable").removesuffix("_range")
                assert (
                    key in GROUND_TRUTH_PARAMETER_MAP[block]
                    or base in GROUND_TRUTH_PARAMETER_MAP[block]
                ), f"{block}.{key} has no mapping entry"

    def test_paths_resolve_against_a_real_spec(
        self, final_spec: dict[str, Any]
    ) -> None:
        for block, params in GROUND_TRUTH_PARAMETER_MAP.items():
            for name, param in params.items():
                node: Any = final_spec
                for part in param.path.split("."):
                    assert isinstance(node, dict) and part in node, (
                        f"{block}.{name} maps to unreachable path {param.path}"
                    )
                    node = node[part]


# ---------------------------------------------------------------------------
# Per-agent token attribution
# ---------------------------------------------------------------------------


class TestPerAgentTokens:
    def test_parts_reconcile_with_run_total(self) -> None:
        log = [
            {"worker": "config", "tokens_used": 4000},
            {"worker": "validation", "tokens_used": 1500},
            {"worker": "security", "tokens_used": 900},
        ]
        per_agent = compute_per_agent_tokens(log, total_tokens=8000)
        assert per_agent == {
            "config": 4000,
            "validation": 1500,
            "security": 900,
            "orchestrator": 1600,
        }
        assert sum(per_agent.values()) == 8000

    def test_repeat_delegations_accumulate_per_worker(self) -> None:
        log = [
            {"worker": "config", "tokens_used": 1000},
            {"worker": "config", "tokens_used": 2000},
        ]
        per_agent = compute_per_agent_tokens(log, total_tokens=5000)
        assert per_agent["config"] == 3000
        assert per_agent["orchestrator"] == 2000
        assert sum(per_agent.values()) == 5000

    def test_orchestrator_self_entries_are_not_summed_as_workers(self) -> None:
        log = [
            {"worker": "orchestrator", "task": "parse_retry", "success": False},
            {"worker": "unknown", "task": "no target", "success": False},
            {"worker": "config", "tokens_used": 3000},
        ]
        per_agent = compute_per_agent_tokens(log, total_tokens=5000)
        assert set(per_agent) == {"config", "orchestrator"}
        assert per_agent["orchestrator"] == 2000

    def test_entries_without_token_counts_are_skipped(self) -> None:
        log = [{"worker": "config"}, {"worker": "validation", "tokens_used": 500}]
        per_agent = compute_per_agent_tokens(log, total_tokens=2000)
        assert "config" not in per_agent
        assert per_agent["validation"] == 500
        assert sum(per_agent.values()) == 2000

    def test_empty_log_attributes_everything_to_orchestrator(self) -> None:
        assert compute_per_agent_tokens([], total_tokens=1200) == {
            "orchestrator": 1200
        }

    def test_negative_orchestrator_share_warns_but_still_reconciles(self) -> None:
        log = [{"worker": "config", "tokens_used": 9000}]
        per_agent = compute_per_agent_tokens(log, total_tokens=5000)
        assert per_agent["orchestrator"] == -4000
        assert sum(per_agent.values()) == 5000

    def test_single_agent_runs_leave_the_field_null(self) -> None:
        result = RunResult(
            scenario_id="s", scenario_text="t", architecture="single", model="m"
        )
        assert result.per_agent_tokens is None


# ---------------------------------------------------------------------------
# Git provenance
# ---------------------------------------------------------------------------


class TestGitProvenance:
    def test_clean_tree(self) -> None:
        with patch("src.experiment.result_logger.subprocess.run") as run:
            run.side_effect = [
                MagicMock(stdout="abc123def456\n"),
                MagicMock(stdout=""),
            ]
            commit, dirty = capture_git_provenance()
        assert commit == "abc123def456"
        assert dirty is False

    def test_dirty_tree(self) -> None:
        with patch("src.experiment.result_logger.subprocess.run") as run:
            run.side_effect = [
                MagicMock(stdout="abc123def456\n"),
                MagicMock(stdout=" M src/experiment/runner.py\n?? notes.txt\n"),
            ]
            commit, dirty = capture_git_provenance()
        assert commit == "abc123def456"
        assert dirty is True

    def test_missing_git_binary_fails_soft(self) -> None:
        with patch("src.experiment.result_logger.subprocess.run") as run:
            run.side_effect = FileNotFoundError("git not found")
            assert capture_git_provenance() == (None, None)

    def test_not_a_repository_fails_soft(self) -> None:
        with patch("src.experiment.result_logger.subprocess.run") as run:
            run.side_effect = subprocess.CalledProcessError(128, "git")
            assert capture_git_provenance() == (None, None)

    def test_timeout_fails_soft(self) -> None:
        with patch("src.experiment.result_logger.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired("git", 10)
            assert capture_git_provenance() == (None, None)

    def test_outside_a_repository_does_not_raise(self, tmp_path: Path) -> None:
        # Real subprocess, no patching: tmp_path is not a git repository.
        commit, dirty = capture_git_provenance(tmp_path)
        assert commit is None or isinstance(commit, str)
        assert dirty is None or isinstance(dirty, bool)


# ---------------------------------------------------------------------------
# Runner integration — provenance lands in the RunResult
# ---------------------------------------------------------------------------


class TestRunnerProvenance:
    def test_model_records_resolved_identifier_not_cli_arg(self) -> None:
        from src.experiment.runner import _run_single

        client = MagicMock()
        client.model_name = "qwen2.5:14b"
        agent_result = MagicMock(
            tokens_used=100,
            wall_clock_s=1.0,
            termination_reason="finalised",
            history=[],
            final_spec=None,
            validator_report=None,
        )

        with (
            patch("src.llm.client.get_client", return_value=client),
            patch("src.agents.single_agent.agent.SingleAgent") as agent_cls,
        ):
            agent_cls.return_value.run.return_value = agent_result
            result = _run_single(
                {"id": "s", "request": "r"},
                model="default",
                budget_limit=1000,
                max_iterations=5,
            )

        assert result.model == "qwen2.5:14b"

    def test_git_fields_are_populated_at_run_start(self) -> None:
        from src.experiment.runner import _run_single

        with (
            patch(
                "src.experiment.runner.capture_git_provenance",
                return_value=("deadbeef", True),
            ),
            patch("src.llm.client.get_client", side_effect=RuntimeError("no backend")),
        ):
            result = _run_single(
                {"id": "s", "request": "r"},
                model="default",
                budget_limit=1000,
                max_iterations=5,
            )

        # Provenance is captured before the agent runs, so it survives a
        # run that fails outright.
        assert result.git_commit == "deadbeef"
        assert result.git_dirty is True
        assert result.termination_reason.startswith("runner_error")
