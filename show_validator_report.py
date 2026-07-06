#!/usr/bin/env python3
"""Run the REAL validator once and print the report.

Deploys nginx + PostgreSQL + Redis in Docker with a known-good config,
runs smoke tests, benchmarks (pgbench / wrk / redis-benchmark), and the
CIS security checks against the LIVE stack, tears everything down, and
prints the structured ValidatorReport in human-readable form.

This is the real harness — the same validate_config the agent will call —
not a simulation. Requires Docker Desktop running. Takes ~30-60s.

Run:  python show_validator_report.py
"""

from __future__ import annotations

import logging
from collections import defaultdict

import structlog

# Quieten library logging so the report is the only output.
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

from src.validator.runner import validate_config  # noqa: E402
# Reuse the harness's known-good spec (SSL off, local-trust pg_hba) — the same
# one the integration tests deploy.
from tests.test_validator.test_docker_runner import make_spec  # noqa: E402

BAR = "=" * 70
_UNIT = {"postgres": "tps", "nginx": "req/s", "redis": "ops/s"}


def main() -> None:
    print(f"\n{BAR}\n  REAL VALIDATOR — deploying nginx + PostgreSQL + Redis in Docker\n{BAR}")
    print("  Bringing the stack up, benchmarking it, and CIS-scanning it live.")
    print("  (~30-60s; the stack is torn down automatically at the end.)\n")

    report = validate_config(make_spec(), budget_seconds=180)

    if report.error:
        print(f"  NOTE: {report.error}\n")

    print("  SMOKE TESTS — did each service start and accept connections?")
    for svc, r in report.smoke_tests.items():
        mark = "PASS" if r.passed else "FAIL"
        extra = "" if r.passed else f"   ({r.error_message})"
        print(f"     [{mark}]  {svc}{extra}")

    print("\n  BENCHMARKS — live throughput")
    for svc, b in report.benchmarks.items():
        if b.throughput is not None:
            print(f"     {svc:<9} {b.throughput:>12,.1f} {_UNIT.get(svc, '')}")
        else:
            print(f"     {svc:<9} (no result: {b.error_message})")

    print("\n  CIS SECURITY CHECKS — scored against the live stack")
    by_service: dict[str, list] = defaultdict(list)
    for c in report.cis_results:
        by_service[c.service].append(c)
    for svc in ("postgres", "nginx", "redis"):
        checks = by_service.get(svc, [])
        passed = sum(1 for c in checks if c.passed)
        print(f"     {svc}  —  {passed}/{len(checks)} passed")
        for c in checks:
            mark = "PASS" if c.passed else "FAIL"
            print(f"        [{mark}]  {c.control_id:<7} {c.name}")

    rate = report.cis_pass_rate()
    print(f"\n{BAR}")
    print(f"  SUMMARY: {report.summary()}")
    if rate is not None:
        print(f"  Overall CIS pass rate: {rate:.0%}")
    print(f"{BAR}\n")


if __name__ == "__main__":
    main()
