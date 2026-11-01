"""Exécution des runs de l'agent en arrière-plan, diffusion de la trace (SSE),
validation humaine, mémoire de session et sauvegarde des traces.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .graph import GRAPH, MAX_ITERATIONS_DEFAULT, RunContext

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs" / "agent"


class Run:
    def __init__(self, run_id: str, question: str, options: dict) -> None:
        self.id = run_id
        self.question = question
        self.options = options
        self.events: list[dict] = []
        self.done = False
        self.final: dict | None = None
        self.path: list[str] = []
        self.cond = threading.Condition()
        self._approval_event = threading.Event()
        self._approval_value = False
        self.waiting_approval = False
        self.started = datetime.now()

    def push(self, event: dict) -> None:
        with self.cond:
            event["seq"] = len(self.events)
            self.events.append(event)
            self.cond.notify_all()

    def wait_events(self, since: int, timeout: float = 15.0) -> list[dict]:
        with self.cond:
            if len(self.events) <= since and not self.done:
                self.cond.wait(timeout)
            return self.events[since:]

    def request_approval(self, _info: dict, timeout: float = 180.0) -> bool:
        self.waiting_approval = True
        self._approval_event.clear()
        ok = self._approval_event.wait(timeout)
        self.waiting_approval = False
        return ok and self._approval_value

    def resolve_approval(self, approve: bool) -> bool:
        if not self.waiting_approval:
            return False
        self._approval_value = approve
        self._approval_event.set()
        return True


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.sessions: dict[str, dict] = {}  # mémoire : dernière affaire et dernier échange
        self._lock = threading.Lock()
        self._busy = threading.Lock()  # un run à la fois : le LLM local est séquentiel

    def start(self, question: str, session_id: str | None = None, *, max_iterations: int = MAX_ITERATIONS_DEFAULT,
              use_external: bool = True, require_approval: bool = False, llm_check: bool = True,
              use_memory: bool = True, model: str | None = None) -> Run:
        options = {"max_iterations": max_iterations, "use_external": use_external,
                   "require_approval": require_approval, "llm_check": llm_check,
                   "use_memory": use_memory, "model": model, "session_id": session_id}
        run = Run(uuid.uuid4().hex[:10], question, options)
        with self._lock:
            self.runs[run.id] = run
        threading.Thread(target=self._execute, args=(run,), daemon=True).start()
        return run

    def _execute(self, run: Run) -> None:
        o = run.options
        with self._busy:
            ctx = RunContext(run.push, approval=run.request_approval if o["require_approval"] else None,
                             llm_check=o["llm_check"], model=o["model"])
            memory = self.sessions.get(o["session_id"] or "", {}) if o["use_memory"] else {}
            state = {"question": run.question, "memory": memory, "evidence": [], "candidates": [],
                     "searches": [], "read_cases": [], "external_used": False,
                     "external_allowed": o["use_external"], "iteration": 0,
                     "max_iterations": o["max_iterations"], "feedback": None, "draft": None}
            ctx.emit("START", "Question reçue", "start", question=run.question, options=o,
                     memory=memory or None)
            final_state: dict = dict(state)
            try:
                # stream_mode="updates" : un élément par nœud exécuté (ordre du graphe).
                for update in GRAPH.stream(state, {"configurable": {"ctx": ctx}, "recursion_limit": 60},
                                           stream_mode="updates"):
                    for node, values in update.items():
                        run.path.append(node)
                        if values:
                            final_state.update(values)
            except Exception as exc:  # erreur imprévue : arrêt contrôlé et visible
                ctx.emit("ERROR", "Erreur inattendue pendant l'exécution", "runner", error=repr(exc))
                ctx.emit("FINAL", "Exécution interrompue", "final", answer="Erreur pendant l'exécution.",
                         status="erreur", stop_reason="erreur contrôlée", sources={"corpus": [], "external": []},
                         warning=repr(exc), iterations=final_state.get("iteration", 0), metrics=ctx.metrics())
            run.final = final_state.get("final")
            self._remember(run, final_state)
            run.done = True
            with run.cond:
                run.cond.notify_all()
            self._save(run, final_state, ctx)

    def _remember(self, run: Run, s: dict) -> None:
        sid = run.options.get("session_id")
        if not sid or not run.final:
            return
        corpus = run.final.get("sources", {}).get("corpus", [])
        case_id = corpus[0]["case_id"] if corpus else (s.get("read_cases") or [None])[-1]
        if not case_id:
            return
        title = corpus[0]["title"] if corpus else next(
            (c["title"] for c in s.get("candidates", []) if c["case_id"] == case_id), case_id)
        self.sessions[sid] = {"case_id": case_id, "title": title, "last_question": run.question,
                              "last_answer": (run.final.get("answer") or "")[:300]}

    def _save(self, run: Run, s: dict, ctx: RunContext) -> None:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", run.question.lower())[:40].strip("-")
        base = RUNS_DIR / f"{run.started:%Y%m%d-%H%M%S}_{slug}_{run.id}"
        payload = {"run_id": run.id, "question": run.question, "options": run.options,
                   "started": run.started.isoformat(timespec="seconds"), "graph_path": run.path,
                   "final": run.final, "events": run.events}
        base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        base.with_suffix(".md").write_text(trace_markdown(payload), encoding="utf-8")
        run.saved_as = str(base.with_suffix(".json"))


def trace_markdown(p: dict) -> str:
    """Trace lisible (livrable) : une ligne par étape, comme demandé dans l'énoncé."""
    out = [f"# Trace — {p['question']}", "",
           f"- Run `{p['run_id']}` — {p['started']} — options : `{json.dumps(p['options'], ensure_ascii=False)}`",
           f"- Chemin dans le graphe : {' → '.join(p['graph_path'])}", "",
           "| # | t (s) | Type | Détail |", "|---|---|---|---|"]
    n = 0
    for e in p["events"]:
        if e["type"] in ("NODE",):
            continue
        n += 1
        d = e["data"]
        if e["type"] == "DECISION":
            det = f"**{e['title']}** — {d.get('reason', '')}"
            if d.get("guardrail"):
                det += f" _(garde-fou : {d['guardrail']})_"
        elif e["type"] == "TOOL":
            det = f"`{e['title']}`"
        elif e["type"] == "OBSERVATION":
            det = e["title"] + (f" — preuves ajoutées : {', '.join(d.get('new_evidence') or [])}" if d.get("new_evidence") else "")
        elif e["type"] == "CHECK":
            failed = [k for k, v in (d.get("checks") or {}).items() if not v]
            det = f"**{e['title']}**" + (f" — échecs : {', '.join(failed)} — {d.get('feedback')}" if failed else "") \
                  + f" → {d.get('next')}"
        elif e["type"] == "GENERATION":
            det = f"Brouillon ({', '.join(d.get('citations') or []) or 'aucune citation'}) : {d.get('answer', '')[:220]}"
        elif e["type"] == "FINAL":
            det = f"**{d.get('status')}** — arrêt : {d.get('stop_reason')}"
        elif e["type"] == "ERROR":
            det = f"⚠ {e['title']} : {d.get('error')}"
        else:
            det = e["title"]
        out.append(f"| {n} | {e['t']} | {e['type']} | {det.replace('|', '/').replace(chr(10), ' ')} |")
    f = p.get("final") or {}
    if f:
        out += ["", "## Réponse finale", "", f.get("answer", ""), ""]
        for s in f.get("sources", {}).get("corpus", []):
            out.append(f"- **[{s['pid']}] Corpus** — {s['title']} ({s['section']}) : « {s['text'][:300]} »")
        for s in f.get("sources", {}).get("external", []):
            out.append(f"- **[{s['pid']}] Externe** — {s['title']} — {s['url']}")
        m = f.get("metrics") or {}
        out += ["", f"**Mesures** : {f.get('iterations')} décision(s) de l'agent, {m.get('llm_calls')} appel(s) LLM, "
                f"{m.get('tool_calls')} appel(s) d'outil, {m.get('prompt_tokens')} + {m.get('completion_tokens')} tokens, "
                f"{m.get('total_seconds')} s au total."]
    return "\n".join(out) + "\n"


MANAGER = RunManager()
