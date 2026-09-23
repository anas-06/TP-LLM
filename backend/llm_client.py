"""Client pour le serveur LLM local (Ollama ou llama.cpp).

Tous les paramètres de génération passent par `GenerationParams`, pour que
chaque comparaison de prompts soit faite avec des paramètres explicites et
identiques (température, seed, top_p, top_k, longueur max, contexte).
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass

import httpx
from pydantic import BaseModel, Field

DEFAULT_MODEL = os.getenv("LLM_MODEL", "llama3.2:1b")
DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")  # "ollama" | "llamacpp"
DEFAULT_BASE_URL = os.getenv(
    "LLM_BASE_URL",
    "http://localhost:11434" if DEFAULT_PROVIDER == "ollama" else "http://localhost:8080",
)
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT", "300"))


class GenerationParams(BaseModel):
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    seed: int = 42
    top_p: float = Field(0.9, gt=0.0, le=1.0)
    top_k: int = Field(40, ge=0)
    max_tokens: int = Field(1024, ge=16, le=4096)
    num_ctx: int = Field(8192, ge=512)
    json_mode: bool = True  # contraint la sortie au JSON (grammaire côté serveur)


@dataclass
class LLMResponse:
    content: str
    latency_s: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, provider: str = DEFAULT_PROVIDER, base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL, timeout: float = TIMEOUT_S):
        if provider not in {"ollama", "llamacpp"}:
            raise ValueError(f"provider inconnu : {provider}")
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._http = httpx.Client(timeout=timeout)

    def chat(self, messages: list[dict], params: GenerationParams) -> LLMResponse:
        start = time.perf_counter()
        try:
            if self.provider == "ollama":
                resp = self._chat_ollama(messages, params)
            else:
                resp = self._chat_llamacpp(messages, params)
        except httpx.HTTPError as exc:
            raise LLMError(f"Appel au LLM local impossible ({self.base_url}) : {exc}") from exc
        resp.latency_s = time.perf_counter() - start
        return resp

    def _chat_ollama(self, messages: list[dict], p: GenerationParams) -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": p.temperature,
                "seed": p.seed,
                "top_p": p.top_p,
                "top_k": p.top_k,
                "num_predict": p.max_tokens,
                "num_ctx": p.num_ctx,
            },
        }
        if p.json_mode:
            payload["format"] = "json"
        r = self._http.post(f"{self.base_url}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        return LLMResponse(
            content=data["message"]["content"],
            latency_s=0.0,
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
        )

    def _chat_llamacpp(self, messages: list[dict], p: GenerationParams) -> LLMResponse:
        # Serveur llama.cpp (`llama-server`), API compatible OpenAI.
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": p.temperature,
            "seed": p.seed,
            "top_p": p.top_p,
            "top_k": p.top_k,
            "max_tokens": p.max_tokens,
        }
        if p.json_mode:
            payload["response_format"] = {"type": "json_object"}
        r = self._http.post(f"{self.base_url}/v1/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        return LLMResponse(
            content=data["choices"][0]["message"]["content"],
            latency_s=0.0,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    def health(self) -> dict:
        try:
            if self.provider == "ollama":
                r = self._http.get(f"{self.base_url}/api/tags", timeout=5)
                r.raise_for_status()
                models = [m["name"] for m in r.json().get("models", [])]
                return {"ok": self.model in models or any(m.startswith(self.model) for m in models),
                        "models": models}
            r = self._http.get(f"{self.base_url}/health", timeout=5)
            return {"ok": r.status_code == 200, "models": [self.model]}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc), "models": []}
