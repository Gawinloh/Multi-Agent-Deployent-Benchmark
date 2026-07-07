"""Phase B sanity checks — run after Ollama is serving qwen2.5:7b.

Usage:
    python scripts/phase_b_sanity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.llm.client import get_client, TokenUsage
from src.schemas.agent import AgentThought, ToolCall
from pydantic import BaseModel, Field


# ---------- Test 1: plain chat round-trip ----------
def test_plain_chat() -> None:
    print("=" * 60)
    print("TEST 1: Plain chat round-trip")
    print("=" * 60)
    client = get_client()
    text, usage = client.chat([{"role": "user", "content": "Say hello in one sentence."}])
    print(f"  Response: {text!r}")
    print(f"  Tokens:   input={usage.input_tokens}, output={usage.output_tokens}")
    assert isinstance(text, str) and len(text) > 0, "Empty response"
    assert usage.input_tokens > 0, "input_tokens is zero — token accounting broken"
    assert usage.output_tokens > 0, "output_tokens is zero — token accounting broken"
    print("  ✓ PASSED\n")


# ---------- Test 2: schema-constrained AgentThought + ToolCall ----------
class AgentResponse(BaseModel):
    """Combined thought + tool call — what the agent loop expects."""

    thought: AgentThought
    tool_call: ToolCall


def test_schema_constrained() -> None:
    print("=" * 60)
    print("TEST 2: Schema-constrained output (AgentThought + ToolCall)")
    print("=" * 60)
    client = get_client()

    prompt = (
        "You are a deployment engineer. The user wants a small PostgreSQL + "
        "nginx + Redis stack for 5 developers on 8GB RAM.\n\n"
        "Your available tools are:\n"
        "- query_rag(question: str, service: str | null) — search docs\n"
        "- generate_config(partial_spec: dict) — generate config files\n"
        "- validate_config(spec: dict) — deploy and test\n"
        "- finalise(final_spec: dict, reason: str) — finish\n\n"
        "Decide your FIRST action. Respond with JSON only."
    )

    messages = [{"role": "user", "content": prompt}]
    result, usage = client.chat(messages, schema=AgentResponse)
    print(f"  Thought:    {result.thought.reasoning[:80]}...")
    print(f"  Action:     {result.thought.planned_next_action}")
    print(f"  Tool call:  {result.tool_call.name}({result.tool_call.args})")
    print(f"  Tokens:     input={usage.input_tokens}, output={usage.output_tokens}")
    assert isinstance(result, AgentResponse), f"Wrong type: {type(result)}"
    assert result.tool_call.name in {
        "query_rag",
        "generate_config",
        "validate_config",
        "finalise",
    }, f"Unexpected tool: {result.tool_call.name}"
    print("  ✓ PASSED\n")


if __name__ == "__main__":
    try:
        test_plain_chat()
        test_schema_constrained()
        print("=" * 60)
        print("ALL PHASE B SANITY CHECKS PASSED")
        print("=" * 60)
    except Exception as e:
        print(f"\n  ✗ FAILED: {e}")
        sys.exit(1)
