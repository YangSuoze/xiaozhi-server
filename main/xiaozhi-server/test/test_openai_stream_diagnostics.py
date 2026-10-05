"""Verify empty LLM streams leave enough evidence without recording response text."""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch


module = importlib.import_module("core.providers.llm.openai.openai")


class _Logger:
    def __init__(self):
        self.records = []

    def bind(self, **_kwargs):
        return self

    def info(self, message):
        self.records.append(("info", message))

    def warning(self, message):
        self.records.append(("warning", message))

    def error(self, message):
        self.records.append(("error", message))


class OpenAIStreamDiagnosticsTests(unittest.TestCase):
    def test_reasoning_only_stream_logs_finish_reason_and_empty_output(self):
        chunk = SimpleNamespace(
            id="completion-123",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoning_content="thinking"),
                    finish_reason="length",
                )
            ],
        )
        provider = module.LLMProvider.__new__(module.LLMProvider)
        provider.model_name = "test-model"
        provider.max_tokens = 1200
        provider.temperature = None
        provider.top_p = None
        provider.frequency_penalty = None
        provider.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: iter([chunk]))
            )
        )
        logger = _Logger()

        with patch.object(module, "logger", logger):
            result = list(
                provider.response(
                    "session", [{"role": "user", "content": "example"}], trace_id="abc-0"
                )
            )

        self.assertEqual(result, [])
        warnings = [message for level, message in logger.records if level == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("trace=abc-0", warnings[0])
        self.assertIn("response_id=completion-123", warnings[0])
        self.assertIn("finish_reason=length", warnings[0])
        self.assertIn("visible_chars=0", warnings[0])
        self.assertIn("reasoning_chars=8", warnings[0])
        self.assertNotIn("thinking", warnings[0])


if __name__ == "__main__":
    unittest.main()
