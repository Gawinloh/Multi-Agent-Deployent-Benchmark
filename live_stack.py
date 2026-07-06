#!/usr/bin/env python3
"""Bring the real stack up and KEEP it running, so you can exec into the
live containers by hand, then tear it all down on Enter.

This deploys the same nginx + PostgreSQL + Redis stack the validator uses
(generated configs, isolated network), with a fixed name so the commands
are predictable. Requires Docker Desktop running.

Usage:
    python live_stack.py          # brings it up, prints exec commands, waits
    # ...in ANOTHER terminal, run the printed `docker exec` commands...
    # back here: press Enter to tear down.
"""

from __future__ import annotations

import logging
import subprocess

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

from src.validator.docker_runner import StackRunner  # noqa: E402
from tests.test_validator.test_docker_runner import make_spec  # noqa: E402

RUN_ID = "demo"  # fixed -> predictable container names
REDIS_PW = "harness-test-pass"  # make_spec's default requirepass
BAR = "=" * 64


def _container_names(project: str) -> dict[str, str]:
    """Map service -> actual running container name."""
    ps = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=15,
    )
    names: dict[str, str] = {}
    for nm in ps.stdout.split():
        for svc in ("postgres", "redis", "nginx"):
            if svc in nm:
                names[svc] = nm
    # fall back to the compose v2 default naming if the lookup came up short
    for svc in ("postgres", "redis", "nginx"):
        names.setdefault(svc, f"{project}-{svc}-1")
    return names


def main() -> None:
    runner = StackRunner(run_id=RUN_ID)
    print("\n  Bringing the stack up (nginx + PostgreSQL + Redis)... ~10s\n")
    runner.up(make_spec())
    n = _container_names(runner.project)

    print(f"""{BAR}
  STACK IS LIVE   (compose project: {runner.project})
{BAR}
  In ANOTHER terminal, exec into the running containers:

  # See them running
  docker ps

  # PostgreSQL - interactive SQL shell
  docker exec -it {n['postgres']} psql -U postgres
      then try:   SHOW password_encryption;   SHOW ssl;   \\du   \\q

  # Redis - interactive CLI
  docker exec -it {n['redis']} redis-cli -a {REDIS_PW}
      then try:   PING    CONFIG GET maxmemory    INFO server    QUIT

  # nginx - a shell inside the container
  docker exec -it {n['nginx']} sh
      then try:   nginx -v    curl -I localhost    exit
{BAR}
""")
    try:
        input("  Press Enter here to tear the stack down...\n")
    finally:
        print("  Tearing down...")
        runner.down()
        print("  Done.\n")


if __name__ == "__main__":
    main()
