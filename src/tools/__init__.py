"""Agent tools.

Each module wraps one capability behind a simple, serialisable function
the agent can call: ``query_rag`` (retrieval), ``generate_config``
(rendering/completion), ``validate_config`` (deployment validation),
``finalise`` (loop termination). The registry (Step 9) exposes them with
a uniform dispatch interface.
"""
