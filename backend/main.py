"""API backend (FastAPI). Le frontend ne parle qu'à cette API, jamais au modèle.

Lancement : uvicorn backend.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .dataset import doc_text, filter_relations, load_split, truncate
from .extraction import run_extraction
from .llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_PROVIDER, GenerationParams, LLMClient, LLMError
from .metrics import aggregate, mark_correct, score_document
from .prompts import STRATEGIES
from .relations import CORE_RELATIONS, RELATIONS, resolve_relation_set

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="TP - Extraction d'informations avec un petit LLM")
client = LLMClient()


class ExtractRequest(BaseModel):
    text: str | None = Field(None, description="Texte libre (ou utiliser example_id)")
    example_id: str | None = Field(None, description="Document du jeu d'évaluation, ex. 'test-3'")
    strategy: str = "few_shot"
    params: GenerationParams = GenerationParams()
    relations: str | list[str] = "core"
    n_shots: int = Field(3, ge=0, le=8)
    drop_invalid: bool = True
    max_sents: int | None = Field(None, ge=1)


class CompareRequest(ExtractRequest):
    strategies: list[str] = list(STRATEGIES)
    temperatures: list[float] = [0.0]


def _find_example(example_id: str) -> dict:
    for doc in load_split("eval"):
        if doc["id"] == example_id:
            return doc
    raise HTTPException(404, f"exemple inconnu : {example_id}")


def _prepare(req: ExtractRequest) -> tuple[str, list[str], dict | None]:
    try:
        codes = resolve_relation_set(req.relations)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if req.strategy not in STRATEGIES:
        raise HTTPException(422, f"stratégie inconnue : {req.strategy}")
    doc = None
    if req.example_id:
        doc = filter_relations(truncate(_find_example(req.example_id), req.max_sents), codes)
        text = doc_text(doc)
    elif req.text and req.text.strip():
        text = req.text.strip()
    else:
        raise HTTPException(422, "fournir `text` ou `example_id`")
    return text, codes, doc


def _run(req: ExtractRequest, strategy: str, params: GenerationParams) -> dict:
    text, codes, doc = _prepare(req)
    try:
        result = run_extraction(client, text, strategy, params, codes, req.n_shots, req.drop_invalid)
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    result["text"] = text
    if doc is not None:
        row = score_document(result["relations"], result["relations"] + result["rejected"], doc)
        row.update({k: result[k] for k in ("json_valid", "schema_valid", "latency_s")})
        result["score"] = aggregate([row])
        mark_correct(result["relations"] + result["rejected"], doc)
        result["gold"] = [{k: t[k] for k in ("subject", "relation", "object")} for t in doc["triples"]]
    return result


@app.get("/api/health")
def health() -> dict:
    return {"provider": client.provider, "base_url": client.base_url,
            "model": client.model, **client.health()}


@app.get("/api/config")
def config() -> dict:
    return {
        "provider": DEFAULT_PROVIDER, "base_url": DEFAULT_BASE_URL, "model": DEFAULT_MODEL,
        "strategies": STRATEGIES,
        "relations": [{"code": c, "label": l, "definition": d, "core": c in CORE_RELATIONS}
                      for c, (l, d) in RELATIONS.items()],
        "default_params": GenerationParams().model_dump(),
    }


@app.get("/api/examples")
def examples(limit: int = 100) -> list[dict]:
    return [{"id": d["id"], "title": d["title"], "n_sents": len(d["sents"]),
             "n_triples": len(d["triples"])} for d in load_split("eval")[:limit]]


@app.get("/api/examples/{example_id}")
def example(example_id: str, relations: str = "core", max_sents: int | None = None) -> dict:
    doc = filter_relations(truncate(_find_example(example_id), max_sents), resolve_relation_set(relations))
    return {"id": doc["id"], "title": doc["title"], "text": doc_text(doc),
            "gold": [{k: t[k] for k in ("subject", "relation", "object")} for t in doc["triples"]]}


@app.post("/api/extract")
def extract(req: ExtractRequest) -> dict:
    return _run(req, req.strategy, req.params)


@app.post("/api/compare")
def compare(req: CompareRequest) -> list[dict]:
    """Lance plusieurs stratégies / températures sur le même texte, mêmes autres paramètres."""
    out = []
    for strategy in req.strategies:
        for temp in req.temperatures:
            params = req.params.model_copy(update={"temperature": temp})
            res = _run(req, strategy, params)
            out.append({k: res.get(k) for k in (
                "strategy", "params", "relations", "rejected", "json_valid", "schema_valid",
                "errors", "latency_s", "score")})
    return out


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")
