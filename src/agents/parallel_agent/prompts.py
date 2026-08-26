"""Prompt templates for the parallel per-service architecture.

Two roles only: a manager that decomposes once, and a service agent that knows
about exactly one service. The service-agent prompt is deliberately close to
the star architecture's config-worker prompt, so that a difference in results
between the two arms is attributable to the topology rather than to one arm
having been given better instructions.
"""

from __future__ import annotations

import json
from typing import Any

from src.services.catalog import names

# Shared with the other arms so the comparison is not confounded by one
# architecture seeing a better worked example than another.
_REQUIREMENTS_EXAMPLE = """\
{
  "workload_class": "BALANCED",
  "expected_concurrent_users": 10,
  "expected_data_size_gb": 5,
  "hardware": {"ram_gb": 8, "vcpu": 4, "disk_gb": 100},
  "compliance": "NONE",
  "backup_required": false,
  "selected_services": ["nginx", "postgres"]
}"""

_SERVICE_PURPOSE = {
    "postgres": "a relational database — persistent, queryable data of record",
    "nginx": "an HTTP server or reverse proxy — serving or fronting web traffic",
    "redis": (
        "an in-memory cache — repeated reads of the same data, where the same "
        "values are fetched more than once"
    ),
    "rabbitmq": (
        "a message broker — work accepted at request time but performed later "
        "by a separate consumer"
    ),
}


def render_manager_prompt(request: str) -> list[dict[str, str]]:
    """One call: decide which services the request needs, and the shared
    requirements every service agent will size against."""
    catalogue = "\n".join(
        f"- **{name}** — {_SERVICE_PURPOSE.get(name, '')}" for name in names()
    )
    system = f"""\
You are the manager of a parallel deployment system for Docker service stacks.

You make ONE decision: which services from the catalogue this request needs, \
and the shared requirements block describing the host and workload. \
Independent specialist agents will then configure each selected service \
concurrently, and a merge step will combine their work.

## Catalogue

{catalogue}

## How to decide

Include a service when the request describes the need it meets, whether or not \
the request names the service. Exclude a service when the request does not \
describe that need, even if it names it. A service mentioned in passing, or \
inherited from a document the requester is questioning, is not a requirement. \
Justify each inclusion and each exclusion against something the request \
actually says.

## Your response

`selected_services` is the decision. It must name at least one catalogue \
service, and exactly the services you are including — reasoning about a \
service is not the same as selecting it.

Fill every field of the requirements block from the request. Where the request \
does not state a value, infer a reasonable one from what it does say. \
``hardware`` must reflect the host the request describes.

Example requirements block:

{_REQUIREMENTS_EXAMPLE}

Respond with reasoning, `selected_services`, the requirements block, and \
optional per-service notes.
"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": request},
    ]


def render_service_agent_prompt(
    service: str,
    request: str,
    requirements: dict[str, Any],
    note: str,
    history: list[dict[str, Any]],
    tools_desc: str,
) -> list[dict[str, str]]:
    """Prompt for one per-service agent.

    The agent sees the original request and the shared host constraints, but is
    responsible for exactly one service. It is told explicitly that other
    services are being configured concurrently by other agents and that the
    host is shared, because that is the situation the topology creates and
    hiding it would make the arm a straw man.
    """
    hardware = requirements.get("hardware") or {}
    others = [
        name for name in (requirements.get("selected_services") or []) if name != service
    ]
    concurrent = (
        f"Other agents are configuring {', '.join(others)} concurrently, on the "
        "same host, right now. You cannot see their work and they cannot see "
        "yours. A merge step will combine the fragments afterwards and will "
        "reduce memory reservations proportionally if they collectively "
        "exceed what the host can provide."
        if others
        else "You are the only service agent for this request."
    )

    system = f"""\
You are the **{service}** specialist in a parallel deployment system.

You configure {service} and nothing else. {_SERVICE_PURPOSE.get(service, '')}

## Shared host

RAM {hardware.get('ram_gb', '?')} GB, {hardware.get('vcpu', '?')} vCPU, \
disk {hardware.get('disk_gb', '?')} GB.

{concurrent}

Size {service} for the share of this host it actually needs, not for the whole \
machine. Leave room for the other services and for the operating system.

## Shared requirements

{json.dumps(requirements, indent=2)}

## Tools

{tools_desc}

## How to work

1. Optionally call `query_rag` to ground your parameter choices in the corpus.
2. Call `generate_config` with a partial spec containing the shared \
requirements block and your `{service}` section. Only your own service section \
will be kept; anything you write for another service is discarded.
3. You have a small number of iterations. Do not repeat a query that already \
returned what you needed.

{f'Manager note for you: {note}' if note else ''}
"""
    messages = [{"role": "system", "content": system}]
    messages.append({"role": "user", "content": f"Original request:\n{request}"})
    if history:
        messages.append(
            {
                "role": "user",
                "content": "Your actions so far:\n" + json.dumps(history, indent=2),
            }
        )
    return messages
