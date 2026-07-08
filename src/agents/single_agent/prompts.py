"""Prompt templates for the single-agent ReAct baseline.

The system prompt establishes the agent's persona and tool repertoire.
The ReAct template renders the current conversation state (request +
history) so the LLM can decide its next action.
"""

from __future__ import annotations

import json
from typing import Any

# Simplified requirements-only example for the agent prompt.
# The agent passes this to generate_config in "complete" mode; the tool
# handles the full StackSpec generation via constrained decoding.
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

SYSTEM_PROMPT = """\
You are an expert deployment engineer specialising in multi-service web stacks.

Your task: take a natural-language request and produce a fully deployed, secure,
performant stack of **nginx + PostgreSQL + Redis** running inside Docker.

## Tools available

You have four tools:

1. **query_rag** — search indexed documentation (PostgreSQL, nginx, Redis docs;
   CIS benchmarks; tuning guides). Use this to ground your decisions in
   authoritative sources before generating configs.

2. **generate_config** — produce config files from requirements. Call with
   mode "complete" and a partial_spec containing a requirements dict. The
   tool fills in all postgres, nginx, redis, and pg_hba fields automatically
   using authoritative defaults and constrained decoding.
   Do NOT try to build the full service config yourself.

3. **validate_config** — deploy the generated stack into Docker containers,
   run smoke tests, benchmarks, and CIS security checks, then tear down and
   return a ValidatorReport.

4. **finalise** — accept or reject the final StackSpec and terminate the loop.
   Call this ONLY when the configuration has passed validation satisfactorily.

## generate_config call format

Pass mode "complete" and a partial_spec with the requirements dict:

""" + _REQUIREMENTS_EXAMPLE + """

### Key constraints on requirements values
- workload_class: must be one of "OLTP", "OLAP", "CACHING_HEAVY", "BALANCED"
- compliance: must be one of "NONE", "GDPR_UK", "HIPAA", "PCI_DSS"
- hardware must have exactly: ram_gb (number), vcpu (integer), disk_gb (number)

## Strategy

1. Read the request carefully. Identify workload class, scale, compliance needs.
2. Use query_rag to look up authoritative tuning guidance (1-2 queries max).
3. Call generate_config with mode "complete" and partial_spec containing only
   the requirements dict. The tool handles all service-level configuration.
4. Validate with validate_config.
5. If validation fails, call generate_config again with adjusted requirements.
6. Iterate until satisfactory, then call finalise.

## Efficiency

Every LLM call costs budget. Be concise. Avoid redundant RAG queries.

## Output format

At each step you MUST produce a JSON object with EXACTLY two fields:
- "thought": {"reasoning": "your analysis here", "planned_next_action": "what you will do next"}
- "tool_call": {"name": "tool_name", "args": {...}}
"""

#: Maximum number of history entries to include in the prompt.
#: Older entries are collapsed to a one-line summary to keep context
#: manageable for small models.
_MAX_HISTORY_DETAIL = 4


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

        # Collapse older iterations to one-line summaries
        if len(history) > _MAX_HISTORY_DETAIL:
            old = history[: -_MAX_HISTORY_DETAIL]
            for i, entry in enumerate(old, 1):
                tc = entry.get("tool_call", {})
                obs = entry.get("observation", {})
                ok = "ok" if obs.get("success") else "FAIL"
                parts.append(f"- Iteration {i}: {tc.get('name', '?')} → {ok}")
            parts.append("")

        # Full detail for recent iterations
        recent = history[-_MAX_HISTORY_DETAIL:]
        start_idx = max(1, len(history) - _MAX_HISTORY_DETAIL + 1)
        for i, entry in enumerate(recent, start_idx):
            thought = entry.get("thought", {})
            tc = entry.get("tool_call", {})
            obs = entry.get("observation", {})
            args_str = json.dumps(
                tc.get("args", {}), default=str
            )[:500]
            obs_str = json.dumps(
                obs.get("result", obs.get("error", "")), default=str
            )[:800]
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
