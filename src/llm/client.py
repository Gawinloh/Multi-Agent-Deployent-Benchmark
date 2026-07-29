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

    @property
    def model_name(self) -> str:
        """Model identifier actually sent to the backend.

        Each concrete client resolves this once at construction time from
        its explicit constructor argument or its backend env var
        (``OLLAMA_MODEL``, ``ANTHROPIC_MODEL``, ...).  Recording it gives
        run provenance the model that was really used rather than
        whatever placeholder the CLI was invoked with.
        """
        return getattr(self, "_model", None) or "unknown"

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


def _fix_json_quirks(text: str, *, skip_newlines: bool = False) -> str:
    """Best-effort repair of common LLM JSON mistakes.

    Fixes applied (order matters):
    1. Strip single-line ``// …`` and ``/* … */`` comments.
    2. Remove trailing commas before ``}`` or ``]``.
    3. Replace Python-style ``True/False/None`` with JSON equivalents.
    4. Escape control characters (see ``_escape_control_chars``).

    *skip_newlines* is forwarded to ``_escape_control_chars``.
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
    # 4. Escape literal control characters inside JSON strings.
    #    Small models often emit raw \n/\t/\r inside string values
    #    instead of the escaped \\n/\\t/\\r that JSON requires.
    #    The skip_newlines flag is threaded through from _extract_json's
    #    two-pass strategy.
    text = _escape_control_chars(text, skip_newlines=skip_newlines)
    return text


def _escape_control_chars(text: str, *, skip_newlines: bool = False) -> str:
    """Replace bare control characters inside JSON string values.

    Walks the text tracking whether we are inside a ``"``-delimited JSON
    string and escapes control chars (U+0000–U+001F) found there.

    When *skip_newlines* is ``True``, ``\\n``, ``\\r``, and ``\\t`` are
    left alone.  This is the fallback used when the in-string tracker
    mis-fires due to unbalanced quotes from the LLM — escaping
    structural whitespace in that situation corrupts the JSON worse than
    leaving bare newlines inside strings.
    """
    skip: set[str] = {"\n", "\r", "\t"} if skip_newlines else set()
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_string:
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ord(ch) < 0x20 and ch not in skip:
            out.append(f"\\u{ord(ch):04x}")
            continue
        out.append(ch)
    return "".join(out)


def _extract_json(text: str) -> str:
    """Pull a JSON object out of an LLM response.

    Handles bare JSON, ```json fenced blocks, and JSON embedded in prose
    (first ``{`` to last ``}``). Applies ``_fix_json_quirks`` to repair
    trailing commas, comments, and Python literals before returning.

    Uses a two-pass strategy for control-character escaping:

    1. **Pass 1** — escape *all* control chars including ``\\n``/``\\r``/
       ``\\t``.  This is correct when the model emits bare newlines
       inside JSON string values.
    2. **Pass 2** (fallback) — if pass 1 produces invalid JSON, retry
       with ``skip_newlines=True``.  This handles the case where the
       in-string tracker mis-fires on unbalanced quotes and incorrectly
       escapes structural whitespace between JSON tokens.

    If both passes fail, return the pass-1 result so downstream code
    sees the more-commonly-correct version.
    """
    # --- extract raw JSON substring ---
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            raw = text[start : end + 1]
        else:
            return text

    # --- pass 1: full escaping (handles bare \n inside strings) ---
    candidate = _fix_json_quirks(raw)
    try:
        json.loads(candidate)
        return candidate
    except (json.JSONDecodeError, ValueError):
        pass

    # --- pass 2: skip newline escaping (handles tracker misfires) ---
    candidate_no_nl = _fix_json_quirks(raw, skip_newlines=True)
    try:
        json.loads(candidate_no_nl)
        return candidate_no_nl
    except (json.JSONDecodeError, ValueError):
        pass

    # Both failed; return pass-1 (more commonly correct).
    return candidate


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


class _StructuredAttemptFailed(Exception):
    """One constrained-decoding attempt failed schema validation.

    Carries that attempt's token usage so the caller can bill it even
    though the attempt produced nothing usable.
    """

    def __init__(self, error: Exception, usage: TokenUsage) -> None:
        super().__init__(str(error))
        self.error = error
        self.usage = usage


def _is_schema_failure(exc: Exception) -> bool:
    """Whether *exc* means "the model's output did not validate".

    Only these are worth retrying. Transport and auth errors must
    propagate rather than burn the retry budget, and Instructor's
    exception types are matched by name because the SDK is imported
    lazily and may be absent.
    """
    if isinstance(exc, ValidationError):
        return True
    return type(exc).__name__ in {
        "InstructorRetryException",
        "IncompleteOutputException",
    }


def _usage_of_failed_attempt(
    exc: Exception, extract: Callable[[Any], TokenUsage]
) -> TokenUsage:
    """Best-effort usage for an attempt that raised.

    Instructor attaches the raw completion to its retry exception. When
    it is absent or shaped unexpectedly the attempt is billed as zero,
    which under-counts rather than inventing tokens.
    """
    completion = getattr(exc, "last_completion", None)
    if completion is None:
        return TokenUsage(0, 0)
    try:
        return extract(completion)
    except (AttributeError, TypeError):
        return TokenUsage(0, 0)


def _retry_structured(
    attempt: Callable[[list[Message]], tuple[BaseModel, TokenUsage]],
    messages: list[Message],
    backend: str,
) -> tuple[BaseModel, TokenUsage]:
    """Retry a constrained-decoding call, accumulating usage per attempt.

    Instructor can retry internally, but ``create_with_completion``
    returns only the *final* completion, so internally-retried attempts
    are invisible: their tokens are spent at the provider but never
    reported, never debited from :class:`TokenBudget`, and never written
    to the run JSON. Backends therefore call Instructor with
    ``max_retries=1`` and retry here instead.

    This mirrors :func:`_parse_with_retries` — same attempt count, same
    cumulative usage, same terminal exception — so that all four
    backends account for tokens identically and H3 comparisons across
    models remain valid.
    """
    total_usage = TokenUsage(0, 0)
    convo = list(messages)
    last_error: Exception | None = None
    for attempt_number in range(1 + MAX_SCHEMA_RETRIES):
        try:
            parsed, usage = attempt(convo)
        except _StructuredAttemptFailed as failure:
            total_usage = total_usage + failure.usage
            last_error = failure.error
            logger.warning(
                "schema_validation_failed",
                backend=backend,
                attempt=attempt_number + 1,
                error=str(failure.error),
            )
            convo = convo + [
                {
                    "role": "user",
                    "content": (
                        "Your previous response failed validation with this "
                        f"error:\n{failure.error}\nRespond again with a "
                        "corrected object only."
                    ),
                }
            ]
            continue
        return parsed, total_usage + usage
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
        try:
            response = self._client.chat(
                model=self._model, messages=messages, **kwargs
            )
        except Exception as exc:
            # Ollama's grammar parser can't handle deeply-nested JSON
            # schemas (e.g. StackSpec with $defs/$ref).  Fall back to
            # basic JSON mode — _parse_with_retries still validates.
            if isinstance(format, dict) and "failed to parse grammar" in str(exc):
                logger.warning(
                    "ollama_grammar_fallback",
                    error=str(exc)[:120],
                )
                response = self._client.chat(
                    model=self._model, messages=messages, format="json"
                )
            else:
                raise
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

        def attempt(convo: list[Message]) -> tuple[BaseModel, TokenUsage]:
            convo_system, convo_rest = _split_system(convo)
            call_kwargs: dict[str, Any] = {}
            if convo_system:
                call_kwargs["system"] = convo_system
            try:
                parsed, completion = self._instructor.messages.create_with_completion(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    messages=convo_rest,
                    response_model=schema,
                    # Retries are driven by _retry_structured, not
                    # Instructor, so that every attempt's usage is billed.
                    max_retries=1,
                    **call_kwargs,
                )
            except Exception as exc:
                if not _is_schema_failure(exc):
                    raise
                raise _StructuredAttemptFailed(
                    exc, _usage_of_failed_attempt(exc, self._usage_of)
                ) from exc
            return parsed, self._usage_of(completion)

        return _retry_structured(attempt, messages, self.backend_name)

    @staticmethod
    def _usage_of(completion: Any) -> TokenUsage:
        return TokenUsage(completion.usage.input_tokens, completion.usage.output_tokens)


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

        def attempt(convo: list[Message]) -> tuple[BaseModel, TokenUsage]:
            try:
                parsed, completion = (
                    self._instructor.chat.completions.create_with_completion(
                        model=self._model,
                        messages=convo,
                        response_model=schema,
                        # Retries are driven by _retry_structured, not
                        # Instructor, so that every attempt's usage is billed.
                        max_retries=1,
                    )
                )
            except Exception as exc:
                if not _is_schema_failure(exc):
                    raise
                raise _StructuredAttemptFailed(
                    exc, _usage_of_failed_attempt(exc, self._usage_of)
                ) from exc
            return parsed, self._usage_of(completion)

        return _retry_structured(attempt, messages, self.backend_name)

    @staticmethod
    def _usage_of(completion: Any) -> TokenUsage:
        return TokenUsage(
            completion.usage.prompt_tokens, completion.usage.completion_tokens
        )


class GeminiClient(LLMClient):
    """Google Gemini. Reads GOOGLE_API_KEY and GEMINI_MODEL."""

    backend_name = "gemini"

    def __init__(self, model: str | None = None) -> None:
        import google.generativeai as genai

        genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
        self._model_name = model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        self._genai = genai

    @property
    def model_name(self) -> str:
        # Gemini stores the identifier under a different attribute name,
        # so the base-class fallback does not apply here.
        return self._model_name

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
