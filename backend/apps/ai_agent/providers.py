"""OpenAI-compatible model provider abstractions.

The web/API layer only depends on :class:`LLMProvider`.  The default provider
speaks the OpenAI chat-completions protocol and deliberately keeps credentials
on the server side.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import jsonschema
from django.conf import settings


@dataclass(frozen=True)
class ModelInfo:
    id: str
    owned_by: str = ""
    context_length: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderError(RuntimeError):
    """An upstream/provider failure safe to surface as a user-facing error.

    ``code`` is deliberately stable so callers can distinguish timeout,
    throttling and authentication failures without parsing provider prose.
    """

    def __init__(self, message: str, code: str = "provider_error", *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LLMProvider(Protocol):
    provider_id: str
    api_key: str

    def list_models(self) -> list[ModelInfo]: ...

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterable[str]: ...

    def extract_structured_data(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        model: str = "",
    ) -> dict[str, Any]: ...


class OpenAICompatibleProvider:
    provider_id = "default"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, *, workspace=None):
        self.workspace = workspace
        workspace_ai = ((getattr(workspace, "settings", {}) or {}).get("ai", {}) if workspace else {}) or {}
        configured_base_url = workspace_ai.get("base_url") if isinstance(workspace_ai, dict) else None
        configured_model = workspace_ai.get("model") if isinstance(workspace_ai, dict) else None
        self.base_url = (base_url or configured_base_url or settings.OPENAI_BASE_URL).rstrip("/")
        self.default_model = str(configured_model or settings.OPENAI_MODEL)
        if api_key is not None:
            self.api_key = api_key
            self.credential_source = "explicit"
        else:
            from apps.core.services.workspace_credentials import workspace_api_key
            workspace_key = workspace_api_key(workspace) if workspace is not None else ""
            self.api_key = workspace_key or settings.OPENAI_API_KEY
            self.credential_source = "workspace" if workspace_key else ("environment" if self.api_key else "none")
        self.last_usage: dict[str, int] = {}

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError("No AI API key is configured for this workspace or server", "provider_not_configured")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.OPENAI_TIMEOUT) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                raise ProviderError(f"upstream returned {exc.code}: {detail[:500]}", "provider_rate_limited", retryable=True) from exc
            code = "provider_auth_failed" if exc.code in {401, 403} else "provider_http_error"
            raise ProviderError(f"upstream returned {exc.code}: {detail[:500]}", code, retryable=exc.code >= 500) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if isinstance(exc, TimeoutError) or getattr(exc, "reason", None).__class__.__name__ in {"TimeoutError", "socket.timeout"}:
                raise ProviderError(f"upstream timeout: {exc}", "provider_timeout", retryable=True) from exc
            raise ProviderError(f"upstream unavailable: {exc}", "provider_unavailable", retryable=True) from exc
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError("upstream returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ProviderError("upstream returned an invalid response", "provider_invalid_response")
        usage = result.get("usage")
        self.last_usage = {
            key: int(usage[key]) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(usage, dict) and str(usage.get(key, "")).isdigit()
        }
        return result

    def list_models(self) -> list[ModelInfo]:
        result = self._request("models")
        raw_models = result.get("data", [])
        if not isinstance(raw_models, list):
            raise ProviderError("upstream returned an invalid model list")
        models: list[ModelInfo] = []
        for item in raw_models:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            context_length = item.get("context_length")
            if context_length is not None:
                try:
                    context_length = int(context_length)
                except (TypeError, ValueError):
                    context_length = None
            models.append(
                ModelInfo(
                    id=str(item["id"]),
                    owned_by=str(item.get("owned_by", "")),
                    context_length=context_length,
                )
            )
        return models

    def complete(
        self,
        messages: list[dict[str, str]],
        model: str,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": 0.35,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        result = self._request("chat/completions", payload, method="POST")
        usage = result.get("usage") if isinstance(result, dict) else None
        if isinstance(usage, dict):
            self.last_usage = {
                key: int(usage[key]) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if str(usage.get(key, "")).isdigit()
            }
        choices = result.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ProviderError("upstream returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("text") is not None
            )
        return str(content or "")

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterable[str]:
        if not self.api_key:
            raise ProviderError("未配置 API Key")
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.OPENAI_TIMEOUT) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        item = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError("上游流式响应不是有效 JSON") from exc
                    if isinstance(item, dict) and item.get("error"):
                        raise ProviderError("上游流式响应错误")
                    choices = item.get("choices", []) if isinstance(item, dict) else []
                    if choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta") or {}
                        text = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(text, str) and text:
                            yield text
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            code = "provider_rate_limited" if exc.code == 429 else ("provider_auth_failed" if exc.code in {401, 403} else "provider_http_error")
            raise ProviderError(f"upstream returned {exc.code}: {detail[:500]}", code, retryable=exc.code >= 500 or exc.code == 429) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            code = "provider_timeout" if isinstance(exc, TimeoutError) else "provider_unavailable"
            raise ProviderError("上游流式响应失败；请检查服务地址、凭证及模型", code, retryable=True) from exc

    def extract_structured_data(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        model: str = "",
    ) -> dict[str, Any]:
        # JSON mode is preferred. Some OpenAI-compatible servers reject the
        # response_format field, so retry once with the constrained prompt.
        try:
            raw = self.complete(messages, model, json_mode=True)
        except ProviderError as exc:
            detail = str(exc).lower()
            if "400" not in detail or "response_format" not in detail:
                raise
            raw = self.complete(messages, model, json_mode=False)

        raw = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            raw = fenced.group(1).strip()
        try:
            data = json.loads(raw)
            jsonschema.validate(data, schema)
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            raise ProviderError("AI 结构化提取无效；未创建提案，原始对话已保留") from exc
        if not isinstance(data, dict):
            raise ProviderError("AI 结构化提取必须返回 JSON 对象")
        return data


def configured_provider(workspace=None) -> OpenAICompatibleProvider:
    """Build a provider using workspace settings/secret with env fallback."""
    return OpenAICompatibleProvider(workspace=workspace)
