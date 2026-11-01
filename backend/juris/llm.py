"""Appels au LLM local pour le RAG et l'agent, avec sortie JSON et comptage.

On réutilise `LLMClient` du TP1. Le modèle par défaut est `llama3.2:3b` (variable
`RAG_MODEL`) : en français juridique, et surtout pour choisir des outils, le 1B se trompe
trop souvent (actions inventées, réponses hors sujet). Le 1B reste utilisable :
RAG_MODEL=llama3.2:1b.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field

from ..llm_client import DEFAULT_BASE_URL, DEFAULT_PROVIDER, GenerationParams, LLMClient, LLMError

RAG_MODEL = os.getenv("RAG_MODEL", "llama3.2:3b")

_clients: dict[str, LLMClient] = {}
_lock = threading.Lock()


def get_client(model: str | None = None) -> LLMClient:
    model = model or RAG_MODEL
    with _lock:
        if model not in _clients:
            _clients[model] = LLMClient(DEFAULT_PROVIDER, DEFAULT_BASE_URL, model)
    return _clients[model]


@dataclass
class Usage:
    """Compteurs d'observabilité : appels LLM, tokens et temps passé dans le modèle."""
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_seconds: float = 0.0
    calls: list[dict] = field(default_factory=list)

    def add(self, purpose: str, resp) -> None:
        self.llm_calls += 1
        self.prompt_tokens += resp.prompt_tokens or 0
        self.completion_tokens += resp.completion_tokens or 0
        self.llm_seconds += resp.latency_s
        self.calls.append({"purpose": purpose, "latency_s": round(resp.latency_s, 2),
                           "prompt_tokens": resp.prompt_tokens,
                           "completion_tokens": resp.completion_tokens})

    def to_dict(self) -> dict:
        return {"llm_calls": self.llm_calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "llm_seconds": round(self.llm_seconds, 1), "calls": self.calls}


def parse_json(raw: str) -> dict | None:
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                out = json.loads(m.group(0))
                return out if isinstance(out, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def llm_json(system: str, user: str, *, purpose: str, usage: Usage | None = None,
             max_tokens: int = 512, model: str | None = None, temperature: float = 0.0) -> tuple[dict | None, str]:
    """Un appel au LLM en mode JSON. Renvoie (objet ou None si invalide, sortie brute).

    Lève LLMError si le serveur local est injoignable.
    """
    params = GenerationParams(temperature=temperature, seed=42, top_p=0.9, top_k=40,
                              max_tokens=max_tokens, num_ctx=6144, json_mode=True)
    resp = get_client(model).chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], params)
    if usage is not None:
        usage.add(purpose, resp)
    return parse_json(resp.content), resp.content


__all__ = ["LLMError", "Usage", "get_client", "llm_json", "parse_json", "RAG_MODEL"]
