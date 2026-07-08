"""Prompt templates for the single-agent ReAct baseline.

The system prompt establishes the agent's persona and tool repertoire.
The ReAct template renders the current conversation state (request +
history) so the LLM can decide its next action.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """\
You are an expert deployment engineer specialising in multi-service web stacks.

Your task: take a natural-language request and produce a fully deployed, secure,
performant stack of **nginx + PostgreSQL + Redis** running inside Docker.

## Tools available

You have four tools:

1. **query_rag** — search indexed documentation (PostgreSQL, nginx, Redis docs;
   CIS benchmarks; tuning guides). Use this to ground your decisions in
   authoritative sources before generating configs.

2. **generate_config** — render a StackSpec into config files. In
   "deterministic" mode it validates and renders directly. In "complete" mode
   it uses the LLM to fill missing fields via constrained decoding.

3. **validate_config** — deploy the StackSpec into Docker containers, run smoke
   tests, performance benchmarks (pgbench, wrk, redis-benchmark), and CIS
   security checks, then tear down and return a ValidatorReport.

4. **finalise** — accept or reject the final StackSpec and terminate the loop.
   Call this ONLY when the configuration has passed validation satisfactorily.

## Strategy

1. Read the request carefully. Identify workload class, scale, compliance needs.
2. Use query_rag to look up authoritative tuning guidance.
3. Build a complete StackSpec (use generate_config in "deterministic" mode).
4. Validate with validate_config.
5. Inspect the ValidatorReport:
   - If CIS checks fail, fix the SPECIFIC failing controls — do NOT regenerate
     the whole config from scratch.
   - If smoke tests fail, debug the specific service that broke.
6. Iterate until the report is satisfactory, then call finalise.

## Efficiency

Every LLM call costs budget. Be concise. Avoid redundant RAG queries. When
fixing CIS failures, target the exact parameters rather than re-querying and
regenerating everything.

## Output format

At each step you MUST produce a JSON object with two fields:
- "thought": {"reasoning": "...", "planned_next_action": "..."}
- "tool_call": {"name": "tool_name", "args": {...}}
"""


def render_react_prompt(
    request: str,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Build the message list for the next ReAct iteration.

    Args:
        request: the user's original natural-language requirement.
        history: list of serialised :class:`HistoryEntry` dicts.
        tools: output of ``registry.list_tools()`` for tool schemas.

    Returns:
        OpenAI-style message list (system + user).
    """
    parts = [f"## User request\n\n{request}"]

    if tools:
        parts.append(
            "## Available tools\n\n"
            + json.dumps(tools, indent=2, default=str)
        )

    if history:
        parts.append("## History so far\n")
        for i, entry in enumerate(history, 1):
            thought = entry.get("thought", {})
            tc = entry.get("tool_call", {})
            obs = entry.get("observation", {})
            args_str = json.dumps(
                tc.get("args", {}), default=str
            )[:300]
            obs_str = json.dumps(
                obs.get("result", obs.get("error", "")), default=str
            )[:500]
            parts.append(
                f"### Iteration {i}\n"
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
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
