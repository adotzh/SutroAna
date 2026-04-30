#!/usr/bin/env python3
"""
Thin state utility for SutroAna optimization loops.
Orchestration logic lives in .claude/skills/optimize/SKILL.md — invoke with /optimize <problem-path>.

Usage:
    python3 manager.py --list [--root <problems-dir>]
    python3 manager.py <problem-path> --status
    python3 manager.py <problem-path> --finish <direction-id> --score <cost>
    python3 manager.py <problem-path> --stop

<problem-path> can be absolute or relative (e.g. ../sutro-problems/matmul).
"""

import json, re, sys, time, argparse
from pathlib import Path


def resolve(problem_path: str) -> Path:
    p = Path(problem_path).expanduser().resolve()
    if not p.is_dir():
        sys.exit(f"Not a directory: {p}")
    return p


def load(problem_dir: Path) -> tuple[dict, dict]:
    cfg_path   = problem_dir / "problem.json"
    state_path = problem_dir / "directions.json"
    if not cfg_path.exists():
        sys.exit(f"No problem.json in {problem_dir}")
    if not state_path.exists():
        sys.exit(f"No directions.json in {problem_dir}")
    cfg   = json.loads(cfg_path.read_text())
    state = json.loads(state_path.read_text())
    return cfg, state


def save_state(problem_dir: Path, state: dict):
    (problem_dir / "directions.json").write_text(json.dumps(state, indent=2) + "\n")


def scan_records(problem_dir: Path, cfg: dict) -> list[dict]:
    pat = re.compile(cfg["record_cost_pattern"])
    records = []
    for p in problem_dir.glob(cfg.get("record_glob", "records/record_*.ir")):
        m = pat.search(p.name)
        if m:
            records.append({"cost": int(m.group(1)), "mtime": p.stat().st_mtime})
    return sorted(records, key=lambda r: r["mtime"])


def list_problems(root: Path):
    found = sorted(p for p in root.iterdir()
                   if p.is_dir() and (p / "problem.json").exists())
    if not found:
        print(f"No problems found in {root}")
        return
    for p in found:
        cfg = json.loads((p / "problem.json").read_text())
        print(f"  {p.name:<22} {cfg.get('description', '')}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("problem",  nargs="?", help="path to problem folder")
    parser.add_argument("--root",   default=".", help="root dir for --list (default: .)")
    parser.add_argument("--list",   action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop",   action="store_true")
    parser.add_argument("--finish", metavar="ID")
    parser.add_argument("--score",  type=int)
    args = parser.parse_args()

    if args.list:
        list_problems(Path(args.root).expanduser().resolve())
        return

    if not args.problem:
        parser.print_help()
        return

    problem_dir  = resolve(args.problem)
    cfg, state   = load(problem_dir)
    stop_signal  = problem_dir / "STOP_SIGNAL"

    if args.stop:
        stop_signal.write_text("stop")
        print(f"STOP_SIGNAL written → {stop_signal}")
        return

    if args.finish:
        for d in state["directions"]:
            if d["id"] == args.finish:
                d["status"] = "done"
                if args.score is not None:
                    d["best_score"] = args.score
        save_state(problem_dir, state)
        score_str = f" (score={args.score:,})" if args.score else ""
        print(f"Marked {args.finish!r} done{score_str}")
        return

    if args.status:
        records = scan_records(problem_dir, cfg)
        best    = min((r["cost"] for r in records), default=state.get("record"))
        done    = [d for d in state["directions"] if d["status"] == "done"]
        active  = [d for d in state["directions"] if d["status"] == "active"]
        pending = [d for d in state["directions"] if d["status"] == "pending"]
        print(f"Problem  : {cfg['name']}")
        print(f"Record   : {best or '—'}")
        print(f"Baseline : {cfg.get('baseline', '—')}")
        print(f"Progress : {len(done)} done / {len(active)} active / {len(pending)} pending")
        if active and records:
            mins = (time.time() - records[-1]["mtime"]) / 60
            print(f"Active   : {active[0]['name']}  ({mins:.0f}m since last record)")
        nxt = next((d for d in state["directions"] if d["status"] == "pending"), None)
        if nxt:
            print(f"Next     : {nxt['name']}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
