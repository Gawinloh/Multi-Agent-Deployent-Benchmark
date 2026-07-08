"""Backend-agnostic LLM client abstraction.

Every LLM call in the project goes through an :class:`LLMClient` so that
backends (Ollama local, Anthropic, OpenAI, Google Gemini) can be swapped
via the ``LLM_BACKEND`` env var without touching agent code, and so token
usage can be enforced through :class:`BudgetEnforcer`.

Structured output strategy:
- Anthropic / OpenAI: Instructor (constrained decoding via tool use).
- Ollama / Gemini: schema embedded as a system-prompt instruction; JSON
  parsed manually and validated with Pydantic, retrying up to
  ``MAX_SCHEMA_RETRIES`` times with the validation error as feedback.

SDK imports are deferred to client construction so unit tests can mock
the SDKs and so unused backends need not be installed.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from src.llm.token_budget import TokenBudget

logger = structlog.get_logger(__name__)

Message = dict[str, str]
ChatResult = tuple[str | BaseModel, "TokenUsage"]

#: Maximum number of repair attempts after a Pydantic validation failure.
MAX_SCHEMA_RETRIES = 3


@dataclass(frozen=True)
class TokenUsage:
    """Token consumption of a single LLM call."""

    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class SchemaParseError(Exception):
    """Raised when the LLM output cannot be validated against the schema
    after ``MAX_SCHEMA_RETRIES`` repair attempts."""


class LLMClient(ABC):
    """Abstract chat interface shared by all backends."""

    backend_name: str = "abstract"

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        schema: type[BaseModel] | None = None,
    ) -> ChatResult:
        """Send a chat conversation and return (response, usage).

        Args:
            messages: OpenAI-style messages
                (``[{"role": "user", "content": "..."}]``).
            schema: optional Pydantic model. When given, the response is
                a validated instance of that model instead of raw text.

        Returns:
            Tuple of (text or schema instance, :class:`TokenUsage`).
        """


def _split_system(messages: list[Message]) -> tuple[str | None, list[Message]]:
    """Separate system messages from the rest (Anthropic/Gemini style)."""
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    return ("\n\n".join(system_parts) or None, rest)


def _fix_json_quirks(text: str) -> str:
    """Best-effort repair of common LLM JSON mistakes.

    Fixes applied (order matters):
    1. Strip single-line ``// …`` and ``/* … */`` comments.
    2. Remove trailing commas before ``}`` or ``]``.
    3. Replace Python-style ``True/False/None`` with JSON equivalents.
    """
    # 1. Comments (single-line // and block /* */)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # 2. Trailing commas:  ,  }  or  ,  ]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # 3. Python booleans / None  (whole-word, inside JSON values)
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b", "null", text)
    return text


def _extract_json(text: str) -> str:
    """Pull a JSON object out of an LLM response.

    Handles bare JSON, ```json fenced blocks, and JSON embedded in prose
    (first ``{`` to last ``}``). Applies ``_fix_json_quirks`` to repair
    trailing commas, comments, and Python literals before returning.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return _fix_json_quirks(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return _fix_json_quirks(text[start : end + 1])
    return text


def _schema_instruction(schema: type[BaseModel]) -> str:
    """System-prompt instruction for backends without constrained decoding."""
    return (
        "You must respond with a single JSON object and nothing else - no "
        "prose, no markdown fences. The JSON must validate against this "
        f"JSON Schema:\n{json.dumps(schema.model_json_schema(), indent=2)}"
    )


def _parse_with_retries(
    generate: Callable[[list[Message]], tuple[str, TokenUsage]],
    messages: list[Message],
    schema: type[BaseModel],
    backend: str,
) -> tuple[BaseModel, TokenUsage]:
    """Call ``generate``, parse JSON, validate; retry with error feedback.

    Used by the Ollama and Gemini backends, which lack native constrained
    decoding. Cumulative usage across attempts is returned so the budget
    sees the true cost.
    """
    total_usage = TokenUsage(0, 0)
    convo = list(messages)
    last_error: Exception | None = None
    for attempt in range(1 + MAX_SCHEMA_RETRIES):
        text, usage = generate(convo)
        total_usage = total_usage + usage
        try:
            parsed = schema.model_validate_json(_extract_json(text))
            return parsed, total_usage
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning(
                "schema_validation_failed",
                backend=backend,
                attempt=attempt + 1,
                error=str(exc),
            )
            convo = convo + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "Your previous response failed validation with this "
                        f"error:\n{exc}\nRespond again with a corrected JSON "
                        "object only."
                    ),
                },
            ]
    raise SchemaParseError(
        f"{backend}: output failed schema validation after "
        f"{MAX_SCHEMA_RETRIES} retries: {last_error}"
    )


class OllamaClient(LLMClient):
    """Local models via Ollama. Reads OLLAMA_HOST and OLLAMA_MODEL."""

    backend_name = "ollama"

    def __init__(self, host: str | None = None, model: str | None = None) -> None:
        import ollama

        self._host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self._model = model or os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
        self._client = ollama.Client(host=self._host)

    def _generate(
        self,
        messages: list[Message],
        format: dict[str, Any] | str | None = None,
    ) -> tuple[str, TokenUsage]:
        kwargs: dict[str, Any] = {}
        if format is not None:
            kwargs["format"] = format
        response = self._client.chat(
            model=self._model, messages=messages, **kwargs
        )
        usage = TokenUsage(
            input_tokens=response.get("prompt_eval_count", 0) or 0,
            output_tokens=response.get("eval_count", 0) or 0,
        )
        return response["message"]["content"], usage

    def chat(
        self, messages: list[Message], schema: type[BaseModel] | None = None
    ) -> ChatResult:
        if schema is None:
            return self._generate(messages)
        json_schema = schema.model_json_schema()
        prompted = [{"role": "system", "content": _schema_instruction(schema)}, *messages]
        return _parse_with_retries(
            lambda msgs: self._generate(msgs, format=json_schema),
            prompted,
            schema,
            self.backend_name,
        )


class AnthropicClient(LLMClient):
    """Anthropic API. Reads ANTHROPIC_API_KEY and ANTHROPIC_MODEL."""

    backend_name = "anthropic"

    def __init__(self, model: str | None = None, max_tokens: int = 4096) -> None:
        import anthropic
        import instructor

        self._model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        self._max_tokens = max_tokens
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self._instructor = instructor.from_anthropic(self._client)

    def chat(
        self, messages: list[Message], schema: type[BaseModel] | None = None
    ) -> ChatResult:
        system, rest = _split_system(messages)
        if schema is None:
            kwargs: dict[str, Any] = {}
            if system:
                kwargs["system"] = system
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=rest,
                **kwargs,
            )
            usage = TokenUsage(response.usage.input_tokens, response.usage.output_tokens)
            return response.content[0].text, usage

        kwargs = {}
        if system:
            kwargs["system"] = system
        parsed, completion = self._instructor.messages.create_with_completion(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=rest,
            response_model=schema,
            max_retries=MAX_SCHEMA_RETRIES,
            **kwargs,
        )
        usage = TokenUsage(completion.usage.input_tokens, completion.usage.output_tokens)
        return parsed, usage


class OpenAIClient(LLMClient):
    """OpenAI API. Reads OPENAI_API_KEY and OPENAI_MODEL."""

    backend_name = "openai"

    def __init__(self, model: str | None = None) -> None:
        import instructor
        import openai

        self._model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self._client = openai.OpenAI()  # reads OPENAI_API_KEY
        self._instructor = instructor.from_openai(self._client)

    def chat(
        self, messages: list[Message], schema: type[BaseModel] | None = None
    ) -> ChatResult:
        if schema is None:
            response = self._client.chat.completions.create(
                model=self._model, messages=messages
            )
            usage = TokenUsage(
                response.usage.prompt_tokens, response.usage.completion_tokens
            )
            return response.choices[0].message.content, usage

        parsed, completion = self._instructor.chat.completions.create_with_completion(
            model=self._model,
            messages=messages,
            response_model=schema,
            max_retries=MAX_SCHEMA_RETRIES,
        )
        usage = TokenUsage(
            completion.usage.prompt_tokens, completion.usage.completion_tokens
        )
        return parsed, usage


class GeminiClient(LLMClient):
    """Google Gemini. Reads GOOGLE_API_KEY and GEMINI_MODEL."""

    backend_name = "gemini"

    def __init__(self, model: str | None = None) -> None:
        import google.generativeai as genai

        genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
        self._model_name = model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        self._genai = genai

    @staticmethod
    def _to_gemini_format(
        messages: list[Message],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system, rest = _split_system(messages)
        contents = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [m["content"]],
            }
            for m in rest
        ]
        return system, contents

    def _generate(self, messages: list[Message]) -> tuple[str, TokenUsage]:
        system, contents = self._to_gemini_format(messages)
        model = self._genai.GenerativeModel(
            self._model_name, system_instruction=system
        )
        response = model.generate_content(contents)
        meta = response.usage_metadata
        usage = TokenUsage(
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        )
        return response.text, usage

    def chat(
        self, messages: list[Message], schema: type[BaseModel] | None = None
    ) -> ChatResult:
        if schema is None:
            return self._generate(messages)
        prompted = [{"role": "system", "content": _schema_instruction(schema)}, *messages]
        return _parse_with_retries(self._generate, prompted, schema, self.backend_name)


_BACKENDS: dict[str, type[LLMClient]] = {
    "ollama": OllamaClient,
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "gemini": GeminiClient,
    "google": GeminiClient,
}


def get_client(backend: str | None = None, **kwargs: Any) -> LLMClient:
    """Factory. ``backend`` falls back to the LLM_BACKEND env var,
    defaulting to "ollama" (local development)."""
    name = (backend or os.environ.get("LLM_BACKEND", "ollama")).lower()
    if name not in _BACKENDS:
        raise ValueError(
            f"Unknown LLM backend {name!r}; expected one of {sorted(_BACKENDS)}"
        )
    logger.info("llm_client_created", backend=name)
    return _BACKENDS[name](**kwargs)


class BudgetEnforcer:
    """Wraps an :class:`LLMClient` so every call debits a :class:`TokenBudget`.

    Refuses to start a call if the budget is already exhausted, and
    propagates :class:`~src.llm.token_budget.BudgetExhausted` raised when
    a call's recorded usage tips the budget over its limit.

    Usable as a context manager purely for readability::

        with BudgetEnforcer(client, budget) as llm:
            text, usage = llm.chat(messages)
    """

    def __init__(self, client: LLMClient, budget: TokenBudget) -> None:
        self._client = client
        self._budget = budget

    @property
    def budget(self) -> TokenBudget:
        return self._budget

    def chat(
        self, messages: list[Message], schema: type[BaseModel] | None = None
    ) -> ChatResult:
        if self._budget.is_exhausted():
            summary = self._budget.summary()
            from src.llm.token_budget import BudgetExhausted

            raise BudgetExhausted(limit=summary["limit"], used=summary["total_used"])
        response, usage = self._client.chat(messages, schema=schema)
        # record_usage raises BudgetExhausted if this call overran the budget.
        self._budget.record_usage(usage.input_tokens, usage.output_tokens)
        return response, usage

    def __enter__(self) -> BudgetEnforcer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        logger.info("budget_enforcer_exit", **self._budget.summary())
