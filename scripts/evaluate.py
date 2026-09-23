"""Évaluation comparative des stratégies de prompting.

Même modèle, mêmes documents d'évaluation (split test de Re-DocRED), mêmes
paramètres de génération : seule la variable étudiée change (stratégie ou
température). Plusieurs seeds peuvent être utilisées pour mesurer la variance
quand la température est > 0.

Exemple :
  python -m scripts.evaluate --n-docs 30 \
      --strategies zero_shot context few_shot cot decompose \
      --temperatures 0 0.3 0.7 1.0 --seeds 1 2 3

Sorties dans results/<nom>/ : predictions.jsonl, summary.json, summary.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

from backend.dataset import doc_text, filter_relations, load_split, truncate
from backend.extraction import run_extraction
from backend.llm_client import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_PROVIDER, GenerationParams, LLMClient
from backend.metrics import aggregate, score_document
from backend.prompts import STRATEGIES
from backend.relations import resolve_relation_set

ROOT = Path(__file__).resolve().parent.parent
METRICS = ["precision", "recall", "f1", "json_valid_rate", "schema_valid_rate",
           "hallucination_rate", "off_schema_rate", "avg_pred", "avg_latency_s"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n-docs", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    p.add_argument("--temperatures", nargs="+", type=float, default=[0.0])
    p.add_argument("--seeds", nargs="+", type=int, default=[42])
    p.add_argument("--n-shots", type=int, default=3)
    p.add_argument("--relations", default="core", help="'core', 'all' ou liste séparée par des virgules")
    p.add_argument("--max-sents", type=int, default=None, help="tronquer les documents")
    p.add_argument("--top-p", type=float, default=0.9)
    p.add_argument("--top-k", type=int, default=40)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--no-json-mode", action="store_true", help="ne pas contraindre la sortie au JSON")
    p.add_argument("--provider", default=DEFAULT_PROVIDER)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--name", default=None, help="nom du dossier de résultats")
    p.add_argument("--resume", action="store_true",
                   help="reprendre une campagne interrompue (même --name) sans refaire les appels déjà faits")
    return p.parse_args()


def fmt(values: list[float]) -> str:
    m = statistics.mean(values)
    if len(values) > 1:
        return f"{m:.3f} ± {statistics.stdev(values):.3f}"
    return f"{m:.3f}"


def main() -> None:
    args = parse_args()
    codes = resolve_relation_set(args.relations)
    client = LLMClient(args.provider, args.base_url, args.model)
    health = client.health()
    if not health["ok"]:
        raise SystemExit(f"LLM indisponible ou modèle absent : {health}")

    docs = [filter_relations(truncate(d, args.max_sents), codes)
            for d in load_split("eval")[args.offset:args.offset + args.n_docs]]
    name = args.name or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    out_dir = ROOT / "results" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    config = {**vars(args), "relation_codes": codes, "doc_ids": [d["id"] for d in docs]}
    (out_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    pred_path = out_dir / "predictions.jsonl"
    cache: dict[tuple, dict] = {}
    if args.resume and pred_path.exists():
        for line in pred_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                if not rec.get("failed"):  # les appels en échec sont retentés
                    cache[(rec["doc_id"], rec["strategy"], rec["temperature"], rec["seed"])] = rec
        print(f"Reprise : {len(cache)} extraction(s) déjà faites.")

    runs: dict[tuple[str, float, int], dict] = {}
    total = len(args.strategies) * len(args.temperatures) * len(args.seeds) * len(docs)
    done, n_failed, t0 = 0, 0, time.time()
    with pred_path.open("w", encoding="utf-8") as fout:
        for strategy in args.strategies:
            for temp in args.temperatures:
                for seed in args.seeds:
                    params = GenerationParams(
                        temperature=temp, seed=seed, top_p=args.top_p, top_k=args.top_k,
                        max_tokens=args.max_tokens, json_mode=not args.no_json_mode)
                    rows_valid, rows_raw = [], []
                    for doc in docs:
                        key = (doc["id"], strategy, temp, seed)
                        if key in cache:
                            rec = cache[key]
                            rows_valid.append(rec["score"])
                            rows_raw.append(rec["score_raw"])
                            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            done += 1
                            continue
                        text = doc_text(doc)
                        failed = False
                        try:
                            res = run_extraction(client, text, strategy, params, codes, args.n_shots)
                        except Exception as exc:  # une erreur ne doit pas arrêter toute la campagne
                            failed = True
                            n_failed += 1
                            res = {"relations": [], "rejected": [], "json_valid": False,
                                   "schema_valid": False, "latency_s": 0.0, "errors": [str(exc)],
                                   "calls": []}
                        all_rel = res["relations"] + res["rejected"]
                        base = {k: res[k] for k in ("json_valid", "schema_valid", "latency_s")}
                        rv = {**score_document(res["relations"], all_rel, doc), **base}
                        rr = {**score_document(all_rel, all_rel, doc), **base}
                        rows_valid.append(rv)
                        rows_raw.append(rr)
                        fout.write(json.dumps({
                            "doc_id": doc["id"], "strategy": strategy, "temperature": temp,
                            "seed": seed, "score": rv, "score_raw": rr, "failed": failed,
                            "relations": res["relations"],
                            "rejected": res["rejected"], "errors": res["errors"],
                            "outputs": [c["output"] for c in res["calls"]],
                        }, ensure_ascii=False) + "\n")
                        fout.flush()
                        done += 1
                        eta = (time.time() - t0) / done * (total - done)
                        print(f"\r[{done}/{total}] {strategy} T={temp} seed={seed} "
                              f"(reste ~{eta / 60:.1f} min)   ", end="", flush=True)
                    runs[(strategy, temp, seed)] = {"validated": aggregate(rows_valid),
                                                    "raw": aggregate(rows_raw)}
    print()
    if n_failed:
        print(f"ATTENTION : {n_failed} appel(s) en échec (serveur LLM ?), comptés comme sorties "
              f"invalides. Relancez avec --resume --name {name} pour les retenter.")

    summary = []
    for strategy in args.strategies:
        for temp in args.temperatures:
            cell = [runs[(strategy, temp, s)] for s in args.seeds]
            entry = {"strategy": strategy, "temperature": temp}
            for m in METRICS:
                entry[m] = [c["validated"][m] for c in cell]
            entry["f1_raw"] = [c["raw"]["f1"] for c in cell]
            entry["precision_raw"] = [c["raw"]["precision"] for c in cell]
            summary.append(entry)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    header = ("| Stratégie | T | P | R | F1 | F1 sans validation | P sans validation | JSON valide "
              "| Schéma valide | Hallucinations | Hors schéma | Relations/doc | Latence (s) |")
    lines = [
        f"# Résultats — {name}",
        "",
        f"- Modèle : `{args.model}` ({args.provider}) — {len(docs)} documents "
        f"({docs[0]['id']} … {docs[-1]['id']}), relations : `{args.relations}` ({len(codes)})",
        f"- Paramètres fixes : top_p={args.top_p}, top_k={args.top_k}, max_tokens={args.max_tokens}, "
        f"json_mode={not args.no_json_mode}, n_shots={args.n_shots}, seeds={args.seeds}",
        "- P/R/F1 : micro-moyennes après validation (relations non ancrées, hors schéma et doublons retirées).",
        "- Hallucinations : part des relations brutes dont le sujet ou l'objet n'apparaît pas dans le texte.",
        "",
        header,
        "|" + "---|" * 13,
    ]
    for e in summary:
        lines.append("| " + " | ".join([
            e["strategy"], f"{e['temperature']:g}", fmt(e["precision"]), fmt(e["recall"]),
            fmt(e["f1"]), fmt(e["f1_raw"]), fmt(e["precision_raw"]), fmt(e["json_valid_rate"]),
            fmt(e["schema_valid_rate"]), fmt(e["hallucination_rate"]), fmt(e["off_schema_rate"]),
            fmt(e["avg_pred"]), fmt(e["avg_latency_s"]),
        ]) + " |")
    md = "\n".join(lines) + "\n"
    (out_dir / "summary.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"Résultats écrits dans {out_dir}")


if __name__ == "__main__":
    main()
