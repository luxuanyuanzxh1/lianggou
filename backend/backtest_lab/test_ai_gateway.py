from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from quant_service.ai_gateway import AiGatewayError, AiSettings, _call_provider, _resolve_ollama_model, chat, external_sources, load_settings, status_payload


class AiGatewayTests(unittest.TestCase):
    def test_openai_compatible_waits_for_key_without_exposing_one(self) -> None:
        values = {
            "QUANTWEAVE_AI_PROVIDER": "openai_compatible",
            "QUANTWEAVE_AI_BASE_URL": "https://api.example.com/v1",
            "QUANTWEAVE_AI_MODEL": "test-model",
            "QUANTWEAVE_AI_API_KEY": "",
        }
        with patch.dict(os.environ, values, clear=False):
            settings = load_settings()
            status = status_payload()
        self.assertEqual(settings.provider, "openai_compatible")
        self.assertEqual(status["status"], "waiting-key")
        self.assertFalse(status["api_key_configured"])
        self.assertNotIn("api_key", status)

    def test_external_remote_source_requires_https(self) -> None:
        value = '[{"id":"bad","name":"bad","url":"http://example.com/data"}]'
        with patch.dict(os.environ, {"QUANTWEAVE_EXTERNAL_DATA_SOURCES": value}, clear=False):
            with self.assertRaises(AiGatewayError):
                external_sources()

    def test_ollama_uses_first_installed_model_when_default_is_not_installed(self) -> None:
        values = {"QUANTWEAVE_AI_PROVIDER": "ollama"}
        with patch.dict(os.environ, values, clear=True):
            settings = load_settings()
            selected = _resolve_ollama_model(settings, ["deepseek-v3.1:671b-cloud", "qwen3-vl:8b"])
        self.assertEqual(selected, "qwen3-vl:8b")

    def test_ollama_requests_final_answer_without_reasoning_trace(self) -> None:
        settings = AiSettings("ollama", "http://127.0.0.1:11434", "qwen3-vl:8b", "", 30)
        response = {"message": {"content": "最终答案"}}
        with patch("quant_service.ai_gateway._resolve_ollama_model", return_value=settings.model), patch(
            "quant_service.ai_gateway._request_json", return_value=response
        ) as request:
            answer = _call_provider(settings, [{"role": "user", "content": "问题"}])
        self.assertEqual(answer, "最终答案")
        self.assertFalse(request.call_args.kwargs["payload"]["think"])

    def test_chat_passes_selected_context_and_sources_to_provider(self) -> None:
        values = {
            "QUANTWEAVE_AI_PROVIDER": "openai_compatible",
            "QUANTWEAVE_AI_BASE_URL": "https://api.example.com/v1",
            "QUANTWEAVE_AI_MODEL": "test-model",
            "QUANTWEAVE_AI_API_KEY": "secret",
            "QUANTWEAVE_EXTERNAL_DATA_SOURCES": "[]",
        }
        with patch.dict(os.environ, values, clear=False), patch(
            "quant_service.ai_gateway._local_context",
            return_value=({"本地行情数据库概况": {"bars": 100}}, [{"id": "market_database", "name": "本地行情数据库概况"}]),
        ), patch("quant_service.ai_gateway._external_context", return_value=({}, [])), patch(
            "quant_service.ai_gateway._call_provider", return_value="可验证回答"
        ) as provider:
            result = chat([{"role": "user", "content": "怎么搭策略"}], ["market_database"])
        self.assertEqual(result["answer"], "可验证回答")
        self.assertEqual(result["sources"][0]["id"], "market_database")
        messages = provider.call_args.args[1]
        self.assertIn("本地行情数据库概况", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
