"""Outils de l'agent. Chaque outil renvoie une observation courte (pour le LLM et la trace)
et des preuves (passages citables) ajoutées à l'état partagé.

- search_cases(query, k) : recherche hybride dans l'index du TP2 ;
- read_case(case_id)     : passages d'une décision (sommaire, passages proches de la question, dispositif) ;
- external_search(query) : API externe (Wikipédia en français), source clairement marquée « externe ».
"""

from __future__ import annotations

import os
import re

import httpx

from ..juris.corpus import SECTIONS, case_meta, find_case
from ..juris.index import get_index
from ..juris.rag import search_cases as rag_search

WIKI_API = os.getenv("EXTERNAL_API", "https://fr.wikipedia.org/w/api.php")
USER_AGENT = os.getenv("EXTERNAL_USER_AGENT", "TPAgenticRAG/1.0 (https://github.com/anas-06/TP-LLM; projet etudiant) python-httpx")
HTTP_TIMEOUT = float(os.getenv("EXTERNAL_TIMEOUT", "12"))
EVIDENCE_WORDS = 160
# Vocabulaire des arrêts de cassation, ajouté à la question pour cibler les bons passages
# (une question dit « reprocher », l'arrêt dit « fait grief à l'arrêt »).
LEGAL_EXPANSION = "grief moyen réponse de la Cour rejette casse annule pourvoi"


class ToolError(RuntimeError):
    """Erreur d'outil contrôlée : affichée dans la trace, l'agent peut continuer."""


def _cut(text: str, n: int = EVIDENCE_WORDS) -> str:
    w = text.split()
    return " ".join(w[:n]) + (" […]" if len(w) > n else "")


def tool_search_cases(query: str, k: int = 5) -> dict:
    query = (query or "").strip()
    if len(query) < 3:
        raise ToolError("requête vide ou trop courte")
    k = max(1, min(int(k or 5), 8))
    res = rag_search(query, k=k)
    results, evidence = [], []
    for rank, r in enumerate(res["results"]):
        best = r["passages"][0] if r["passages"] else None
        results.append({"case_id": r["case_id"], "title": r["title"], "solution": r["solution"],
                        "themes": r["themes"][:4], "score": r["score"], "exact": r["exact_match"],
                        "snippet": _cut(best["text"], 40) if best else ""})
        # Les 3 premiers résultats fournissent déjà un passage citable ; pour les 2 premiers on ajoute
        # le sommaire officiel, qui énonce la règle de droit (suffit souvent pour une question de principe).
        if best and rank < 3:
            evidence.append({"source": "corpus", "case_id": r["case_id"], "title": r["title"],
                             "chunk_id": best["chunk_id"], "section_label": best["section_label"],
                             "text": _cut(best["text"]), "tool": "search_cases"})
        if rank < 2 and r.get("summary") and (not best or best["section"] != "sommaire"):
            rows = get_index().case_rows.get(r["case_id"], [])
            som = next((get_index().chunks[i] for i in rows if get_index().chunks[i]["section"] == "sommaire"), None)
            if som:
                evidence.append({"source": "corpus", "case_id": r["case_id"], "title": r["title"],
                                 "chunk_id": som["chunk_id"], "section_label": SECTIONS["sommaire"],
                                 "text": _cut(som["text"]), "tool": "search_cases"})
    return {"results": results, "evidence": evidence, "mode": res["mode"],
            "summary": f"{len(results)} décision(s) trouvée(s)" + (
                " ; meilleure : " + results[0]["title"] if results else " — aucun résultat")}


def tool_read_case(case_id: str, question: str) -> dict:
    c = find_case(case_id)
    if not c:
        raise ToolError(f"décision introuvable : {case_id!r}")
    idx = get_index()
    rows = idx.case_rows.get(c["id"], [])
    chunks = [idx.chunks[r] for r in rows]
    # Lecture « juridique » : un passage par rôle (faits, grief du pourvoi, réponse de la Cour,
    # dispositif), en prenant dans chaque rôle le passage le plus proche de la question.
    picked: list[dict] = []
    if c.get("summary"):  # sommaire officiel (règle de droit), s'il existe
        picked += [ch for ch in chunks if ch["section"] == "sommaire"][:1]
    query = question + " " + LEGAL_EXPANSION
    for section in ("expose", "moyens", "motivations", "dispositif"):
        sec_rows = [r for r in rows if idx.chunks[r]["section"] == section]
        if not sec_rows:
            continue
        hits = idx.search_chunks(query, k=1, mode="bm25", rows=sec_rows)
        best = idx.chunks[hits[0][0]] if hits else idx.chunks[sec_rows[-1 if section == "motivations" else 0]]
        picked.append(best)
    meta = case_meta(c)
    evidence = [{"source": "corpus", "case_id": c["id"], "title": meta["title"], "chunk_id": ch["chunk_id"],
                 "section_label": SECTIONS.get(ch["section"], ch["section"]), "text": _cut(ch["text"]),
                 "tool": "read_case"} for ch in picked]
    sections = ", ".join(e["section_label"] for e in evidence)
    return {"case": {"case_id": c["id"], "title": meta["title"], "solution": meta["solution"]},
            "evidence": evidence,
            "summary": f"{meta['title']} ({meta['solution']}) : {len(evidence)} passage(s) lus — {sections}"}


def tool_external_search(query: str) -> dict:
    query = (query or "").strip()
    if len(query) < 3:
        raise ToolError("requête externe vide")
    headers = {"User-Agent": USER_AGENT}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, headers=headers) as http:
            s = http.get(WIKI_API, params={"action": "query", "list": "search", "srsearch": query,
                                           "srlimit": 2, "format": "json"})
            s.raise_for_status()
            hits = s.json().get("query", {}).get("search", [])
            if not hits:
                return {"evidence": [], "results": [], "summary": "API externe : aucun résultat"}
            ids = "|".join(str(h["pageid"]) for h in hits)
            e = http.get(WIKI_API, params={"action": "query", "prop": "extracts|info", "inprop": "url",
                                           "exintro": 1, "explaintext": 1, "pageids": ids, "format": "json"})
            e.raise_for_status()
            pages = e.json().get("query", {}).get("pages", {})
    except httpx.HTTPError as exc:
        raise ToolError(f"API externe indisponible : {exc}") from exc
    evidence, results = [], []
    for h in hits:
        p = pages.get(str(h["pageid"]), {})
        text = re.sub(r"\s+", " ", p.get("extract") or "").strip()
        if not text:
            continue
        url = p.get("fullurl") or f"https://fr.wikipedia.org/?curid={h['pageid']}"
        results.append({"title": p.get("title"), "url": url})
        evidence.append({"source": "external", "title": f"Wikipédia — {p.get('title')}", "url": url,
                         "section_label": "Encyclopédie (source externe)", "text": _cut(text, 120),
                         "tool": "external_search"})
    return {"evidence": evidence, "results": results,
            "summary": f"API externe : {len(evidence)} article(s) — " + ", ".join(r["title"] for r in results)}


TOOLS = {
    "search_cases": "Rechercher des décisions dans le corpus (index hybride BM25 + embeddings du TP2)",
    "read_case": "Lire les passages clés d'une décision (sommaire, passages pertinents, dispositif)",
    "external_search": "Chercher une définition générale dans une source externe (Wikipédia FR)",
}
