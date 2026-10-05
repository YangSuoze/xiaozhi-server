import time

import httpx
import openai
from openai.types import CompletionUsage
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()


class LLMProvider(LLMProviderBase):
    supports_trace_id = True

    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")
        timeout = config.get("timeout", 300)
        self.timeout = int(timeout) if timeout else 300
        self.extra_body = config.get("extra_body") or {}
        if not isinstance(self.extra_body, dict):
            raise ValueError("LLM extra_body 必须是字典")

        param_defaults = {
            "max_tokens": int,
            "temperature": lambda x: round(float(x), 1),
            "top_p": lambda x: round(float(x), 1),
            "frequency_penalty": lambda x: round(float(x), 1),
        }

        for param, converter in param_defaults.items():
            value = config.get(param)
            try:
                setattr(
                    self,
                    param,
                    converter(value) if value not in (None, "") else None,
                )
            except (ValueError, TypeError):
                setattr(self, param, None)

        logger.debug(
            f"意图识别参数初始化: {self.temperature}, {self.max_tokens}, {self.top_p}, {self.frequency_penalty}"
        )

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
        )

    @staticmethod
    def normalize_dialogue(dialogue):
        """自动修复 dialogue 中缺失 content 的消息"""
        for msg in dialogue:
            if "role" in msg and "content" not in msg:
                msg["content"] = ""
        return dialogue

    def response(self, session_id, dialogue, **kwargs):
        trace_id = kwargs.get("trace_id", "-")
        started_at = time.monotonic()
        chunks = 0
        raw_chars = 0
        visible_chars = 0
        reasoning_chars = 0
        finish_reason = None
        response_id = None
        error_type = None
        completed = False
        try:
            dialogue = self.normalize_dialogue(dialogue)

            request_params = {
                "model": self.model_name,
                "messages": dialogue,
                "stream": True,
            }
            if self.extra_body:
                request_params["extra_body"] = self.extra_body

            # 添加可选参数,只有当参数不为None时才添加
            optional_params = {
                "max_tokens": kwargs.get("max_tokens", self.max_tokens),
                "temperature": kwargs.get("temperature", self.temperature),
                "top_p": kwargs.get("top_p", self.top_p),
                "frequency_penalty": kwargs.get(
                    "frequency_penalty", self.frequency_penalty
                ),
            }

            for key, value in optional_params.items():
                if value is not None:
                    request_params[key] = value

            template_kwargs = self.extra_body.get("chat_template_kwargs") or {}
            enable_thinking = self.extra_body.get(
                "enable_thinking",
                template_kwargs.get("enable_thinking")
                if isinstance(template_kwargs, dict) else None,
            )
            logger.bind(tag=TAG).info(
                f"LLM流请求 trace={trace_id} model={self.model_name} "
                f"messages={len(dialogue)} max_tokens={request_params.get('max_tokens')} "
                f"enable_thinking={enable_thinking}"
            )
            responses = self.client.chat.completions.create(**request_params)

            is_active = True
            for chunk in responses:
                chunks += 1
                response_id = response_id or getattr(chunk, "id", None)
                choices = getattr(chunk, "choices", None) or []
                choice = choices[0] if choices else None
                if choice and getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None) if choice else None
                reasoning = getattr(delta, "reasoning_content", None)
                if isinstance(reasoning, str):
                    reasoning_chars += len(reasoning)
                content = getattr(delta, "content", None) or ""
                if not isinstance(content, str):
                    content = ""
                raw_chars += len(content)
                if content:
                    if "<think>" in content:
                        is_active = False
                        content = content.split("<think>")[0]
                    if "</think>" in content:
                        is_active = True
                        content = content.split("</think>")[-1]
                    if is_active:
                        visible_chars += len(content)
                        yield content
            completed = True

        except Exception as e:
            error_type = type(e).__name__
            logger.bind(tag=TAG).error(
                f"LLM流异常 trace={trace_id} type={error_type} "
                f"status={getattr(e, 'status_code', '-')} "
                f"code={getattr(e, 'code', '-')} "
                f"request_id={getattr(e, 'request_id', '-')}"
            )
        finally:
            summary = (
                f"LLM流结束 trace={trace_id} model={self.model_name} "
                f"response_id={response_id or '-'} finish_reason={finish_reason or '-'} "
                f"chunks={chunks} raw_chars={raw_chars} visible_chars={visible_chars} "
                f"reasoning_chars={reasoning_chars} filtered_chars={raw_chars - visible_chars} "
                f"completed={completed} error={error_type or '-'} "
                f"elapsed_ms={int((time.monotonic() - started_at) * 1000)}"
            )
            if completed and visible_chars == 0:
                logger.bind(tag=TAG).warning(summary)
            else:
                logger.bind(tag=TAG).info(summary)

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        try:
            dialogue = self.normalize_dialogue(dialogue)
            logger.bind(tag=TAG).info("111")
            logger.bind(tag=TAG).info(dialogue)
            request_params = {
                "model": self.model_name,
                "messages": dialogue,
                "stream": True,
                "tools": functions,
            }
            if self.extra_body:
                request_params["extra_body"] = self.extra_body

            optional_params = {
                "max_tokens": kwargs.get("max_tokens", self.max_tokens),
                "temperature": kwargs.get("temperature", self.temperature),
                "top_p": kwargs.get("top_p", self.top_p),
                "frequency_penalty": kwargs.get(
                    "frequency_penalty", self.frequency_penalty
                ),
            }

            for key, value in optional_params.items():
                if value is not None:
                    request_params[key] = value
            logger.bind(tag=TAG).info(request_params)
            stream = self.client.chat.completions.create(**request_params)

            for chunk in stream:
                if getattr(chunk, "choices", None):
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", "")
                    tool_calls = getattr(delta, "tool_calls", None)
                    yield content, tool_calls
                elif isinstance(getattr(chunk, "usage", None), CompletionUsage):
                    usage_info = getattr(chunk, "usage", None)
                    logger.bind(tag=TAG).info(
                        f"Token 消耗：输入 {getattr(usage_info, 'prompt_tokens', '未知')}，"
                        f"输出 {getattr(usage_info, 'completion_tokens', '未知')}，"
                        f"共计 {getattr(usage_info, 'total_tokens', '未知')}"
                    )

        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in function call streaming: {e}")
            yield f"【OpenAI服务响应异常: {e}】", None
