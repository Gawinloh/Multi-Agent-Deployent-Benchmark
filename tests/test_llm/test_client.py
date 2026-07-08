"""Unit tests for src.llm.client.

The underlying SDKs (ollama, anthropic, openai, instructor,
google-generativeai) are mocked via sys.modules injection, so these
tests run without any SDK installed and without network access.
"""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from src.llm.client import (
    BudgetEnforcer,
    SchemaParseError,
    TokenUsage,
    _escape_control_chars,
    _extract_json,
    get_client,
)
from src.llm.token_budget import BudgetExhausted, TokenBudget


class Answer(BaseModel):
    """Toy schema used in structured-output tests."""

    city: str
    population: int


# ---------------------------------------------------------------------------
# Fake SDK fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ollama(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    module = ModuleType("ollama")
    client = MagicMock(name="ollama.Client()")
    module.Client = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "ollama", module)
    return client


def _ollama_response(content: str, prompt: int = 10, evals: int = 5) -> dict:
    return {
        "message": {"content": content},
        "prompt_eval_count": prompt,
        "eval_count": evals,
    }


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    anthropic_mod = ModuleType("anthropic")
    sdk_client = MagicMock(name="anthropic.Anthropic()")
    anthropic_mod.Anthropic = MagicMock(return_value=sdk_client)

    instructor_mod = ModuleType("instructor")
    instructor_client = MagicMock(name="instructor.from_anthropic()")
    instructor_mod.from_anthropic = MagicMock(return_value=instructor_client)
    instructor_mod.from_openai = MagicMock()

    monkeypatch.setitem(sys.modules, "anthropic", anthropic_mod)
    monkeypatch.setitem(sys.modules, "instructor", instructor_mod)
    return sdk_client, instructor_client


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    openai_mod = ModuleType("openai")
    sdk_client = MagicMock(name="openai.OpenAI()")
    openai_mod.OpenAI = MagicMock(return_value=sdk_client)

    instructor_mod = ModuleType("instructor")
    instructor_client = MagicMock(name="instructor.from_openai()")
    instructor_mod.from_openai = MagicMock(return_value=instructor_client)
    instructor_mod.from_anthropic = MagicMock()

    monkeypatch.setitem(sys.modules, "openai", openai_mod)
    monkeypatch.setitem(sys.modules, "instructor", instructor_mod)
    return sdk_client, instructor_client


@pytest.fixture
def fake_gemini(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    google_mod = ModuleType("google")
    genai_mod = ModuleType("google.generativeai")
    genai_mod.configure = MagicMock()
    genai_mod.GenerativeModel = MagicMock()
    google_mod.generativeai = genai_mod
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.generativeai", genai_mod)
    return genai_mod


def _gemini_response(text: str, prompt: int = 7, candidates: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt, candidates_token_count=candidates
        ),
    )


MESSAGES = [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------------
# JSON repair helpers
# ---------------------------------------------------------------------------


class TestEscapeControlChars:
    """Unit tests for _escape_control_chars."""

    def test_escapes_bare_newline_inside_string(self) -> None:
        # Model emits a bare \n inside a JSON string value.
        raw = '{"thought": "step one\nstep two"}'
        result = _escape_control_chars(raw)
        assert result == '{"thought": "step one\\u000astep two"}'

    def test_preserves_structural_newline_between_tokens(self) -> None:
        # Structural \n between JSON tokens — should NOT be touched.
        raw = '{"a": 1}\n{"b": 2}'
        result = _escape_control_chars(raw)
        assert result == raw  # unchanged

    def test_skip_newlines_leaves_bare_newlines_alone(self) -> None:
        raw = '{"thought": "step one\nstep two"}'
        result = _escape_control_chars(raw, skip_newlines=True)
        assert result == raw  # newline left bare

    def test_escapes_nul_and_bel_regardless(self) -> None:
        raw = '{"x": "a\x00b\x07c"}'
        result = _escape_control_chars(raw, skip_newlines=True)
        assert "\\u0000" in result
        assert "\\u0007" in result


class TestExtractJsonTwoPass:
    """The two-pass strategy in _extract_json."""

    def test_bare_newline_in_string_is_fixed(self) -> None:
        # Pass 1 should handle this: escape the \n inside the string.
        raw = '{"thought": "line one\nline two", "val": 1}'
        result = _extract_json(raw)
        import json
        parsed = json.loads(result)
        assert parsed["thought"] == "line one\nline two"
        assert parsed["val"] == 1

    def test_structural_newlines_preserved_in_valid_json(self) -> None:
        # Valid JSON with structural newlines between tokens must survive
        # the two-pass pipeline intact.
        raw = '{\n  "thought": "hello",\n  "value": 42\n}'
        result = _extract_json(raw)
        import json
        parsed = json.loads(result)
        assert parsed == {"thought": "hello", "value": 42}

    def test_fallback_when_pass1_fails(self) -> None:
        # If pass-1 (full escaping) produces invalid JSON but pass-2
        # (skip_newlines) produces valid JSON, the fallback should kick in.
        # We simulate this by patching _fix_json_quirks to fail on the
        # first call and succeed on the second.
        import json
        from unittest.mock import patch, call

        valid_json = '{"a": 1}'
        broken = '{broken'
        call_count = 0

        def mock_fix(text, *, skip_newlines=False):
            nonlocal call_count
            call_count += 1
            return broken if call_count == 1 else valid_json

        with patch("src.llm.client._fix_json_quirks", side_effect=mock_fix):
            result = _extract_json('{"a": 1}')

        assert json.loads(result) == {"a": 1}
        assert call_count == 2  # both passes were tried


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


class TestOllamaClient:
    def test_plain_chat(self, fake_ollama: MagicMock) -> None:
        fake_ollama.chat.return_value = _ollama_response("hi there", 12, 8)
        client = get_client("ollama", model="test-model")

        text, usage = client.chat(MESSAGES)

        assert text == "hi there"
        assert usage == TokenUsage(12, 8)
        fake_ollama.chat.assert_called_once_with(model="test-model", messages=MESSAGES)

    def test_schema_valid_first_try(self, fake_ollama: MagicMock) -> None:
        fake_ollama.chat.return_value = _ollama_response(
            '{"city": "Coventry", "population": 345000}'
        )
        client = get_client("ollama")

        result, usage = client.chat(MESSAGES, schema=Answer)

        assert isinstance(result, Answer)
        assert result.city == "Coventry"
        # Schema instruction must be injected as a system message.
        sent = fake_ollama.chat.call_args.kwargs["messages"]
        assert sent[0]["role"] == "system"
        assert "JSON Schema" in sent[0]["content"]

    def test_schema_retry_with_error_feedback(self, fake_ollama: MagicMock) -> None:
        fake_ollama.chat.side_effect = [
            _ollama_response('{"city": "Coventry"}'),  # missing population
            _ollama_response('{"city": "Coventry", "population": 345000}'),
        ]
        client = get_client("ollama")

        result, usage = client.chat(MESSAGES, schema=Answer)

        assert isinstance(result, Answer)
        assert fake_ollama.chat.call_count == 2
        # Usage from both attempts accumulates (10+5 each).
        assert usage == TokenUsage(20, 10)
        # The retry conversation must include the validation error.
        retry_messages = fake_ollama.chat.call_args.kwargs["messages"]
        assert any("failed validation" in m["content"] for m in retry_messages)

    def test_schema_fails_after_max_retries(self, fake_ollama: MagicMock) -> None:
        fake_ollama.chat.return_value = _ollama_response("not json at all")
        client = get_client("ollama")

        with pytest.raises(SchemaParseError):
            client.chat(MESSAGES, schema=Answer)

        assert fake_ollama.chat.call_count == 4  # 1 initial + 3 retries

    def test_markdown_fenced_json_is_parsed(self, fake_ollama: MagicMock) -> None:
        fake_ollama.chat.return_value = _ollama_response(
            'Here you go:\n```json\n{"city": "Leeds", "population": 800000}\n```'
        )
        client = get_client("ollama")

        result, _ = client.chat(MESSAGES, schema=Answer)

        assert isinstance(result, Answer)
        assert result.city == "Leeds"


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class TestAnthropicClient:
    def test_plain_chat_splits_system(
        self, fake_anthropic: tuple[MagicMock, MagicMock]
    ) -> None:
        sdk, _ = fake_anthropic
        sdk.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="hello back")],
            usage=SimpleNamespace(input_tokens=20, output_tokens=9),
        )
        client = get_client("anthropic", model="test-model")

        messages = [{"role": "system", "content": "be brief"}, *MESSAGES]
        text, usage = client.chat(messages)

        assert text == "hello back"
        assert usage == TokenUsage(20, 9)
        kwargs = sdk.messages.create.call_args.kwargs
        assert kwargs["system"] == "be brief"
        assert kwargs["messages"] == MESSAGES  # system removed from list

    def test_schema_uses_instructor(
        self, fake_anthropic: tuple[MagicMock, MagicMock]
    ) -> None:
        _, instructor_client = fake_anthropic
        parsed = Answer(city="Coventry", population=345000)
        completion = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=30, output_tokens=15)
        )
        instructor_client.messages.create_with_completion.return_value = (
            parsed,
            completion,
        )
        client = get_client("anthropic")

        result, usage = client.chat(MESSAGES, schema=Answer)

        assert result is parsed
        assert usage == TokenUsage(30, 15)
        kwargs = instructor_client.messages.create_with_completion.call_args.kwargs
        assert kwargs["response_model"] is Answer
        assert kwargs["max_retries"] == 3


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class TestOpenAIClient:
    def test_plain_chat(self, fake_openai: tuple[MagicMock, MagicMock]) -> None:
        sdk, _ = fake_openai
        sdk.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=6),
        )
        client = get_client("openai", model="test-model")

        text, usage = client.chat(MESSAGES)

        assert text == "hi"
        assert usage == TokenUsage(11, 6)

    def test_schema_uses_instructor(
        self, fake_openai: tuple[MagicMock, MagicMock]
    ) -> None:
        _, instructor_client = fake_openai
        parsed = Answer(city="York", population=200000)
        completion = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=25, completion_tokens=12)
        )
        instructor_client.chat.completions.create_with_completion.return_value = (
            parsed,
            completion,
        )
        client = get_client("openai")

        result, usage = client.chat(MESSAGES, schema=Answer)

        assert result is parsed
        assert usage == TokenUsage(25, 12)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


class TestGeminiClient:
    def test_plain_chat(self, fake_gemini: MagicMock) -> None:
        model = MagicMock()
        model.generate_content.return_value = _gemini_response("hello", 7, 3)
        fake_gemini.GenerativeModel.return_value = model
        client = get_client("gemini", model="test-model")

        text, usage = client.chat(
            [{"role": "system", "content": "be brief"}, *MESSAGES]
        )

        assert text == "hello"
        assert usage == TokenUsage(7, 3)
        # System message becomes system_instruction.
        _, kwargs = fake_gemini.GenerativeModel.call_args
        assert kwargs["system_instruction"] == "be brief"

    def test_schema_parses_json(self, fake_gemini: MagicMock) -> None:
        model = MagicMock()
        model.generate_content.return_value = _gemini_response(
            '{"city": "Bath", "population": 90000}'
        )
        fake_gemini.GenerativeModel.return_value = model
        client = get_client("gemini")

        result, _ = client.chat(MESSAGES, schema=Answer)

        assert isinstance(result, Answer)
        assert result.city == "Bath"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown LLM backend"):
            get_client("not-a-backend")

    def test_reads_env_var(
        self, monkeypatch: pytest.MonkeyPatch, fake_ollama: MagicMock
    ) -> None:
        monkeypatch.setenv("LLM_BACKEND", "ollama")
        client = get_client()
        assert client.backend_name == "ollama"

    def test_explicit_backend_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch, fake_ollama: MagicMock
    ) -> None:
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        client = get_client("ollama")
        assert client.backend_name == "ollama"


# ---------------------------------------------------------------------------
# BudgetEnforcer
# ---------------------------------------------------------------------------


class TestBudgetEnforcer:
    def _client_returning(self, usage: TokenUsage) -> MagicMock:
        client = MagicMock()
        client.chat.return_value = ("ok", usage)
        return client

    def test_records_usage(self) -> None:
        budget = TokenBudget(1000)
        enforcer = BudgetEnforcer(self._client_returning(TokenUsage(100, 50)), budget)

        text, usage = enforcer.chat(MESSAGES)

        assert text == "ok"
        assert budget.remaining() == 850

    def test_raises_when_call_overruns_budget(self) -> None:
        budget = TokenBudget(100)
        enforcer = BudgetEnforcer(self._client_returning(TokenUsage(80, 80)), budget)

        with pytest.raises(BudgetExhausted):
            enforcer.chat(MESSAGES)

    def test_refuses_call_when_already_exhausted(self) -> None:
        budget = TokenBudget(100)
        budget.record_usage(50, 50)  # exactly exhausted, no raise
        client = self._client_returning(TokenUsage(1, 1))
        enforcer = BudgetEnforcer(client, budget)

        with pytest.raises(BudgetExhausted):
            enforcer.chat(MESSAGES)

        client.chat.assert_not_called()  # never reached the LLM

    def test_usable_as_context_manager(self) -> None:
        budget = TokenBudget(1000)
        with BudgetEnforcer(self._client_returning(TokenUsage(10, 10)), budget) as llm:
            llm.chat(MESSAGES)
        assert budget.remaining() == 980
