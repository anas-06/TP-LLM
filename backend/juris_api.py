"""Routes API du TP2 (RAG jurisprudence) et du TP3 (agent)."""

from __future__ import annotations

import json
from collections import Counter

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent.runner import MANAGER, RUNS_DIR
from .agent.tools import TOOLS
from .juris import rag
from .juris.corpus import load_cases
from .juris.index import INDEX_DIR, get_index
from .juris.llm import RAG_MODEL, LLMError, get_client

router = APIRouter(prefix="/api")

SCENARIOS = [
    {"id": "s1", "label": "RAG simple",
     "question": "Le juge judiciaire peut-il apprécier le caractère réel et sérieux du licenciement d'un salarié protégé autorisé par l'inspecteur du travail ?"},
    {"id": "s2", "label": "Multi-étapes",
     "question": "Dans l'affaire n° 23-20.161 sur un bail rural, que reprochait le bailleur à l'arrêt d'appel et qu'a décidé la Cour de cassation ?"},
    {"id": "s3", "label": "Information insuffisante",
     "question": "Qu'est-ce que la théorie de l'imprévision et la Cour de cassation l'a-t-elle appliquée à un contrat ?"},
]


# ------------------------------------------------------------------ TP2
@router.get("/juris/status")
def status() -> dict:
    cases = load_cases()
    meta = {}
    if (INDEX_DIR / "meta.json").exists():
        meta = json.loads((INDEX_DIR / "meta.json").read_text(encoding="utf-8"))
    dates = sorted(c["decision_date"] for c in cases.values())
    return {"n_cases": len(cases), "date_min": dates[0], "date_max": dates[-1], "index": meta,
            "model": RAG_MODEL, "llm": get_client().health()}


@router.get("/juris/filters")
def filters() -> dict:
    cases = load_cases().values()
    return {"chambers": [c for c, _ in Counter(x.get("chamber") for x in cases).most_common() if c],
            "solutions": [s for s, _ in Counter(x.get("solution") for x in cases).most_common() if s]}


@router.get("/juris/search")
def search(q: str, k: int = 10, mode: str = "hybrid", chamber: str | None = None,
           solution: str | None = None) -> dict:
    if mode not in {"hybrid", "bm25", "dense"}:
        raise HTTPException(422, "mode : hybrid | bm25 | dense")
    if not q.strip():
        raise HTTPException(422, "requête vide")
    return rag.search_cases(q, k=max(1, min(k, 30)), mode=mode, chamber=chamber or None,
                            solution=solution or None)


@router.get("/juris/case/{case_id}")
def case(case_id: str) -> dict:
    c = rag.get_case(case_id)
    if not c:
        raise HTTPException(404, "décision introuvable")
    return c


class SummaryRequest(BaseModel):
    refresh: bool = False
    cached_only: bool = False  # ne renvoie que le résumé en cache, sans appeler le LLM


@router.post("/juris/case/{case_id}/summary")
def summary(case_id: str, req: SummaryRequest = SummaryRequest()) -> dict:
    try:
        return rag.summarize(case_id, refresh=req.refresh, cached_only=req.cached_only)
    except KeyError:
        raise HTTPException(404, "décision introuvable")
    except LLMError as exc:
        raise HTTPException(502, str(exc))


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3)
    history: list[dict] = []


@router.post("/juris/case/{case_id}/ask")
def ask(case_id: str, req: AskRequest) -> dict:
    try:
        return rag.answer_question(case_id, req.question, req.history)
    except KeyError:
        raise HTTPException(404, "décision introuvable")
    except LLMError as exc:
        raise HTTPException(502, str(exc))


class StaticRagRequest(BaseModel):
    question: str = Field(..., min_length=3)
    k: int = 5


@router.post("/juris/static_rag")
def static_rag(req: StaticRagRequest) -> dict:
    try:
        return rag.static_rag(req.question, req.k)
    except LLMError as exc:
        raise HTTPException(502, str(exc))


# ------------------------------------------------------------------ TP3
class AgentRequest(BaseModel):
    question: str = Field(..., min_length=3)
    session_id: str | None = None
    max_iterations: int = Field(5, ge=1, le=8)
    use_external: bool = True
    require_approval: bool = False
    llm_check: bool = True
    use_memory: bool = True


@router.get("/agent/config")
def agent_config() -> dict:
    return {"tools": TOOLS, "scenarios": SCENARIOS, "model": RAG_MODEL,
            "dense": get_index().has_dense}


@router.post("/agent/run")
def agent_run(req: AgentRequest) -> dict:
    run = MANAGER.start(req.question, req.session_id, max_iterations=req.max_iterations,
                        use_external=req.use_external, require_approval=req.require_approval,
                        llm_check=req.llm_check, use_memory=req.use_memory)
    return {"run_id": run.id}


@router.get("/agent/stream/{run_id}")
def agent_stream(run_id: str, since: int = 0) -> StreamingResponse:
    run = MANAGER.runs.get(run_id)
    if not run:
        raise HTTPException(404, "run inconnu")

    def gen():
        i = since
        while True:
            events = run.wait_events(i)
            for e in events:
                yield f"id: {e['seq']}\nevent: trace\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
            i += len(events)
            if run.done and i >= len(run.events):
                yield "event: done\ndata: {}\n\n"
                return
            if not events:
                yield ": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ApprovalRequest(BaseModel):
    approve: bool


@router.post("/agent/approve/{run_id}")
def agent_approve(run_id: str, req: ApprovalRequest) -> dict:
    run = MANAGER.runs.get(run_id)
    if not run:
        raise HTTPException(404, "run inconnu")
    return {"ok": run.resolve_approval(req.approve)}


@router.get("/agent/memory/{session_id}")
def agent_memory(session_id: str) -> dict:
    return MANAGER.sessions.get(session_id, {})


@router.delete("/agent/memory/{session_id}")
def agent_forget(session_id: str) -> dict:
    MANAGER.sessions.pop(session_id, None)
    return {"ok": True}


TRACES_DIR = RUNS_DIR.parents[1] / "docs" / "traces"


@router.get("/agent/saved/{name}")
def agent_saved(name: str) -> dict:
    """Trace sauvegardée d'un scénario (docs/traces/<name>.json), pour la relire dans l'interface."""
    if not name.replace("_", "").isalnum():
        raise HTTPException(422, "nom invalide")
    path = TRACES_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, "trace introuvable")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/agent/runs")
def agent_runs(limit: int = 20) -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    out = []
    for p in sorted(RUNS_DIR.glob("*.json"), reverse=True)[:limit]:
        d = json.loads(p.read_text(encoding="utf-8"))
        f = d.get("final") or {}
        out.append({"file": p.name, "question": d["question"], "started": d["started"],
                    "status": f.get("status"), "metrics": f.get("metrics")})
    return out
