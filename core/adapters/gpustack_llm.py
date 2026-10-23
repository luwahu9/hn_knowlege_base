"""LlmGateway 的 GPUStack 实现（OpenAI 兼容接口）。规格：docs/spec-13-qa.md"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from core.app.ports import LlmResult, LlmUsage, PermanentError, Role, TransientError
from core.domain.qa import extract_json


def api_base(url: str) -> str:
    """规范化服务地址：去掉末尾的 "/"，再去掉末尾的 "/v1"（如有），最后加上 "/v1"。
    例："http://h:80" → "http://h:80/v1"；"http://h:80/v1/" → "http://h:80/v1"。url 为空 → ValueError。"""
    url = url.strip().rstrip("/")
    if not url:
        raise ValueError("服务地址不能为空")
    return url.removesuffix("/v1") + "/v1"


class GpustackLlm:
    """models：角色 → 对话模型名；embed_model / rerank_model：向量与重排模型名（None 表示不可用）。
    extra_body：原样合并进每个对话请求体（例如关闭思考模式的参数），不覆盖本类设置的字段。
    api_key 给出时以 "Authorization: Bearer <key>" 发送。
    structured_output 为 False 时不发送 response_format（后端不支持结构化输出时使用），仍按 json_schema 解析文本。"""

    def __init__(self, base_url: str, *, models: Mapping[Role, str], embed_model: str | None = None,
                 rerank_model: str | None = None, api_key: str | None = None,
                 extra_body: Mapping[str, Any] | None = None, structured_output: bool = True,
                 timeout_seconds: float = 180.0,
                 http: httpx.Client | None = None) -> None:
        from copy import deepcopy

        self._base = api_base(base_url)
        self._models = dict(models)
        self._embed_model = embed_model
        self._rerank_model = rerank_model
        self._extra = deepcopy(dict(extra_body or {}))
        self._structured = structured_output
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout_seconds
        self._http = http if http is not None else httpx.Client(timeout=timeout_seconds)

    def complete(self, role: Role, messages: Sequence[Mapping[str, str]], *,
                 json_schema: Mapping[str, Any] | None = None, temperature: float = 0.0,
                 max_tokens: int = 2048, caller: str) -> LlmResult:
        """POST /chat/completions。

        - 角色未配置模型 → PermanentError（不发请求）；messages 为空 → ValueError。
        - 请求体：extra_body 的内容（其中的 "response_format" 一律忽略，该字段只由本类控制），再覆盖
          {"model", "messages", "temperature", "max_tokens", "stream": false}；
          json_schema 给出且 structured_output 为 True 时再加 "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": json_schema}}。
        - 响应：text = choices[0].message.content（缺失或不是字符串 → PermanentError）；
          usage 取 usage.prompt_tokens / completion_tokens，缺失记 0；model_alias = 响应的 model，缺失用请求的模型名。
        - json_schema 给出时 parsed = extract_json(text)（得到 dict 才算，否则 None；不做逐字段 schema 校验，
          由用例层的 parse_answer 校验）；未给出时 parsed = None。
        - 错误：连接失败、超时、5xx、429 → TransientError；其他非 200、非 JSON → PermanentError。
        """
        if role not in self._models:
            raise PermanentError("该角色未配置对话模型")
        if not messages:
            raise ValueError("消息不能为空")
        body = dict(self._extra)
        body.pop("response_format", None)
        model = self._models[role]
        body.update(model=model, messages=[dict(message) for message in messages],
                    temperature=temperature, max_tokens=max_tokens, stream=False)
        if json_schema is not None and self._structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": dict(json_schema)}}
        data = self._request("/chat/completions", body)
        try:
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            prompt, completion = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            alias = data.get("model", model)
            if (not isinstance(text, str) or not isinstance(alias, str)
                    or type(prompt) is not int or type(completion) is not int):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise PermanentError("GPUStack 对话响应格式不正确") from None
        parsed = extract_json(text) if json_schema is not None else None
        return LlmResult(text, parsed if isinstance(parsed, dict) else None, LlmUsage(prompt, completion), alias)

    def embed(self, texts: Sequence[str], *, caller: str) -> list[list[float]]:
        """POST /embeddings，body {"model", "input": list(texts)}。embed_model 为 None → PermanentError；
        texts 为空 → 返回 []（不发请求）。按每项的 index 排序后取 embedding；条数不等于输入 → PermanentError。"""
        if self._embed_model is None:
            raise PermanentError("未配置向量模型")
        if not texts:
            return []
        data = self._request("/embeddings", {"model": self._embed_model, "input": list(texts)})
        try:
            items = data["data"]
            if not isinstance(items, list) or len(items) != len(texts):
                raise ValueError
            return [[float(value) for value in item["embedding"]] for item in sorted(items, key=lambda item: item["index"])]
        except (KeyError, TypeError, ValueError, OverflowError):
            raise PermanentError("GPUStack 向量响应格式或数量不正确") from None

    def rerank(self, query: str, passages: Sequence[str], *, caller: str) -> list[float]:
        """POST /rerank，body {"model", "query", "documents": list(passages)}。rerank_model 为 None → PermanentError；
        passages 为空 → 返回 []（不发请求）。响应 results 每项含 index 与 relevance_score；
        按 index 放回输入顺序；有缺失、重复或越界的 index → PermanentError。"""
        if self._rerank_model is None:
            raise PermanentError("未配置重排模型")
        if not passages:
            return []
        data = self._request("/rerank", {"model": self._rerank_model, "query": query, "documents": list(passages)})
        try:
            items = data["results"]
            if not isinstance(items, list) or len(items) != len(passages):
                raise ValueError
            scores: dict[int, float] = {}
            for item in items:
                index = item["index"]
                if type(index) is not int or not 0 <= index < len(passages) or index in scores:
                    raise ValueError
                scores[index] = float(item["relevance_score"])
            return [scores[i] for i in range(len(passages))]
        except (KeyError, TypeError, ValueError, OverflowError):
            raise PermanentError("GPUStack 重排响应缺少有效且唯一的序号或分数") from None

    def _request(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """统一请求和错误分类，错误信息不引用请求头或服务返回的凭据。"""
        try:
            response = self._http.post(self._base + path, headers=self._headers, json=dict(body), timeout=self._timeout)
        except httpx.RequestError as exc:
            raise TransientError(f"GPUStack 请求失败（{type(exc).__name__}）") from None
        if response.status_code == 429 or 500 <= response.status_code < 600:
            raise TransientError(f"GPUStack HTTP {response.status_code}")
        if response.status_code != 200:
            raise PermanentError(f"GPUStack HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise PermanentError("GPUStack 响应不是有效 JSON") from None
        if not isinstance(data, dict):
            raise PermanentError("GPUStack 响应不是 JSON 对象")
        return data

