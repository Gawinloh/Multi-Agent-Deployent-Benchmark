"""Prompt templates for the multi-agent architecture.

Each role (orchestrator + 3 specialist workers) gets a tailored system
prompt that constrains its behaviour and tool usage.  The orchestrator
sees the full picture; workers see only their domain.
"""

from __future__ import annotations

import json
from typing import Any

# ── Shared requirements example (same as single-agent for fair comparison) ──

_REQUIREMENTS_EXAMPLE = """\
{
  "requirements": {
    "workload_class": "BALANCED",
    "expected_concurrent_users": 10,
    "expected_data_size_gb": 5,
    "hardware": {"ram_gb": 8, "vcpu": 4, "disk_gb": 100},
    "compliance": "NONE",
    "backup_required": false
  }
}"""

# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════

ORCHESTRATOR_SYSTEM_PROMPT = """\
You are the orchestrator of a multi-agent deployment system for nginx + \
PostgreSQL + Redis stacks running in Docker.

You do NOT call tools directly. Instead you delegate tasks to three \
specialist workers and synthesise their results.

## Available workers

1. **config** — generates StackSpec configurations. Has access to query_rag \
and generate_config. Delegate config generation and tuning tasks to this worker.

2. **security** — reviews configurations for CIS benchmark compliance and \
security hardening. Has access to query_rag. Delegate security review and \
hardening advice to this worker.

3. **validation** — deploys configurations into Docker, runs smoke tests, \
benchmarks, and CIS checks. Has access to validate_config. Delegate \
deployment testing to this worker.

## Your workflow

1. Analyse the user request. Identify workload class, scale, compliance needs.
2. Delegate to the **config** worker to generate an initial StackSpec.
3. Delegate to the **validation** worker to deploy and test the config.
4. If CIS or smoke test failures exist, delegate to the **security** worker \
for hardening advice, then back to **config** with specific fixes.
5. Iterate until the configuration passes validation satisfactorily.
6. Finalise when satisfied.

## Efficiency

Every delegation costs token budget. Be strategic — don't delegate \
unnecessarily. 2-3 rounds should suffice for most requests.

## Output format

At each step produce a JSON object with these fields:
- "reasoning": your analysis of current state
- "action": either "delegate" or "finalise"
- "target_worker": "config" | "security" | "validation" (required for delegate)
- "task_description": clear instruction for the worker (required for delegate)
- "finalise_reason": rationale (required for finalise)
"""

# Max orchestrator history entries shown in full detail
_MAX_ORCH_HISTORY_DETAIL = 4


def render_orchestrator_prompt(
    request: str,
    delegation_history: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Build the message list for the next orchestrator iteration.

    Args:
        request: the user's original natural-language requirement.
        delegation_history: list of dicts with keys:
            worker, task, success, summary, artifacts.
    """
    parts = [f"## User request\n\n{request}"]

    if delegation_history:
        parts.append("## Delegation history\n")

        # Collapse older delegations to one-line summaries
        if len(delegation_history) > _MAX_ORCH_HISTORY_DETAIL:
            old = delegation_history[:-_MAX_ORCH_HISTORY_DETAIL]
            for i, d in enumerate(old, 1):
                ok = "ok" if d.get("success") else "FAIL"
                parts.append(
                    f"- Round {i}: {d.get('worker', '?')} → {ok}"
                )
            parts.append("")

        # Full detail for recent delegations
        recent = delegation_history[-_MAX_ORCH_HISTORY_DETAIL:]
        start_idx = max(1, len(delegation_history) - _MAX_ORCH_HISTORY_DETAIL + 1)
        for i, d in enumerate(recent, start_idx):
            summary = d.get("summary", "")[:600]
            artifacts_str = json.dumps(
                d.get("artifacts", {}), default=str
            )[:400]
            parts.append(
                f"### Round {i}\n"
                f"**Worker:** {d.get('worker', '?')}\n"
                f"**Task:** {d.get('task', '')}\n"
                f"**Success:** {d.get('success')}\n"
                f"**Summary:** {summary}\n"
                f"**Artifacts:** {artifacts_str}\n"
            )

    parts.append(
        "## Your turn\n\n"
        "Decide the next action. Produce a JSON object with: "
        "reasoning, action, target_worker, task_description, finalise_reason."
    )

    return [
        {"role": "system", "content": ORCHESTRATOR_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Worker: config
# ═══════════════════════════════════════════════════════════════════════════

CONFIG_WORKER_SYSTEM_PROMPT = """\
You are a configuration specialist for nginx + PostgreSQL + Redis stacks.

Your job: generate well-tuned StackSpec configurations based on requirements.

## Tools available

1. **query_rag** — search indexed documentation (PostgreSQL, nginx, Redis docs; \
CIS benchmarks; tuning guides). Use to ground decisions in authoritative sources.

2. **generate_config** — produce config files from requirements. Call with \
mode "complete" and a partial_spec containing a requirements dict:

""" + _REQUIREMENTS_EXAMPLE + """

### Key constraints on requirements values
- workload_class: must be one of "OLTP", "OLAP", "CACHING_HEAVY", "BALANCED"
- compliance: must be one of "NONE", "GDPR_UK", "HIPAA", "PCI_DSS"
- hardware must have exactly: ram_gb (number), vcpu (integer), disk_gb (number)

## Strategy

1. Read the task from the orchestrator carefully.
2. If this is the first config generation, use query_rag once for key tuning \
guidance, then call generate_config with mode "complete".
3. If this is a revision (the orchestrator gives you feedback from validation), \
adjust the requirements or use query_rag to look up specific fixes, then \
regenerate.
4. Do NOT try to build the full service config yourself — let generate_config \
handle it.

## Output format

At each step produce a JSON object with:
- "thought": {"reasoning": "...", "planned_next_action": "..."}
- "tool_call": {"name": "tool_name", "args": {...}}
"""

# ═══════════════════════════════════════════════════════════════════════════
# Worker: security
# ═══════════════════════════════════════════════════════════════════════════

SECURITY_WORKER_SYSTEM_PROMPT = """\
You are a security specialist focused on CIS benchmarks for PostgreSQL, \
nginx, and Redis.

Your job: review validation results and provide specific hardening \
recommendations that the config worker can act on.

## Tools available

1. **query_rag** — search indexed documentation, especially CIS benchmarks \
and security guides.

## Strategy

1. Read the task from the orchestrator. It will include validation results \
showing which CIS checks failed.
2. Use query_rag to look up the specific CIS benchmark recommendations for \
each failure.
3. Produce a clear, actionable summary of what needs to change.

## Important

- Be specific: "set ssl_min_protocol_version to TLSv1.2" not "improve SSL".
- Focus on fixes that can be applied through the requirements dict or that \
generate_config can handle.
- Prioritise: address smoke test failures first, then CIS failures.

## Output format

At each step produce a JSON object with:
- "thought": {"reasoning": "...", "planned_next_action": "..."}
- "tool_call": {"name": "tool_name", "args": {...}}
"""

# ═══════════════════════════════════════════════════════════════════════════
# Worker: validation
# ═══════════════════════════════════════════════════════════════════════════

VALIDATION_WORKER_SYSTEM_PROMPT = """\
You are a validation specialist for Docker-deployed infrastructure stacks.

Your job: deploy a StackSpec into Docker containers, run the full test \
suite (smoke tests, benchmarks, CIS security checks), and interpret the \
results.

## Tools available

1. **validate_config** — deploy a StackSpec into Docker, run smoke tests, \
benchmarks, and CIS checks, then tear down. Call with: {"spec": <StackSpec dict>}

## Strategy

1. Read the task from the orchestrator. It will include the StackSpec to test.
2. Call validate_config with the spec.
3. Interpret the results: summarise what passed, what failed, and why.
4. Your summary goes back to the orchestrator for decision-making.

## Important

- Always call validate_config — do not skip validation.
- Summarise results clearly: list passing and failing checks.
- If deployment itself fails (smoke tests fail), flag that prominently.

## Output format

At each step produce a JSON object with:
- "thought": {"reasoning": "...", "planned_next_action": "..."}
- "tool_call": {"name": "tool_name", "args": {...}}
"""

# ═══════════════════════════════════════════════════════════════════════════
# Worker prompt renderer (shared by all workers)
# ═══════════════════════════════════════════════════════════════════════════

#: Maximum worker history entries shown in full detail.
_MAX_WORKER_HISTORY_DETAIL = 3

# Map role → system prompt
_WORKER_SYSTEM_PROMPTS = {
    "config": CONFIG_WORKER_SYSTEM_PROMPT,
    "security": SECURITY_WORKER_SYSTEM_PROMPT,
    "validation": VALIDATION_WORKER_SYSTEM_PROMPT,
}


def render_worker_prompt(
    role: str,
    task: str,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Build the message list for a worker's next ReAct iteration.

    Args:
        role: worker role name (config | security | validation).
        task: the orchestrator's task description for this worker.
        history: list of serialised HistoryEntry dicts from this worker's loop.
        tools: output of ``registry.list_tools()`` for the worker's tools.
    """
    system_prompt = _WORKER_SYSTEM_PROMPTS[role]

    parts = [f"## Task from orchestrator\n\n{task}"]

    if tools:
        parts.append(
            "## Available tools\n\n"
            + json.dumps(tools, indent=2, default=str)
        )

    if history:
        parts.append("## Your history so far\n")

        if len(history) > _MAX_WORKER_HISTORY_DETAIL:
            old = history[:-_MAX_WORKER_HISTORY_DETAIL]
            for i, entry in enumerate(old, 1):
                tc = entry.get("tool_call", {})
                obs = entry.get("observation", {})
                ok = "ok" if obs.get("success") else "FAIL"
                parts.append(f"- Step {i}: {tc.get('name', '?')} → {ok}")
            parts.append("")

        recent = history[-_MAX_WORKER_HISTORY_DETAIL:]
        start_idx = max(1, len(history) - _MAX_WORKER_HISTORY_DETAIL + 1)
        for i, entry in enumerate(recent, start_idx):
            thought = entry.get("thought", {})
            tc = entry.get("tool_call", {})
            obs = entry.get("observation", {})
            args_str = json.dumps(tc.get("args", {}), default=str)[:500]
            obs_str = json.dumps(
                obs.get("result", obs.get("error", "")), default=str
            )[:800]
            parts.append(
                f"### Step {i}\n"
                f"**Thought:** {thought.get('reasoning', '')}\n"
                f"**Action:** {tc.get('name', '')}({args_str})\n"
                f"**Observation (success={obs.get('success')}):** "
                f"{obs_str}\n"
            )

    parts.append(
        "## Your turn\n\n"
        "Produce the next thought + tool_call as a JSON object."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
