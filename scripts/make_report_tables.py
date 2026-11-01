"""Génère les tableaux LaTeX du rapport à partir des traces réelles (docs/traces/*.json).

    python -m scripts.make_report_tables   ->  docs/rapport/generated/*.tex
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACES = ROOT / "docs" / "traces"
OUT = ROOT / "docs" / "rapport" / "generated"

COLORS = {"DECISION": "cDecision", "TOOL": "cTool", "OBSERVATION": "cObs", "GENERATION": "cGen",
          "CHECK": "cCheck", "FINAL": "cFinal", "ERROR": "cError", "APPROVAL": "cCheck", "START": "cObs"}
LABELS = {"GENERATION": "GÉNÉRATION", "ERROR": "ERREUR", "APPROVAL": "VALIDATION", "START": "DÉBUT"}
CHECK_LABEL = {"preuves_presentes": "preuves présentes", "citations_valides": "citations valides",
               "source_corpus_citee": "décision du corpus citée", "reponse_trouvee": "réponse trouvée",
               "affaire_demandee_citee": "affaire demandée citée", "notions_cles_sourcees": "notions clés sourcées", "pas_d_info_non_sourcee": "pas d'info non sourcée",
               "coherence_reponse_passages": "cohérence réponse/passages"}


def tex(s: object) -> str:
    s = str(s if s is not None else "")
    s = s.replace("\\", "\\textbackslash{}")
    for a, b in [("&", "\\&"), ("%", "\\%"), ("$", "\\$"), ("#", "\\#"), ("_", "\\_"), ("{", "\\{"),
                 ("}", "\\}"), ("~", "\\textasciitilde{}"), ("^", "\\textasciicircum{}")]:
        s = s.replace(a, b)
    s = s.replace("\\textbackslash\\{\\}", "\\textbackslash{}")
    for a, b in [("→", "$\\rightarrow$"), ("⇄", "$\\leftrightarrows$"), ("≈", "$\\approx$"), ("✓", "\\checkmark{}"),
                 ("✗", "$\\times$"), ("…", "\\ldots{}"), ("’", "'"), ("“", "``"), ("”", "''"), ("∅", "$\\emptyset$"),
                 (" ", "~"), (" ", "\\,"), ("⚠", "")]:
        s = s.replace(a, b)
    return re.sub(r"[^\x00-ɏ$\\{}~,`'^]", "", s)  # retire les symboles non gérés par pdfLaTeX


def cut(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def detail(e: dict) -> str:
    d = e["data"]
    t = e["type"]
    if t == "START":
        o = d.get("options", {})
        return tex(f"Question reçue ; {o.get('max_iterations')} étapes max, source externe "
                   f"{'activée' if o.get('use_external') else 'désactivée'}")
    if t == "DECISION":
        s = f"\\textbf{{{tex(e['title'])}}} --- {tex(cut(d.get('reason', ''), 140))}"
        if d.get("action") not in ("answer", "stop") and d.get("args"):
            args = {k: v for k, v in d["args"].items() if k not in ("case_id",)}
            if d["action"] == "read_case":
                args = {"case_id": d["args"].get("label")}
            s += f" \\newline\\texttt{{{tex(d['action'])}({tex(json.dumps(args, ensure_ascii=False)[1:-1])})}}"
        if d.get("guardrail"):
            s += f" \\newline\\textcolor{{cCheck}}{{\\emph{{garde-fou : {tex(d['guardrail'])}}}}}"
        return s
    if t == "TOOL":
        return f"\\texttt{{{tex(cut(e['title'], 110))}}}"
    if t == "OBSERVATION":
        s = tex(cut(e["title"], 160))
        if d.get("new_evidence"):
            s += tex(f" ; preuves ajoutées : {', '.join(d['new_evidence'])}")
        return s
    if t == "GENERATION":
        return f"Brouillon citant {tex(', '.join(d.get('citations') or []) or 'aucune source')} : \\emph{{{tex(cut(d.get('answer', ''), 230))}}}"
    if t == "CHECK":
        failed = [CHECK_LABEL.get(k, k) for k, v in (d.get("checks") or {}).items() if not v]
        s = f"\\textbf{{{tex(e['title'])}}}"
        if failed:
            s += tex(f" --- échecs : {', '.join(failed)}")
        if d.get("feedback"):
            s += f" \\newline{{\\small {tex(cut(d['feedback'], 200))}}}"
        s += tex(f" ; suite : {d.get('next')}")
        return s
    if t == "FINAL":
        return f"\\textbf{{Réponse {tex(d.get('status'))}}} --- condition d'arrêt : {tex(d.get('stop_reason'))}"
    if t == "ERROR":
        return tex(f"{e['title']} : {cut(d.get('error', ''), 160)}")
    return tex(e["title"])


def trace_table(sid: str) -> None:
    p = json.loads((TRACES / f"scenario_{sid}.json").read_text(encoding="utf-8"))
    rows = []
    n = 0
    for e in p["events"]:
        if e["type"] == "NODE":
            continue
        n += 1
        lab = LABELS.get(e["type"], e["type"])
        rows.append(f"{n} & {e['t']:.1f} & \\tracetag{{{COLORS.get(e['type'], 'cObs')}}}{{{tex(lab)}}} & {detail(e)} \\\\")
    f = p.get("final") or {}
    m = f.get("metrics") or {}
    out = [
        "\\begin{longtable}{@{}r r l p{0.66\\textwidth}@{}}",
        "\\toprule \\# & t (s) & Type & Détail \\\\ \\midrule \\endhead",
        *rows,
        "\\bottomrule \\end{longtable}",
        "\\begin{answerbox}{" + tex(f"Réponse finale ({f.get('status')})") + "}",
        tex(f.get("answer", "")),
    ]
    srcs = f.get("sources", {})
    if srcs.get("corpus") or srcs.get("external"):
        out.append("\\par\\smallskip{\\small")
        for s in srcs.get("corpus", []):
            out.append(f"\\textbf{{[{s['pid']}] Corpus}} --- {tex(s['title'])} ({tex(s['section'])}) : "
                       f"\\emph{{« {tex(cut(s['text'], 260))} »}}\\par")
        for s in srcs.get("external", []):
            out.append(f"\\textbf{{[{s['pid']}] Externe}} --- {tex(s['title'])} : \\url{{{s['url']}}}\\par")
        out.append("}")
    out.append("\\end{answerbox}")
    out.append(tex(f"Mesures : {f.get('iterations')} décisions de l'agent, {m.get('llm_calls')} appels LLM, "
                   f"{m.get('tool_calls')} appels d'outils, {(m.get('prompt_tokens') or 0) + (m.get('completion_tokens') or 0)} tokens, "
                   f"{m.get('total_seconds')} s. Chemin : {' → '.join(p['graph_path'])}.") + "\\par")
    (OUT / f"trace_{sid}.tex").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"trace_{sid}.tex : {n} étapes")


def comparison() -> None:
    path = TRACES / "comparaison.json"
    if not path.exists():
        return
    rows = json.loads(path.read_text(encoding="utf-8"))
    lines = ["\\begin{tabular}{@{}l l l r r r r@{}}", "\\toprule",
             "Scénario & Système & Résultat & Appels LLM & Outils & Tokens & Durée (s) \\\\ \\midrule"]
    for r in rows.values():
        a = r["agent"]
        lines.append(f"{tex(r['label'])} & Agent & {tex(a['status'])} & {a['llm_calls']} & {a['tool_calls']} & {a['tokens']} & {a['seconds']} \\\\")
        if "static" in r:
            s = r["static"]
            lines.append(f" & RAG statique & {'répond' if s['found'] else 'pas de réponse'} & {s['llm_calls']} & 1 & {s['tokens']} & {s['seconds']} \\\\")
        lines.append("\\addlinespace")
    lines += ["\\bottomrule", "\\end{tabular}"]
    (OUT / "comparaison.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ans = []
    for r in rows.values():
        if "static" in r:
            ans.append(f"\\paragraph{{{tex(r['label'])}.}} \\emph{{RAG statique}} : {tex(cut(r['static']['answer'], 420))} "
                       f"(sources : {tex(', '.join(r['static']['sources']) or 'aucune')})")
    (OUT / "comparaison_reponses.tex").write_text("\n\n".join(ans) + "\n", encoding="utf-8")
    print("comparaison.tex")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in sorted(TRACES.glob("scenario_*.json")):
        trace_table(f.stem.split("_", 1)[1])
    comparison()


if __name__ == "__main__":
    main()
