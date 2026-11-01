"""Exécute les 3 scénarios du TP3 avec l'agent, et le RAG statique du TP2 sur les mêmes questions.

Les traces sont copiées dans docs/traces/ (JSON + Markdown) et la comparaison agent / RAG
statique est écrite dans docs/traces/comparaison.md et comparaison.json.

    python -m scripts.run_scenarios            # les 3 scénarios
    python -m scripts.run_scenarios --only s3  # un seul
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from backend.agent.runner import MANAGER
from backend.juris.rag import static_rag
from backend.juris_api import SCENARIOS

OUT = Path(__file__).resolve().parent.parent / "docs" / "traces"


def run_agent(question: str, session: str) -> dict:
    run = MANAGER.start(question, session, max_iterations=5, use_external=True)
    seen = 0
    while True:
        events = run.wait_events(seen, timeout=5)
        for e in events:
            if e["type"] != "NODE":
                print(f"   {e['t']:>6.1f}s {e['type']:<12} {e['title']}", flush=True)
        seen += len(events)
        if run.done and seen >= len(run.events):
            break
    time.sleep(0.5)  # laisse le temps à l'écriture de la trace
    return {"run": run, "saved": getattr(run, "saved_as", None)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--no-static", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    comp_path = OUT / "comparaison.json"
    rows = json.loads(comp_path.read_text(encoding="utf-8")) if comp_path.exists() else {}
    for sc in SCENARIOS:
        if args.only and sc["id"] not in args.only:
            continue
        print(f"\n=== Scénario {sc['id']} — {sc['label']}\n{sc['question']}", flush=True)
        res = run_agent(sc["question"], session=f"scenario-{sc['id']}")
        run = res["run"]
        if res["saved"]:
            src = Path(res["saved"])
            for ext in (".json", ".md"):
                shutil.copy(src.with_suffix(ext), OUT / f"scenario_{sc['id']}{ext}")
        f = run.final or {}
        m = f.get("metrics", {})
        row = {"scenario": sc["id"], "label": sc["label"], "question": sc["question"],
               "agent": {"status": f.get("status"), "answer": f.get("answer"),
                         "decisions": f.get("iterations"), "llm_calls": m.get("llm_calls"),
                         "tool_calls": m.get("tool_calls"), "seconds": m.get("total_seconds"),
                         "tokens": (m.get("prompt_tokens") or 0) + (m.get("completion_tokens") or 0),
                         "path": run.path,
                         "sources": list(dict.fromkeys(
                             [s["title"] for s in f.get("sources", {}).get("corpus", [])] +
                             [s["title"] for s in f.get("sources", {}).get("external", [])]))}}
        if not args.no_static:
            print("   RAG statique…", flush=True)
            s = static_rag(sc["question"])
            row["static"] = {"answer": s["answer"], "found": s["found"], "citations": s["citations"],
                             "llm_calls": s["usage"]["llm_calls"], "seconds": s["latency_s"],
                             "tokens": s["usage"]["prompt_tokens"] + s["usage"]["completion_tokens"],
                             "sources": list(dict.fromkeys(p["title"] for p in s["passages"] if p["pid"] in s["citations"]))}
        rows[sc["id"]] = row
        comp_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["# Agentic RAG (TP3) vs RAG statique (TP2)", "",
             "| Scénario | Système | Statut | Appels LLM | Outils | Tokens | Durée (s) |", "|---|---|---|---|---|---|---|"]
    for r in rows.values():
        a = r["agent"]
        lines.append(f"| {r['label']} | Agent | {a['status']} | {a['llm_calls']} | {a['tool_calls']} | {a['tokens']} | {a['seconds']} |")
        if "static" in r:
            s = r["static"]
            lines.append(f"| {r['label']} | RAG statique | {'répond' if s['found'] else 'pas de réponse'} | "
                         f"{s['llm_calls']} | 1 | {s['tokens']} | {s['seconds']} |")
    for r in rows.values():
        lines += ["", f"## {r['label']}", "", f"**Question** : {r['question']}", "",
                  f"**Agent** ({' → '.join(r['agent']['path'])}) : {r['agent']['answer']}", "",
                  f"Sources : {', '.join(r['agent']['sources']) or 'aucune'}"]
        if "static" in r:
            lines += ["", f"**RAG statique** : {r['static']['answer']}", "",
                      f"Sources : {', '.join(r['static']['sources']) or 'aucune'}"]
    (OUT / "comparaison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nTraces et comparaison écrites dans {OUT}")


if __name__ == "__main__":
    sys.exit(main())
