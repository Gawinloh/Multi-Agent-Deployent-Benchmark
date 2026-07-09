"""Multi-agent architecture: orchestrator + specialist workers.

Star topology: the orchestrator receives the user request, delegates
sub-tasks to specialist workers, collects results, and decides when to
call ``finalise``.  Workers never communicate with each other directly.

Workers:
    1. **config** — generates StackSpec via query_rag + generate_config.
    2. **security** — reviews CIS results, suggests hardening via query_rag.
    3. **validation** — deploys and tests via validate_config, interprets results.
"""
