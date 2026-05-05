#!/usr/bin/env python3
"""
Read-only progress dashboard for a SutroAna optimization run.

Usage:
    python3 monitor.py <problem-path>              # one-shot snapshot
    python3 monitor.py <problem-path> --watch      # refresh every 10s
    python3 monitor.py <problem-path> --tail       # stream events as they happen
    python3 monitor.py <problem-path> --oneline    # single status line (tmux/shell)
    python3 monitor.py --list [--root <dir>]       # list all problems
"""

import json, re, time, sys, argparse
from pathlib import Path
from datetime import datetime


def resolve(p: str) -> Path:
    return Path(p).expanduser().resolve()


def load_cfg(problem_dir: Path) -> dict:
    defaults = {
        "max_lanes": 2,
        "baseline": None,
        "plateau_sensitivity": 0.2,
        "token_budget": None,
        "record_lower_is_better": True,
        "stall_minutes": 15,
        "stall_min_files": 3,
        "record_glob": "records/record_*.ir",
        "record_cost_pattern": "record_(\\d+)",
        "experiment_glob": "exp_*.py",
    }
    cfg_path = problem_dir / "problem.json"
    if cfg_path.exists():
        defaults.update(json.loads(cfg_path.read_text()))
    return defaults


def load_state(problem_dir: Path) -> dict:
    p = problem_dir / "directions.json"
    if not p.exists():
        return {"record": None, "lanes": [], "improvement_history": [], "directions": []}
    return json.loads(p.read_text())


def scan_records(problem_dir: Path, cfg: dict) -> list[dict]:
    pat      = re.compile(cfg["record_cost_pattern"])
    lane_pat = re.compile(r"_lane(\d+)")
    records  = []
    for p in problem_dir.glob(cfg["record_glob"]):
        m = pat.search(p.name)
        if not m:
            continue
        lm   = lane_pat.search(p.name)
        lane = int(lm.group(1)) if lm else None
        records.append({"cost": int(m.group(1)), "mtime": p.stat().st_mtime,
                        "name": p.name, "lane": lane})
    return sorted(records, key=lambda r: r["mtime"])


def scan_experiments(problem_dir: Path, cfg: dict) -> list[dict]:
    exps = []
    for p in problem_dir.glob(cfg["experiment_glob"]):
        m    = re.match(r"exp_(\d+)_", p.stem)
        lane = int(m.group(1)) if m else None
        exps.append({"name": p.stem, "mtime": p.stat().st_mtime,
                     "size": p.stat().st_size, "lane": lane})
    return sorted(exps, key=lambda e: e["mtime"])


def read_tokens(problem_dir: Path) -> int:
    log = problem_dir / "token_log.jsonl"
    if not log.exists():
        return 0
    total = 0
    for line in log.read_text().splitlines():
        try:
            total += json.loads(line.strip()).get("tokens", 0)
        except Exception:
            pass
    return total


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_delta(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs/60:.1f}m"
    return f"{secs/3600:.1f}h"


def plateau_bar(score: float, width: int = 16) -> str:
    filled = int(round(min(score, 1.0) * width))
    return "█" * filled + "·" * (width - filled)


# ─── event formatting ─────────────────────────────────────────────────────────

# ANSI colour helpers
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
CYAN   = lambda t: _c("36", t)
DIM    = lambda t: _c("2",  t)
BOLD   = lambda t: _c("1",  t)


def format_event(ev: dict, baseline: int | None) -> str | None:
    ts     = fmt_time(ev.get("ts", time.time()))
    etype  = ev.get("type", "?")

    if etype == "new_record":
        cost  = ev["cost"]
        prev  = ev.get("prev")
        lane  = ev.get("lane")
        fname = ev.get("file", "")
        lane_tag = f"L{lane}" if lane is not None else "—"
        delta_prev = f"-{prev-cost:,}" if prev is not None else "first"
        delta_base = (f"  Δ{cost-baseline:+,} vs baseline"
                      if baseline is not None else "")
        return (f"{DIM(ts)}  {GREEN(BOLD('NEW RECORD'))}  "
                f"{cost:>9,}  ({lane_tag}, {fname})  "
                f"{CYAN(delta_prev)}{delta_base}")

    if etype == "stall":
        lid  = ev.get("lane_id", "?")
        mins = ev.get("minutes", "?")
        return f"{DIM(ts)}  {YELLOW('STALL')}       L{lid} — {mins}m since last record"

    if etype == "plateau":
        score = ev.get("score", "?")
        return f"{DIM(ts)}  {YELLOW('PLATEAU')}     score={score:.2f}  parallelizing"

    if etype == "spawn":
        lid   = ev.get("lane_id", "?")
        dname = ev.get("direction_name", ev.get("direction_id", "?"))
        why   = ev.get("reason", "")
        tag   = f"  ({why})" if why else ""
        return f"{DIM(ts)}  {GREEN('SPAWN')}       L{lid} → {dname}{tag}"

    if etype == "stop":
        lid = ev.get("lane_id", "?")
        why = ev.get("reason", "")
        tag = f"  ({why})" if why else ""
        return f"{DIM(ts)}  {YELLOW('STOP')}        L{lid}{tag}"

    if etype == "ideate":
        return f"{DIM(ts)}  {CYAN('IDEATE')}      generating new directions"

    if etype == "tick":
        sit    = ev.get("situation", "?")
        record = ev.get("record")
        score  = ev.get("plateau_score", "—")
        wakeup = ev.get("next_wakeup_minutes", "?")
        rec_str = f"{record:,}" if record else "—"
        return (f"{DIM(ts)}  {DIM('TICK')}        {sit:<10}  "
                f"record={rec_str}  plateau={score}  next={wakeup}m")

    return None


# ─── tail mode ────────────────────────────────────────────────────────────────


def tail_events(problem_dir: Path, cfg: dict, last: int = 20, interval: float = 0.5):
    """Stream events.jsonl — prints last N on start, then follows new lines."""
    log_path = problem_dir / "events.jsonl"
    baseline = cfg.get("baseline")

    # Print header
    print(BOLD(f"  {cfg.get('name', problem_dir.name).upper()}") +
          f"  — live event stream  (Ctrl-C to stop)")
    print(DIM("─" * 70))

    # Replay last N events
    lines = []
    if log_path.exists():
        lines = log_path.read_text().splitlines()
    for line in lines[-last:]:
        try:
            ev  = json.loads(line)
            msg = format_event(ev, baseline)
            if msg:
                print(msg)
        except Exception:
            pass

    # Follow
    offset = log_path.stat().st_size if log_path.exists() else 0
    try:
        while True:
            time.sleep(interval)
            if not log_path.exists():
                continue
            size = log_path.stat().st_size
            if size <= offset:
                continue
            with open(log_path) as f:
                f.seek(offset)
                new = f.read()
            offset = size
            for line in new.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev  = json.loads(line)
                    msg = format_event(ev, baseline)
                    if msg:
                        print(msg, flush=True)
                except Exception:
                    pass
    except KeyboardInterrupt:
        print(DIM("\nStream stopped."))


# ─── oneline mode ─────────────────────────────────────────────────────────────


def oneline(problem_dir: Path, cfg: dict, state: dict, records: list[dict]):
    """Print a single compact status line suitable for tmux or shell polling."""
    baseline  = cfg.get("baseline")
    record    = state.get("record")
    lanes     = state.get("lanes", [])
    dirs      = state.get("directions", [])
    tokens    = read_tokens(problem_dir)
    budget    = cfg.get("token_budget")

    name = cfg.get("name", problem_dir.name)

    # Record delta
    if record and baseline:
        pct = (baseline - record) / baseline * 100
        rec_str = f"rec={record:,} ({pct:.1f}%↓)"
    elif record:
        rec_str = f"rec={record:,}"
    else:
        rec_str = "rec=—"

    # Lane summaries
    lane_parts = []
    for lane in lanes:
        lid    = lane["id"]
        status = lane.get("status", "?")
        sig    = problem_dir / f"STOP_SIGNAL_{lid}"
        dname  = next((d["name"] for d in dirs
                       if d["id"] == lane.get("direction_id")), "—")
        icon   = {"active": "🔄", "idle": "💤", "done": "✅"}.get(status, "?")
        flag   = "⛔" if sig.exists() else ""
        short  = dname[:12] + ("…" if len(dname) > 12 else "")
        lane_parts.append(f"L{lid}:{short}{icon}{flag}")
    if not lane_parts:
        lane_parts = ["no lanes"]

    # Pending count
    pending = sum(1 for d in dirs if d["status"] == "pending")

    # Token budget
    tok_str = f"{tokens//1000}k tok"
    if budget:
        pct = tokens / budget * 100
        tok_str += f"/{budget//1000}k ({pct:.0f}%)"

    parts = [f"[{name}]", rec_str] + lane_parts + [f"{pending} pending", tok_str]
    print("  " + "  |  ".join(parts))


# ─── dashboard (watch) mode ───────────────────────────────────────────────────


def render(problem_dir: Path, cfg: dict, state: dict,
           records: list[dict], exps: list[dict]):
    now      = time.time()
    lower    = cfg.get("record_lower_is_better", True)
    baseline = cfg.get("baseline")
    record   = state.get("record")
    history  = state.get("improvement_history", [])
    lanes    = state.get("lanes", [])
    dirs     = state.get("directions", [])

    done_dirs    = [d for d in dirs if d["status"] == "done"]
    active_dirs  = [d for d in dirs if d["status"] == "active"]
    pending_dirs = [d for d in dirs if d["status"] == "pending"]

    # Plateau score
    plateau_score = 1.0
    if len(history) >= 3:
        h     = sorted(history, key=lambda x: x["ts"])
        rates = []
        for i in range(1, len(h)):
            dt = max((h[i]["ts"] - h[i-1]["ts"]) / 60, 0.1)
            dc = h[i-1]["cost"] - h[i]["cost"]
            rates.append(dc / dt)
        if len(rates) >= 2 and rates[:-1]:
            avg = sum(rates[:-1]) / len(rates[:-1])
            plateau_score = (rates[-1] / avg) if avg > 0 else 0.0

    tokens_used  = read_tokens(problem_dir)
    token_budget = cfg.get("token_budget")

    print("\033[2J\033[H", end="")
    print("=" * 70)
    print(f"  {cfg.get('name', problem_dir.name).upper()}  ·  {cfg.get('description','')[:44]}")
    print(f"  {datetime.now().strftime('%H:%M:%S')}  "
          f"Record: {record or '—'}  Baseline: {baseline or '—'}  "
          f"Dirs: {len(done_dirs)}✅ {len(active_dirs)}🔄 {len(pending_dirs)}⬜")
    print("=" * 70)

    # Lanes
    if lanes:
        print("\n  LANES")
        for lane in lanes:
            lid    = lane["id"]
            status = lane.get("status", "?")
            dname  = next((d["name"] for d in dirs
                           if d["id"] == lane.get("direction_id")), "—")
            sig    = problem_dir / f"STOP_SIGNAL_{lid}"
            flag   = "⛔ STOP_SIGNAL" if sig.exists() else ""
            lane_recs  = [r for r in records if r["lane"] == lid]
            last_score = lane_recs[-1]["cost"] if lane_recs else "—"
            icon   = {"active": "🔄", "idle": "💤", "done": "✅"}.get(status, "?")
            print(f"  {icon} Lane {lid}  {dname:<36} best={last_score}  {flag}")

    # Direction queue
    print("\n  DIRECTIONS")
    for d in dirs:
        icon  = {"pending": "⬜", "active": "🔄", "done": "✅"}.get(d["status"], "?")
        score = f"{d['best_score']:,}" if d.get("best_score") else "—"
        print(f"  {icon}  {d['name']:<46}  {score:>10}")

    # Improvement history / plateau
    if history:
        print(f"\n  IMPROVEMENT HISTORY  "
              f"plateau_score={plateau_score:.2f}  "
              f"{plateau_bar(plateau_score)}  "
              f"sensitivity={cfg['plateau_sensitivity']}")
        print(f"  {'Time':>8}  {'Score':>9}  {'delta':>8}")
        print("  " + "-" * 34)
        prev = None
        for h in sorted(history, key=lambda x: x["ts"]):
            delta = f"-{prev-h['cost']:,}" if prev else "—"
            print(f"  {fmt_time(h['ts']):>8}  {h['cost']:>9,}  {delta:>8}")
            prev = h["cost"]

    # Record timeline from files
    if records:
        print(f"\n  RECORD FILES")
        print(f"  {'Time':>8}  {'Score':>9}  {'Lane':>5}  {'vs base':>9}")
        print("  " + "-" * 40)
        for r in records[-10:]:
            vs = f"{r['cost']-baseline:+,}" if baseline else "—"
            print(f"  {fmt_time(r['mtime']):>8}  {r['cost']:>9,}  "
                  f"{'L'+str(r['lane']) if r['lane'] is not None else '—':>5}  {vs:>9}")

    # Recent experiments
    if exps:
        print(f"\n  RECENT EXPERIMENTS (last 6)")
        for e in sorted(exps, key=lambda x: x["mtime"], reverse=True)[:6]:
            lane_tag = f"L{e['lane']}" if e["lane"] is not None else "  "
            print(f"  {fmt_time(e['mtime'])}  [{lane_tag}] {e['name']:<36}"
                  f"  {e['size']/1024:>5.1f} KB  ({fmt_delta(now-e['mtime'])} ago)")
        run  = now - exps[0]["mtime"]
        rate = len(exps) / run * 60 if run > 0 else 0
        print(f"  {len(exps)} total  ·  {rate:.1f}/min  ·  elapsed {fmt_delta(run)}")

    # Token budget
    print(f"\n  TOKENS  {tokens_used:,} used / "
          f"{f'{token_budget:,}' if token_budget else 'unlimited'}")

    # Stall status
    if records:
        mins        = (now - records[-1]["mtime"]) / 60
        stall_mins  = cfg.get("stall_minutes", 15)
        exps_since  = sum(1 for e in exps if e["mtime"] > records[-1]["mtime"])
        stall_files = cfg.get("stall_min_files", 3)
        stalled     = mins >= stall_mins and exps_since >= stall_files
        print()
        if stalled:
            print(f"  ⚠  STALLED — {mins:.0f}m since last record, "
                  f"{exps_since} files written. Run /optimize to redirect.")
        elif plateau_score < cfg["plateau_sensitivity"]:
            print(f"  📉 PLATEAU — score {plateau_score:.2f} < {cfg['plateau_sensitivity']}. "
                  f"Manager will parallelize on next tick.")
        else:
            print(f"  ✓  {mins:.1f}m since last record.")

    nxt = next((d for d in dirs if d["status"] == "pending"), None)
    print(f"\n  Next pending: {nxt['name'] if nxt else '(none — all exhausted)'}")
    print()
    print("=" * 70)


# ─── main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("problem",    nargs="?")
    parser.add_argument("--root",     default=".")
    parser.add_argument("--list",     action="store_true")
    parser.add_argument("--watch",    action="store_true", help="refresh dashboard every N seconds")
    parser.add_argument("--tail",     action="store_true", help="stream events as they happen")
    parser.add_argument("--oneline",  action="store_true", help="single status line")
    parser.add_argument("--last",     type=int, default=20, help="lines of history shown by --tail")
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()

    if args.list:
        root = Path(args.root).expanduser().resolve()
        for p in sorted(root.iterdir()):
            if p.is_dir() and (p / "problem.json").exists():
                cfg = load_cfg(p)
                print(f"  {p.name:<22} {cfg.get('description','')}")
        return

    if not args.problem:
        parser.print_help()
        return

    problem_dir = resolve(args.problem)
    cfg = load_cfg(problem_dir)

    if args.tail:
        tail_events(problem_dir, cfg, last=args.last)
        return

    if args.oneline:
        state   = load_state(problem_dir)
        records = scan_records(problem_dir, cfg)
        oneline(problem_dir, cfg, state, records)
        return

    while True:
        state   = load_state(problem_dir)
        records = scan_records(problem_dir, cfg)
        exps    = scan_experiments(problem_dir, cfg)
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
