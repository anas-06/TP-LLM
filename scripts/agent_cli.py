"""Client en ligne de commande de l'agent : lance une question et affiche la trace en direct.

    python -m scripts.agent_cli "Ma question" [--url http://localhost:8000] [--no-external]
    python -m scripts.agent_cli --follow <run_id>
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def show(e: dict) -> None:
    if e["type"] == "NODE":
        return
    d = dict(e["data"])
    d.pop("options", None)
    m = d.pop("metrics", None)
    print(f"{e['t']:>7.1f}s  {e['type']:<12} {e['title']}")
    detail = json.dumps(d, ensure_ascii=False)
    if detail != "{}":
        print("           " + detail[:700])
    if m:
        print("           mesures : " + json.dumps({k: v for k, v in m.items() if k != "calls"}, ensure_ascii=False))
    sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--follow")
    ap.add_argument("--session", default="cli")
    ap.add_argument("--no-external", action="store_true")
    ap.add_argument("--max-iterations", type=int, default=5)
    args = ap.parse_args()
    with httpx.Client(timeout=None) as http:
        run_id = args.follow
        if not run_id:
            r = http.post(f"{args.url}/api/agent/run", json={
                "question": args.question, "session_id": args.session,
                "use_external": not args.no_external, "max_iterations": args.max_iterations})
            r.raise_for_status()
            run_id = r.json()["run_id"]
        print(f"run {run_id}")
        with http.stream("GET", f"{args.url}/api/agent/stream/{run_id}") as s:
            for line in s.iter_lines():
                if line.startswith("data: ") and line != "data: {}":
                    show(json.loads(line[6:]))


if __name__ == "__main__":
    main()
