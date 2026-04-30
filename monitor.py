#!/usr/bin/env python3
"""
Read-only progress dashboard for a SutroAna optimization run.

Usage:
    python3 monitor.py <problem-path>            # one-shot snapshot
    python3 monitor.py <problem-path> --watch    # refresh every 10s
    python3 monitor.py --list [--root <dir>]     # list all problems
"""

import json, re, time, argparse, math
from pathlib import Path
from datetime import datetime


def resolve(p: str) -> Path:
    return Path(p).expanduser().resolve()


def load(problem_dir: Path) -> tuple[dict, dict]:
    cfg   = json.loads((problem_dir / "problem.json").read_text())
    state = json.loads((problem_dir / "directions.json").read_text())
    return cfg, state


def scan_records(problem_dir: Path, cfg: dict) -> list[dict]:
    pat = re.compile(cfg["record_cost_pattern"])
    records = []
    for p in problem_dir.glob(cfg.get("record_glob", "records/record_*.ir")):
        m = pat.search(p.name)
        if m:
            records.append({"cost": int(m.group(1)), "mtime": p.stat().st_mtime,
                            "name": p.name})
    return sorted(records, key=lambda r: r["mtime"])


def scan_experiments(problem_dir: Path, cfg: dict) -> list[dict]:
    exps = []
    for p in problem_dir.glob(cfg.get("experiment_glob", "exp_*.py")):
        exps.append({"name": p.stem, "mtime": p.stat().st_mtime, "size": p.stat().st_size})
    return sorted(exps, key=lambda e: e["mtime"])


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_delta(secs: float) -> str:
    return f"{secs:.0f}s" if secs < 60 else f"{secs/60:.1f}m"


def render(problem_dir: Path, cfg: dict, state: dict,
           records: list[dict], exps: list[dict]):
    now      = time.time()
    lower    = cfg.get("record_lower_is_better", True)
    baseline = cfg.get("baseline")
    record   = state.get("record")
    done     = sum(1 for d in state["directions"] if d["status"] == "done")
    active   = sum(1 for d in state["directions"] if d["status"] == "active")
    pending  = sum(1 for d in state["directions"] if d["status"] == "pending")

    stop_signal = problem_dir / "STOP_SIGNAL"

    print("\033[2J\033[H", end="")
    print("=" * 68)
    print(f"  {cfg['name'].upper()}  ·  {cfg.get('description','')}")
    print(f"  {datetime.now().strftime('%H:%M:%S')}  "
          f"Record: {record or '—'}  Baseline: {baseline or '—'}  "
          f"Dirs: {done}✅ {active}🔄 {pending}⬜")
    print("=" * 68)

    # Direction queue
    print("\n  DIRECTIONS")
    for d in state["directions"]:
        icon  = {"pending": "⬜", "active": "🔄", "done": "✅"}.get(d["status"], "?")
        score = f"{d['best_score']:,}" if d.get("best_score") else "—"
        print(f"  {icon}  {d['name']:<46}  {score:>10}")

    # Record timeline
    if records and baseline:
        print("\n  RECORD TIMELINE")
        print(f"  {'Time':>8}  {'Score':>9}  {'vs base':>9}  {'gap':>6}  Experiment")
        print("  " + "-" * 58)
        prev_mtime = records[0]["mtime"] - 1
        for r in records:
            gap   = fmt_delta(r["mtime"] - prev_mtime)
            exp   = next((e["name"] for e in reversed(exps)
                          if e["mtime"] <= r["mtime"]), "?")
            sign  = "-" if lower else "+"
            delta = abs(baseline - r["cost"])
            print(f"  {fmt_time(r['mtime']):>8}  {r['cost']:>9,}  "
                  f"{sign}{delta:>8,}  {gap:>6}  {exp}")
            prev_mtime = r["mtime"]

    # Stall / running status
    print()
    if records:
        last_ts    = records[-1]["mtime"]
        mins_since = (now - last_ts) / 60
        files_since = sum(1 for e in exps if e["mtime"] > last_ts)
        stall_mins = cfg.get("stall_minutes", 15)
        stall_files = cfg.get("stall_min_files", 3)
        stalled = mins_since >= stall_mins and files_since >= stall_files

        if stalled:
            print(f"  ⚠  STALLED — {mins_since:.0f}m since last record, "
                  f"{files_since} files written.")
            if stop_signal.exists():
                print(f"     STOP_SIGNAL present — explorer will halt soon.")
            else:
                print(f"     Run /optimize {problem_dir.name} to redirect.")
        else:
            print(f"  ✓  {mins_since:.1f}m since last record, "
                  f"{files_since} new files.")

    # Recent experiments
    if exps:
        print(f"\n  RECENT EXPERIMENTS")
        for e in sorted(exps, key=lambda x: x["mtime"], reverse=True)[:5]:
            print(f"  {fmt_time(e['mtime'])}  {e['name']:<36}"
                  f"  {e['size']/1024:>5.1f} KB  ({fmt_delta(now-e['mtime'])} ago)")
        run = now - exps[0]["mtime"]
        rate = len(exps) / run * 60 if run > 0 else 0
        print(f"  {len(exps)} total  ·  {rate:.1f}/min  ·  "
              f"elapsed {fmt_delta(run)}")

    nxt = next((d for d in state["directions"] if d["status"] == "pending"), None)
    print(f"\n  Next: {nxt['name'] if nxt else '(all directions exhausted)'}")
    print()
    print("=" * 68)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("problem",   nargs="?", help="path to problem folder")
    parser.add_argument("--root",    default=".", help="root for --list")
    parser.add_argument("--list",    action="store_true")
    parser.add_argument("--watch",   action="store_true")
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()

    if args.list:
        root = Path(args.root).expanduser().resolve()
        for p in sorted(root.iterdir()):
            if p.is_dir() and (p / "problem.json").exists():
                cfg = json.loads((p / "problem.json").read_text())
                print(f"  {p.name:<22} {cfg.get('description','')}")
        return

    if not args.problem:
        parser.print_help()
        return

    problem_dir = resolve(args.problem)
    while True:
        cfg, state = load(problem_dir)
        records    = scan_records(problem_dir, cfg)
        exps       = scan_experiments(problem_dir, cfg)
        render(problem_dir, cfg, state, records, exps)
        if not args.watch:
            break
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break


if __name__ == "__main__":
    main()
