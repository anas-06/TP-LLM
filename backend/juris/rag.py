"""Services du RAG (TP2) : recherche d'affaires, résumé, question-réponse sur une affaire.

Les réponses du LLM sont contraintes aux passages fournis, numérotés [P1], [P2]…
Le code vérifie ensuite que chaque citation renvoie à un passage réellement fourni.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .corpus import DATA_DIR, SECTIONS, case_meta, case_sections, find_case, load_cases
from .index import get_index
from .llm import RAG_MODEL, Usage, llm_json

CACHE_PATH = DATA_DIR / "cache" / "summaries.json"
_cache_lock = threading.Lock()
NUMBER_RE = re.compile(r"\b\d{2}-\d{2}\.\d{3}\b")


def _words(text: str, n: int) -> str:
    w = text.split()
    return " ".join(w[:n]) + (" […]" if len(w) > n else "")


# ---------------------------------------------------------------- recherche
def search_cases(query: str, k: int = 10, mode: str = "hybrid", chamber: str | None = None,
                 solution: str | None = None) -> dict:
    """Recherche d'affaires : les passages sont regroupés par décision (meilleur passage)."""
    t0 = time.perf_counter()
    idx, cases = get_index(), load_cases()
    results: dict[str, dict] = {}

    # Un numéro de pourvoi dans la requête donne un accès direct à la décision.
    for num in NUMBER_RE.findall(query):
        c = find_case(num)
        if c:
            rows = idx.case_rows.get(c["id"], [])
            results[c["id"]] = {"score": 1.0, "passages": [idx.chunks[rows[0]]] if rows else [],
                                "exact": True}

    for i, score, detail in idx.search_chunks(query, k=k * 8, mode=mode):
        ch = idx.chunks[i]
        c = cases[ch["case_id"]]
        if chamber and c.get("chamber") != chamber:
            continue
        if solution and c.get("solution") != solution:
            continue
        r = results.setdefault(ch["case_id"], {"score": 0.0, "passages": [], "exact": False})
        r["score"] = max(r["score"], score) if not r["exact"] else r["score"]
        if len(r["passages"]) < 2:
            r["passages"].append({**ch, "scores": detail})

    ranked = sorted(results.items(), key=lambda x: -x[1]["score"])[:k]
    out = []
    for cid, r in ranked:
        meta = case_meta(cases[cid])
        out.append({**meta, "score": round(r["score"], 4), "exact_match": r["exact"],
                    "passages": [{"chunk_id": p["chunk_id"], "section": p["section"],
                                  "section_label": SECTIONS.get(p["section"], p["section"]),
                                  "text": p["text"], "scores": p.get("scores")}
                                 for p in r["passages"]]})
    return {"query": query, "mode": mode if idx.has_dense or mode == "bm25" else "bm25",
            "dense_available": idx.has_dense, "results": out,
            "latency_s": round(time.perf_counter() - t0, 3)}


def get_case(case_id: str) -> dict | None:
    c = find_case(case_id)
    if not c:
        return None
    return {**case_meta(c), "sections": case_sections(c)}


def case_passages(case_id: str, query: str, k: int = 4, mode: str = "hybrid") -> list[dict]:
    """Passages d'une affaire les plus proches d'une question (retrieval restreint à l'affaire)."""
    idx = get_index()
    rows = idx.case_rows.get(case_id, [])
    if not rows:
        return []
    hits = idx.search_chunks(query, k=k, mode=mode, rows=rows)
    if not hits:  # aucun mot commun : on prend le sommaire puis le début des motivations
        return [idx.chunks[r] for r in rows[:k]]
    return [{**idx.chunks[i], "scores": d} for i, _, d in hits]


# ---------------------------------------------------------------- résumé
SUMMARY_SYSTEM = """Tu es juriste. Tu résumes une décision de la Cour de cassation en français clair.
Règles :
- Utilise UNIQUEMENT les passages fournis. N'invente aucun fait, nom, date ou article.
- Après chaque information, cite le passage source entre crochets, par exemple [P2].
- Si une rubrique n'apparaît pas dans les passages, écris "Non précisé dans la décision."
Réponds en JSON : {"faits": "...", "procedure": "...", "question": "...", "decision": "...", "portee": "..."}
- faits : ce qui s'est passé entre les parties (2-3 phrases)
- procedure : les décisions des juges du fond et qui forme le pourvoi (1-2 phrases)
- question : la question de droit posée à la Cour (1 phrase)
- decision : la solution (rejet, cassation…) et son motif principal (2 phrases)
- portee : la règle de droit retenue, d'après le sommaire s'il existe (1 phrase)"""

SUMMARY_BUDGET = {"sommaire": 120, "introduction": 60, "expose": 260, "moyens": 160,
                  "motivations": 320, "dispositif": 90}


def _summary_passages(c: dict) -> list[dict]:
    passages = []
    if c.get("summary"):
        passages.append({"section": "sommaire", "text": _words(c["summary"], SUMMARY_BUDGET["sommaire"])})
    for sec in case_sections(c):
        budget = SUMMARY_BUDGET.get(sec["section"])
        if budget:
            txt = sec["text"]
            if sec["section"] == "motivations":  # la réponse de la Cour est souvent à la fin
                w = txt.split()
                txt = " ".join(w[-budget:]) if len(w) > budget else txt
                passages.append({"section": sec["section"], "text": ("[…] " if len(w) > budget else "") + txt})
            else:
                passages.append({"section": sec["section"], "text": _words(txt, budget)})
    for i, p in enumerate(passages, 1):
        p["pid"] = f"P{i}"
        p["section_label"] = SECTIONS.get(p["section"], p["section"])
    return passages


def _format_passages(passages: list[dict]) -> str:
    return "\n\n".join(f"[{p['pid']}] ({p['section_label']}) {p['text']}" for p in passages)


def _citations(text: str) -> set[str]:
    return set(re.findall(r"\[?(P\d+)\]?", text or ""))


def _as_text(value) -> str:
    """Le modèle renvoie parfois une liste de phrases au lieu d'un texte."""
    if isinstance(value, list):
        return " ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def _attribute(text: str, passages: list[dict], threshold: float = 0.5) -> tuple[str, bool]:
    """Ajoute [P#] après chaque phrase non citée, vers le passage qui partage le plus de ses mots."""
    from .index import tokenize
    vocab = {p["pid"]: set(tokenize(p["text"])) for p in passages}
    out, added = [], False
    # coupe aux fins de phrase, mais pas après « M. » (« M. [Y] »)
    for sent in re.split(r"(?<=[.!?])(?<!\bM\.)\s+(?=[A-ZÀ-Ý])", text):
        toks = set(tokenize(sent))
        if toks and not _citations(sent):
            pid, v = max(vocab.items(), key=lambda kv: len(toks & kv[1]))
            if len(toks & v) / len(toks) >= threshold:
                sent = sent.rstrip() + f" [{pid}]"
                added = True
        out.append(sent)
    return " ".join(out), added


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def summarize(case_id: str, refresh: bool = False, cached_only: bool = False) -> dict:
    c = find_case(case_id)
    if not c:
        raise KeyError(case_id)
    key = f"{c['id']}::{RAG_MODEL}"
    with _cache_lock:
        cache = _load_cache()
    if not refresh and key in cache:
        return {**cache[key], "cached": True}
    if cached_only:
        return {"case_id": c["id"], "summary": None, "cached": False}

    passages = _summary_passages(c)
    usage = Usage()
    t0 = time.perf_counter()
    user = f"Décision : {case_meta(c)['title']} — solution : {c.get('solution')}\n\n{_format_passages(passages)}"
    obj, raw = llm_json(SUMMARY_SYSTEM, user, purpose="résumé", usage=usage, max_tokens=700)
    fields = ["faits", "procedure", "question", "decision", "portee"]
    summary = {f: _as_text((obj or {}).get(f)) for f in fields}
    valid_ids = {p["pid"] for p in passages}
    cited = set().union(*(_citations(v) for v in summary.values()))
    uncited = [f for f in fields if summary[f] and not _citations(summary[f])
               and "non précisé" not in summary[f].lower()]
    # Le 3B oublie souvent les citations : le code rattache alors chaque phrase au passage
    # le plus proche (si le recouvrement est suffisant). C'est signalé dans `auto_cited_fields`.
    auto = []
    for f in uncited:
        summary[f], added = _attribute(summary[f], passages)
        if added:
            auto.append(f)
    checks = {
        "json_valid": obj is not None,
        "invalid_citations": sorted(cited - valid_ids),
        "uncited_fields": [f for f in uncited if f not in auto],
        "auto_cited_fields": auto,
    }
    result = {"case_id": c["id"], "summary": summary, "passages": passages, "checks": checks,
              "model": RAG_MODEL, "usage": usage.to_dict(), "raw": raw,
              "latency_s": round(time.perf_counter() - t0, 1), "cached": False}
    if obj is not None:
        with _cache_lock:
            cache = _load_cache()
            cache[key] = result
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


# ---------------------------------------------------------------- question-réponse
QA_SYSTEM = """Tu es un assistant juridique. Tu réponds à une question sur UNE décision de la Cour de cassation.
Règles :
- Réponds UNIQUEMENT à partir des passages fournis, en français, en 2 à 5 phrases.
- Cite les passages utilisés entre crochets, par exemple [P1], juste après l'information.
- Si les passages ne permettent pas de répondre, mets "found": false et explique ce qui manque.
- N'invente aucun nom, date, montant ni article absent des passages.
Réponds en JSON : {"answer": "...", "citations": ["P1"], "found": true}"""


def answer_question(case_id: str, question: str, history: list[dict] | None = None, k: int = 4) -> dict:
    c = find_case(case_id)
    if not c:
        raise KeyError(case_id)
    history = (history or [])[-2:]
    # La question précédente aide à interpréter une relance (« et pourquoi ? »).
    retrieval_query = " ".join([h.get("question", "") for h in history] + [question])
    passages = case_passages(c["id"], retrieval_query, k=k)
    for i, p in enumerate(passages, 1):
        p["pid"] = f"P{i}"
        p["section_label"] = SECTIONS.get(p["section"], p["section"])
    hist = "".join(f"Question précédente : {h.get('question')}\nRéponse : {h.get('answer')}\n\n" for h in history)
    user = (f"Décision : {case_meta(c)['title']} — solution : {c.get('solution')}\n\n"
            f"Passages :\n{_format_passages(passages)}\n\n{hist}Question : {question}")
    usage = Usage()
    t0 = time.perf_counter()
    obj, raw = llm_json(QA_SYSTEM, user, purpose="question-réponse", usage=usage, max_tokens=400)
    answer = str((obj or {}).get("answer") or "").strip()
    found = bool((obj or {}).get("found", True))
    valid = {p["pid"] for p in passages}
    cited = (_citations(answer) | {x for x in (obj or {}).get("citations", []) if isinstance(x, str)}) & valid
    invalid = sorted(_citations(answer) - valid)
    used = [p for p in passages if p["pid"] in cited]
    checks = {"json_valid": obj is not None, "invalid_citations": invalid,
              "grounded": bool(cited) or not found, "found": found}
    return {"case_id": c["id"], "question": question, "answer": answer or "Réponse invalide du modèle.",
            "found": found, "citations": sorted(cited, key=lambda x: int(x[1:])),
            "passages": [{k: p[k] for k in ("pid", "chunk_id", "section", "section_label", "text")} for p in passages],
            "used_passages": [p["pid"] for p in used], "checks": checks, "model": RAG_MODEL,
            "usage": usage.to_dict(), "raw": raw, "latency_s": round(time.perf_counter() - t0, 1)}


# ---------------------------------------------------------------- RAG statique (référence pour le TP3)
STATIC_SYSTEM = """Tu es un assistant juridique. Réponds à la question à partir des extraits de décisions
de la Cour de cassation fournis, en français, en 3 à 6 phrases.
- Cite les extraits utilisés entre crochets, par exemple [P1], et nomme la décision (numéro de pourvoi).
- N'utilise aucune information absente des extraits. Si les extraits ne suffisent pas, dis-le ("found": false).
Réponds en JSON : {"answer": "...", "citations": ["P1"], "found": true}"""


def static_rag(question: str, k: int = 5) -> dict:
    """Pipeline fixe : 1 recherche, k passages (2 max par décision), 1 génération."""
    t0 = time.perf_counter()
    idx, cases = get_index(), load_cases()
    passages, per_case = [], {}
    for i, _, d in idx.search_chunks(question, k=40):
        ch = idx.chunks[i]
        if per_case.get(ch["case_id"], 0) >= 2:
            continue
        per_case[ch["case_id"]] = per_case.get(ch["case_id"], 0) + 1
        passages.append({**ch, "title": case_meta(cases[ch["case_id"]])["title"]})
        if len(passages) >= k:
            break
    for i, p in enumerate(passages, 1):
        p["pid"] = f"P{i}"
        p["section_label"] = SECTIONS.get(p["section"], p["section"])
    ctx = "\n\n".join(f"[{p['pid']}] {p['title']} ({p['section_label']}) : {_words(p['text'], 150)}" for p in passages)
    usage = Usage()
    obj, raw = llm_json(STATIC_SYSTEM, f"Extraits :\n{ctx}\n\nQuestion : {question}",
                        purpose="rag statique", usage=usage, max_tokens=450)
    answer = str((obj or {}).get("answer") or "").strip()
    cited = _citations(answer) & {p["pid"] for p in passages}
    return {"question": question, "answer": answer, "found": bool((obj or {}).get("found", True)),
            "citations": sorted(cited, key=lambda x: int(x[1:])),
            "passages": [{k: p[k] for k in ("pid", "case_id", "title", "section_label", "text")} for p in passages],
            "usage": usage.to_dict(), "latency_s": round(time.perf_counter() - t0, 1), "model": RAG_MODEL}
