#!/usr/bin/env python3
"""Local web UI: prompt -> agent reasoning -> real Docker validation that
STAYS UP so you can exec into the containers yourself, then a teardown button.

  INPUT   : a natural-language stack request (the prompt box).
  PROCESS : the single-agent ReAct loop's think -> act -> observe steps.
  OUTPUT  : the REAL validator deploying the agent's config into Docker,
            running live commands inside the containers, CIS-scoring it, and
            LEAVING THE STACK RUNNING with ready-to-paste `docker exec`
            commands. A button tears it down when you're done.

The agent's MODEL is scripted (deterministic) so the demo is reliable;
the ReAct loop, the config rendering, and everything under Output (real
containers, live exec, CIS checks) is the real system. Requires Docker
Desktop running.

Run:  python app.py        then open  http://127.0.0.1:5001
"""

from __future__ import annotations

import copy
import logging
import subprocess
import uuid

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

from flask import Flask, jsonify, request  # noqa: E402

from src.agents.single_agent.agent import AgentStep, SingleAgent  # noqa: E402
from src.llm.client import LLMClient, TokenUsage  # noqa: E402
from src.llm.token_budget import TokenBudget  # noqa: E402
from src.schemas.agent import AgentThought, ToolCall  # noqa: E402
from src.schemas.stack import StackSpec  # noqa: E402
from src.tools.finaliser import finalise  # noqa: E402
from src.tools.registry import (  # noqa: E402
    FinaliseInput,
    GenerateConfigInput,
    QueryRagInput,
    Tool,
    ToolRegistry,
    ValidateConfigInput,
)
from src.validator.benchmarks.pgbench import run_pgbench  # noqa: E402
from src.validator.benchmarks.redis_bench import run_redis_benchmark  # noqa: E402
from src.validator.cis_checks.nginx import NginxCISChecker  # noqa: E402
from src.validator.cis_checks.postgres import PostgresCISChecker  # noqa: E402
from src.validator.cis_checks.redis import RedisCISChecker  # noqa: E402
from src.validator.docker_runner import StackRunner, StackStartupError  # noqa: E402
from src.validator.runner import _run_smoke_tests  # noqa: E402

RUN_ID = "demo"  # fixed -> predictable container names, one stack at a time
PROJECT = f"stack-{RUN_ID}"
NETWORK = f"stack_{RUN_ID}_net"

# --------------------------------------------------------------------------
# Deploy-safe StackSpec (SSL off, local-trust pg_hba). SPEC_BAD uses weak md5
# auth, which the agent's self-check catches and fixes to scram-sha-256.
# --------------------------------------------------------------------------

SPEC_GOOD: dict = {
    "requirements": {
        "workload_class": "BALANCED", "expected_concurrent_users": 5,
        "expected_data_size_gb": 1,
        "hardware": {"ram_gb": 2, "vcpu": 2, "disk_gb": 10},
        "compliance": "NONE", "backup_required": False,
    },
    "postgres": {
        "memory": {"shared_buffers": "128MB", "effective_cache_size": "512MB",
                   "work_mem": "4MB", "maintenance_work_mem": "64MB"},
        "connections": {"max_connections": 20, "superuser_reserved_connections": 3},
        "wal": {"wal_level": "replica", "checkpoint_completion_target": 0.9,
                "max_wal_size": "512MB"},
        "security": {"ssl": False, "password_encryption": "scram-sha-256",
                     "log_connections": True, "log_disconnections": True,
                     "ssl_min_protocol_version": "TLSv1.2"},
        "logging": {"log_destination": "stderr", "log_statement": "ddl",
                    "log_min_duration_statement": 1000},
    },
    "nginx": {
        "worker": {"worker_processes": "auto", "worker_connections": 512},
        "http": {"sendfile": True, "tcp_nopush": True, "tcp_nodelay": True,
                 "keepalive_timeout": 65, "keepalive_requests": 1000, "gzip": True},
        "security": {"server_tokens": False, "autoindex": False,
                     "client_max_body_size": "10m"},
        "ssl": {"protocols": ["TLSv1.2", "TLSv1.3"], "ciphers": "HIGH:!aNULL:!MD5",
                "prefer_server_ciphers": True, "session_cache": "shared:SSL:10m",
                "session_timeout": "1d", "stapling": False},
    },
    "redis": {
        "memory": {"maxmemory": "256MB", "maxmemory_policy": "allkeys-lru",
                   "maxmemory_samples": 5},
        "persistence": {"save": ["3600 1"], "appendonly": False, "appendfsync": "everysec"},
        "security": {"protected_mode": True, "requirepass": "harness-test-pass",
                     "rename_commands": {}},
        "networking": {"bind": ["0.0.0.0"], "port": 6379},
    },
    "pg_hba": {"rules": [
        {"type": "local", "database": "all", "user": "all", "auth_method": "trust"},
        {"type": "host", "database": "all", "user": "all", "address": "0.0.0.0/0",
         "auth_method": "scram-sha-256"},
    ]},
}

SPEC_BAD = copy.deepcopy(SPEC_GOOD)
SPEC_BAD["postgres"]["security"]["password_encryption"] = "md5"


# --------------------------------------------------------------------------
# Scripted agent (the PROCESS panel)
# --------------------------------------------------------------------------


def _step(reasoning: str, action: str, tool: str, args: dict) -> AgentStep:
    return AgentStep(thought=AgentThought(reasoning=reasoning, planned_next_action=action),
                     tool_call=ToolCall(name=tool, args=args))


SCRIPT = [
    _step("Small dev stack, 2 vCPU / no compliance. I'll look up sizing guidance "
          "and the CIS authentication baseline first.",
          "search the documentation", "query_rag",
          {"question": "shared_buffers sizing and CIS auth baseline for a small Postgres stack"}),
    _step("Got pgtune sizing and the CIS auth rules. Building the stack spec and "
          "rendering the config files.",
          "generate the config files", "generate_config",
          {"partial_spec": SPEC_BAD, "mode": "deterministic"}),
    _step("Running a self-check on the generated configuration.",
          "self-check the config", "validate_config", {"spec": SPEC_BAD}),
    _step("Self-check flagged weak Postgres auth (md5). Switching to "
          "scram-sha-256 and regenerating.",
          "fix the auth setting and regenerate", "generate_config",
          {"partial_spec": SPEC_GOOD, "mode": "deterministic"}),
    _step("Re-checking the corrected configuration.",
          "re-check", "validate_config", {"spec": SPEC_GOOD}),
    _step("Auth fixed. Finalising — the independent validator will now deploy and "
          "score the configuration in Docker.",
          "finalise the run", "finalise",
          {"final_spec": SPEC_GOOD, "reason": "auth corrected; ready for validation"}),
]


class ScriptedModel(LLMClient):
    backend_name = "scripted-web"

    def __init__(self, steps: list[AgentStep]) -> None:
        self._steps = steps
        self._i = 0

    def chat(self, messages: list[dict[str, str]], schema: type | None = None):
        step = self._steps[min(self._i, len(self._steps) - 1)]
        self._i += 1
        return step, TokenUsage(input_tokens=210, output_tokens=70)


def _demo_registry() -> ToolRegistry:
    reg = ToolRegistry()

    def query_rag(**_kw):
        return [
            {"text": "shared_buffers ~= 25% of RAM (pgtune).", "source": "pgtune",
             "url": "", "service": "postgres", "score": 0.91},
            {"text": "CIS 1.2: password_encryption must be scram-sha-256.",
             "source": "CIS PostgreSQL Benchmark", "url": "", "service": "postgres", "score": 0.88},
        ]

    def generate_config(partial_spec, **_kw):
        return {"postgresql_conf": StackSpec.model_validate(partial_spec).postgres.render_conf()}

    def validate_config_stub(spec, **_kw):
        return {"auth_ok": spec["postgres"]["security"]["password_encryption"] == "scram-sha-256"}

    reg.register(Tool("query_rag", "search docs", QueryRagInput, query_rag))
    reg.register(Tool("generate_config", "render configs", GenerateConfigInput, generate_config))
    reg.register(Tool("validate_config", "self-check", ValidateConfigInput, validate_config_stub))
    reg.register(Tool("finalise", "accept & stop", FinaliseInput,
                      lambda **kw: finalise(kw["final_spec"], kw["reason"]).model_dump(mode="json")))
    return reg


def _summarise(tool: str, obs) -> str:
    if not obs.success:
        return f"error: {obs.error}"
    r = obs.result
    if tool == "query_rag":
        return f"{len(r)} doc chunks retrieved (top: {r[0]['source']})"
    if tool == "generate_config":
        return f"rendered configs (postgresql.conf = {len(r['postgresql_conf'].splitlines())} lines)"
    if tool == "validate_config":
        return "self-check: auth OK" if r.get("auth_ok") else "self-check: auth FAIL (md5)"
    if tool == "finalise":
        return "accepted — auth fixed; ready for full validation"
    return str(r)[:80]


def run_agent(prompt: str) -> dict:
    agent = SingleAgent(ScriptedModel(SCRIPT), TokenBudget(100_000),
                        max_iterations=25, registry=_demo_registry())
    result = agent.run(prompt)
    steps = [{"n": i, "tool": e.tool_call.name, "thought": e.thought.reasoning,
              "result": _summarise(e.tool_call.name, e.observation), "ok": e.observation.success}
             for i, e in enumerate(result.history, 1)]
    final = result.final_spec.model_dump(mode="json") if result.final_spec else SPEC_GOOD
    return {"prompt": prompt, "steps": steps, "final_spec": final}


# --------------------------------------------------------------------------
# Real deploy that STAYS UP (the OUTPUT panel) + manual teardown
# --------------------------------------------------------------------------

_ACTIVE: dict = {"runner": None}


def _force_clean() -> None:
    """Best-effort removal of any lingering demo stack (e.g. after a restart)."""
    try:
        ids = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"],
            capture_output=True, text=True, timeout=15).stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, text=True, timeout=30)
        subprocess.run(["docker", "network", "rm", NETWORK], capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def _teardown_active() -> None:
    runner = _ACTIVE.get("runner")
    _ACTIVE["runner"] = None
    if runner is not None:
        try:
            runner.down()
        except Exception:  # noqa: BLE001
            pass
    _force_clean()


def _probe(runner: StackRunner, service: str, label: str, command: list[str]) -> dict:
    try:
        r = runner.exec_in(service, command)
        out = (r.stdout or r.stderr or "").strip() or f"(exit {r.exit_code})"
    except Exception as exc:  # noqa: BLE001
        out = f"(probe failed: {exc})"
    return {"cmd": label, "out": out[:600]}


def _container_names(project: str) -> dict[str, str]:
    ps = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
         "--format", "{{.Names}}"], capture_output=True, text=True, timeout=15)
    names: dict[str, str] = {}
    for nm in ps.stdout.split():
        for svc in ("postgres", "redis", "nginx"):
            if svc in nm:
                names[svc] = nm
    for svc in ("postgres", "redis", "nginx"):
        names.setdefault(svc, f"{project}-{svc}-1")
    return names


def _inspect_limits(name: str) -> tuple[int | None, float | None]:
    """The Docker-enforced memory (MB) and CPU limits on a container."""
    try:
        r = subprocess.run(
            ["docker", "inspect", name, "--format", "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}"],
            capture_output=True, text=True, timeout=15)
        mem_b, nano = r.stdout.split()
        mem, nano = int(mem_b), int(nano)
        return (mem // (1024 * 1024) if mem > 0 else None, round(nano / 1e9, 2) if nano > 0 else None)
    except Exception:  # noqa: BLE001
        return (None, None)


def _fmt_limit(mem_mb: int | None, cpus: float | None) -> str:
    return f"{mem_mb} MB" if mem_mb else "no cap" + (f" · {cpus} CPU" if cpus else "")


def _deployed_specs(runner: StackRunner, spec: StackSpec, names: dict) -> dict:
    """Requested config vs the live values inside the running containers."""
    pw = spec.redis.security.requirepass
    rc = ["redis-cli", "-a", pw] if pw else ["redis-cli"]

    def pg(param: str) -> str:
        try:
            r = runner.exec_in("postgres", ["psql", "-U", "postgres", "-tAc", f"SHOW {param}"])
            return r.stdout.strip() or "?"
        except Exception:  # noqa: BLE001
            return "?"

    def rget(key: str) -> str:
        try:
            r = runner.exec_in("redis", [*rc, "CONFIG", "GET", key])
            parts = r.stdout.split()
            return parts[-1] if parts else "?"
        except Exception:  # noqa: BLE001
            return "?"

    def ngval(directive: str) -> str:
        try:
            r = runner.exec_in("nginx", ["grep", "-m1", "-w", directive, "/etc/nginx/nginx.conf"])
            line = r.stdout.strip().rstrip(";").split()
            return line[-1] if line else "?"
        except Exception:  # noqa: BLE001
            return "?"

    rm = rget("maxmemory")
    try:
        rm_live = f"{int(rm) // (1024 * 1024)}MB"
    except (ValueError, TypeError):
        rm_live = rm

    pgm, pgc = _inspect_limits(names["postgres"])
    rdm, rdc = _inspect_limits(names["redis"])
    ngm, ngc = _inspect_limits(names["nginx"])
    return {
        "postgres": {
            "limit": _fmt_limit(pgm, pgc),
            "rows": [
                ["shared_buffers", spec.postgres.memory.shared_buffers, pg("shared_buffers")],
                ["effective_cache_size", spec.postgres.memory.effective_cache_size, pg("effective_cache_size")],
                ["work_mem", spec.postgres.memory.work_mem, pg("work_mem")],
                ["max_connections", str(spec.postgres.connections.max_connections), pg("max_connections")],
            ],
        },
        "redis": {
            "limit": _fmt_limit(rdm, rdc),
            "rows": [
                ["maxmemory", spec.redis.memory.maxmemory, rm_live],
                ["maxmemory-policy", spec.redis.memory.maxmemory_policy, rget("maxmemory-policy")],
            ],
        },
        "nginx": {
            "limit": _fmt_limit(ngm, ngc),
            "rows": [
                ["worker_processes", str(spec.nginx.worker.worker_processes), ngval("worker_processes")],
                ["worker_connections", str(spec.nginx.worker.worker_connections), ngval("worker_connections")],
            ],
        },
    }


def deploy_inspect_keep(spec_dict: dict) -> dict:
    """Deploy for real, inspect live, CIS-score — and LEAVE IT RUNNING."""
    _teardown_active()  # clear any previous stack first
    spec = StackSpec.model_validate(spec_dict)
    runner = StackRunner(run_id=RUN_ID)
    try:
        runner.up(spec)
    except StackStartupError as exc:
        _teardown_active()
        return {"error": f"stack failed to start: {exc}"}

    _ACTIVE["runner"] = runner  # keep it up; /teardown will clean it
    pw = spec.redis.security.requirepass
    rc = ["redis-cli", "-a", pw] if pw else ["redis-cli"]

    ps = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={runner.project}",
         "--format", "{{.Names}}  {{.Image}}  {{.Status}}"],
        capture_output=True, text=True, timeout=15)
    probes = {
        "host": [{"cmd": "docker ps", "out": (ps.stdout or ps.stderr).strip()}],
        "postgres": [
            _probe(runner, "postgres", "psql -tAc 'SELECT version()'",
                   ["psql", "-U", "postgres", "-tAc", "SELECT version()"]),
            _probe(runner, "postgres", "psql -tAc 'SHOW password_encryption'",
                   ["psql", "-U", "postgres", "-tAc", "SHOW password_encryption"]),
            _probe(runner, "postgres", "psql -tAc 'SHOW ssl'",
                   ["psql", "-U", "postgres", "-tAc", "SHOW ssl"]),
        ],
        "redis": [
            _probe(runner, "redis", "redis-cli PING", [*rc, "PING"]),
            _probe(runner, "redis", "redis-cli CONFIG GET maxmemory", [*rc, "CONFIG", "GET", "maxmemory"]),
        ],
        "nginx": [
            _probe(runner, "nginx", "nginx -v", ["nginx", "-v"]),
            _probe(runner, "nginx", "curl -sI localhost", ["curl", "-sI", "http://localhost/"]),
        ],
    }

    smoke = _run_smoke_tests(runner, spec)
    benchmarks = {"postgres": run_pgbench(runner, duration_s=5),
                  "redis": run_redis_benchmark(runner, duration_s=5, password=pw)}
    cis = []
    for checker in (PostgresCISChecker, NginxCISChecker, RedisCISChecker):
        cis.extend(checker(runner).run_all())

    report = {
        "smoke_tests": {s: {"did_start": v.did_start, "accepts_connections": v.accepts_connections}
                        for s, v in smoke.items()},
        "benchmarks": {s: {"throughput": b.throughput} for s, b in benchmarks.items()},
        "cis_results": [c.model_dump(mode="json") for c in cis],
    }
    names = _container_names(runner.project)
    execs = [
        {"svc": "postgres", "cmd": f"docker exec -it {names['postgres']} psql -U postgres"},
        {"svc": "redis", "cmd": f"docker exec -it {names['redis']} redis-cli -a {pw}"},
        {"svc": "nginx", "cmd": f"docker exec -it {names['nginx']} sh"},
    ]
    specs = _deployed_specs(runner, spec, names)
    return {"probes": probes, "report": report, "execs": execs, "specs": specs}


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multi-service deployment agent — demo</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#2a2f3a; --text:#e6e8ec;
          --muted:#9aa3b2; --accent:#E8593C; --good:#2ecc71; --bad:#e74c3c; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 -apple-system,system-ui,sans-serif; background:var(--bg); color:var(--text); }
  header { padding:20px 28px; border-bottom:1px solid var(--line); }
  header h1 { margin:0 0 4px; font-size:18px; }
  header p { margin:0; color:var(--muted); font-size:12.5px; }
  .note { margin-top:8px; font-size:12px; color:var(--accent); }
  main { display:grid; grid-template-columns:1fr 1fr 1.2fr; min-height:calc(100vh - 96px); }
  section { padding:20px 22px; border-right:1px solid var(--line); }
  section:last-child { border-right:none; }
  .lbl { font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); margin-bottom:12px; }
  textarea { width:100%; height:150px; resize:vertical; background:var(--panel); color:var(--text);
             border:1px solid var(--line); border-radius:8px; padding:12px; font:13px/1.5 inherit; }
  button { margin-top:14px; width:100%; padding:11px; border:none; border-radius:8px; background:var(--accent);
           color:#fff; font-weight:600; font-size:14px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .hint { margin-top:10px; font-size:11.5px; color:var(--muted); }
  .step { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 12px;
          margin-bottom:10px; opacity:0; transform:translateY(6px); transition:.35s; }
  .step.show { opacity:1; transform:none; }
  .step .head { font-weight:600; font-size:12.5px; }
  .step .head .tool { color:var(--accent); }
  .step .thought { color:var(--muted); font-size:12.5px; margin:4px 0; }
  .step .res { font-size:12px; }
  .res .ok { color:var(--good); } .res .no { color:var(--bad); }
  .status { color:var(--muted); font-size:12.5px; margin-bottom:12px; }
  .termlbl { font-size:11px; color:var(--muted); margin:2px 0 8px; }
  .term { background:#0a0c10; border:1px solid var(--line); border-radius:8px; padding:10px 12px;
          font:11.5px/1.55 ui-monospace,Menlo,monospace; margin-bottom:16px; max-height:230px; overflow:auto; }
  .tcmd { color:var(--accent); white-space:pre-wrap; }
  .tcmd .host { color:var(--muted); }
  .tout { color:#9fb4c9; white-space:pre-wrap; margin:1px 0 9px; }
  .svc { margin-bottom:12px; }
  .svc h4 { margin:0 0 6px; font-size:13px; }
  .svc h4 .frac { color:var(--muted); font-weight:400; font-size:12px; }
  .chk { font-size:12px; padding:2px 0; display:flex; gap:8px; }
  .chk .tag { width:42px; font-weight:600; }
  .pass { color:var(--good); } .fail { color:var(--bad); }
  table.spec { width:100%; border-collapse:collapse; font-size:11.5px; margin-bottom:14px; }
  table.spec th { text-align:left; color:var(--muted); font-weight:500; padding:2px 10px 4px 0; border-bottom:1px solid var(--line); }
  table.spec td { padding:3px 10px 3px 0; font-family:ui-monospace,Menlo,monospace; }
  table.spec td.match { color:var(--good); }
  .rate { margin:6px 0 16px; padding:12px; background:var(--panel); border:1px solid var(--line);
          border-radius:8px; font-size:13px; }
  .rate b { font-size:20px; }
  .bench { font-size:12.5px; color:var(--muted); margin-bottom:8px; }
  .live { border:1px solid var(--good); border-radius:8px; padding:12px 14px; margin-top:8px; }
  .live .h { font-size:12.5px; font-weight:600; color:var(--good); margin-bottom:8px; }
  .live .cmd { font:11.5px/1.7 ui-monospace,Menlo,monospace; color:var(--text);
               background:#0a0c10; border:1px solid var(--line); border-radius:6px;
               padding:7px 9px; margin-bottom:6px; white-space:pre-wrap; user-select:all; }
  .teardown { background:#3a2020; color:#ff9a8a; margin-top:10px; }
  .spin { display:inline-block; width:13px; height:13px; border:2px solid var(--line);
          border-top-color:var(--accent); border-radius:50%; animation:s .8s linear infinite; vertical-align:-2px; }
  @keyframes s { to { transform:rotate(360deg); } }
</style></head>
<body>
<header>
  <h1>Natural-language &rarr; deployed, security-scored web stack</h1>
  <p>Single-agent baseline &middot; nginx + PostgreSQL + Redis &middot; MSc dissertation system</p>
  <p class="note">Demo mode: the agent's model is scripted for reliability. The ReAct loop, config
     rendering, and everything under Output (real containers, live exec, CIS checks) is the real system.</p>
</header>
<main>
  <section>
    <div class="lbl">1 &middot; Input</div>
    <textarea id="prompt">I need a small dev web stack for an internal tool. ~5 developers, about 5GB of Postgres data, on a single 8GB / 4-vCPU host. No compliance, but use sane security defaults.</textarea>
    <button id="run" onclick="runDemo()">Run the agent</button>
    <div class="hint">Deploys real containers and LEAVES THEM RUNNING so you can exec in. Tear down with the button when done.</div>
  </section>
  <section>
    <div class="lbl">2 &middot; Process &mdash; the agent's reasoning</div>
    <div id="steps"></div>
  </section>
  <section>
    <div class="lbl">3 &middot; Output &mdash; real Docker validation</div>
    <div id="output"><div class="status">Run the agent to deploy and inspect its configuration.</div></div>
  </section>
</main>
<script>
const $ = s => document.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function runDemo() {
  const btn = $('#run'); btn.disabled = true;
  $('#steps').innerHTML = '';
  $('#output').innerHTML = '<div class="status">Waiting for the agent&hellip;</div>';

  const a = await (await fetch('/agent', {method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({prompt: $('#prompt').value})})).json();

  for (const s of a.steps) {
    const el = document.createElement('div'); el.className = 'step';
    const r = s.ok ? '<span class="ok">'+esc(s.result)+'</span>' : '<span class="no">'+esc(s.result)+'</span>';
    el.innerHTML = '<div class="head">Step '+s.n+' &middot; <span class="tool">'+esc(s.tool)+'</span></div>'
                 + '<div class="thought">'+esc(s.thought)+'</div><div class="res">&rarr; '+r+'</div>';
    $('#steps').appendChild(el);
    await sleep(60); el.classList.add('show'); await sleep(520);
  }

  $('#output').innerHTML = '<div class="status"><span class="spin"></span> '
    + 'Deploying real containers, inspecting them, scoring CIS&hellip; (the stack will stay up)</div>';

  const data = await (await fetch('/validate', {method:'POST', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify({spec: a.final_spec})})).json();
  renderOutput(data);
  btn.disabled = false;
}

function renderOutput(data) {
  if (data.flask_error || data.error) {
    $('#output').innerHTML = '<div class="status fail">Error: '+esc(data.flask_error || data.error)+'</div>'; return;
  }
  const probes = data.probes || {}, rep = data.report || {}, execs = data.execs || [];
  let html = '';

  html += '<div class="termlbl">Live, from inside the running containers</div><div class="term">';
  for (const key of ['host','postgres','redis','nginx']) {
    for (const p of (probes[key] || [])) {
      html += '<div class="tcmd"><span class="host">'+(key==='host'?'host':key)+' $</span> '+esc(p.cmd)+'</div>';
      html += '<div class="tout">'+esc(p.out)+'</div>';
    }
  }
  html += '</div>';

  const specs = data.specs || {};
  if (Object.keys(specs).length) {
    html += '<div class="termlbl">Deployed resource allocation &amp; live parameters (requested vs. running)</div>';
    ['postgres','redis','nginx'].forEach(svc => {
      const sp = specs[svc]; if (!sp) return;
      html += '<div class="svc"><h4>'+svc+' <span class="frac">container limit: '+esc(sp.limit)+'</span></h4>';
      html += '<table class="spec"><tr><th>parameter</th><th>requested</th><th>live in container</th></tr>';
      (sp.rows || []).forEach(r => {
        const norm = x => String(x).replace(/\\s/g,'').toLowerCase();
        const cls = norm(r[1]) === norm(r[2]) ? 'match' : '';
        html += '<tr><td>'+esc(r[0])+'</td><td>'+esc(r[1])+'</td><td class="'+cls+'">'+esc(r[2])+'</td></tr>';
      });
      html += '</table></div>';
    });
  }

  const smoke = Object.entries(rep.smoke_tests || {}).map(([s,v]) =>
    (v.did_start && v.accepts_connections) ? '<span class="pass">'+s+'</span>' : '<span class="fail">'+s+'</span>'
  ).join(' &middot; ');
  html += '<div class="bench">Smoke tests: '+smoke+'</div>';
  const bn = Object.entries(rep.benchmarks || {}).map(([s,v]) =>
    v.throughput != null ? s+' '+Math.round(v.throughput).toLocaleString() : s+' (n/a)').join(' &middot; ');
  html += '<div class="bench">Throughput: '+bn+'</div>';

  const cis = rep.cis_results || [], byS = {};
  cis.forEach(c => { (byS[c.service] = byS[c.service] || []).push(c); });
  ['postgres','nginx','redis'].forEach(svc => {
    const cs = byS[svc] || []; if (!cs.length) return;
    const p = cs.filter(c => c.passed).length;
    html += '<div class="svc"><h4>'+svc+' <span class="frac">'+p+'/'+cs.length+'</span></h4>';
    cs.forEach(c => {
      const tag = c.passed ? '<span class="tag pass">PASS</span>' : '<span class="tag fail">FAIL</span>';
      html += '<div class="chk">'+tag+'<span>'+esc(c.control_id)+'  '+esc(c.name)+'</span></div>';
    });
    html += '</div>';
  });
  const total = cis.length, passed = cis.filter(c => c.passed).length;
  const rate = total ? Math.round(100*passed/total) : 0;
  html += '<div class="rate">Independent CIS score on the agent\\'s config: <b>'+rate+'%</b> ('
        + passed+'/'+total+' controls). The agent fixed auth; the validator reveals what remains.</div>';

  html += '<div class="live"><div class="h">&#9679; The stack is still running &mdash; exec in yourself</div>';
  execs.forEach(e => { html += '<div class="cmd">'+esc(e.cmd)+'</div>'; });
  html += '<button class="teardown" onclick="teardownStack(this)">Tear down the stack</button></div>';

  $('#output').innerHTML = html;
}

async function teardownStack(btn) {
  btn.disabled = true; btn.textContent = 'Tearing down…';
  await fetch('/teardown', {method:'POST'});
  btn.textContent = 'Stack torn down';
}
</script>
</body></html>"""


app = Flask(__name__)


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/agent", methods=["POST"])
def agent_ep():
    return jsonify(run_agent((request.json or {}).get("prompt", "")))


@app.route("/validate", methods=["POST"])
def validate_ep():
    spec = (request.json or {}).get("spec", {})
    try:
        return jsonify(deploy_inspect_keep(spec))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"flask_error": str(exc)})


@app.route("/teardown", methods=["POST"])
def teardown_ep():
    _teardown_active()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("\n  Open  http://127.0.0.1:5001  in your browser.  (Ctrl-C to stop)\n")
    print("  Note: the demo stack stays up after a run; use the Tear down button")
    print("  in the page, or it is cleaned automatically on the next run.\n")
    app.run(host="127.0.0.1", port=5001, debug=False)
