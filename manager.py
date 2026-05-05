#!/usr/bin/env python3
"""
Agentic optimization harness — manager.

Usage:
    python3 manager.py <problem-path> --tick
    python3 manager.py <problem-path> --status
    python3 manager.py <problem-path> --finish <id> --score <cost>
    python3 manager.py <problem-path> --stop [--lane <n>]
    python3 manager.py --list [--root <dir>]
"""

import json
import re
import sys
import time
import argparse
from pathlib import Path

# ─── defaults ────────────────────────────────────────────────────────────────

CFG_DEFAULTS = {
    "max_lanes": 2,
    "manager_cadence_min": 10,
    "plateau_sensitivity": 0.2,
    "token_budget": None,
    "record_lower_is_better": True,
    "stall_minutes": 15,
    "stall_min_files": 3,
    "record_glob": "records/record_*.ir",
    "record_cost_pattern": "record_(\\d+)",
    "experiment_glob": "exp_*.py",
}

# ─── helpers ─────────────────────────────────────────────────────────────────


def resolve(problem_path: str) -> Path:
    p = Path(problem_path).expanduser().resolve()
    if not p.is_dir():
        sys.exit(f"Not a directory: {p}")
    return p


def load_cfg(problem_dir: Path) -> dict:
    cfg_path = problem_dir / "problem.json"
    if not cfg_path.exists():
        return dict(CFG_DEFAULTS)
    raw = json.loads(cfg_path.read_text())
    merged = dict(CFG_DEFAULTS)
    merged.update(raw)
    return merged


def load_state(problem_dir: Path) -> dict:
    state_path = problem_dir / "directions.json"
    if not state_path.exists():
        return {"record": None, "lanes": [], "improvement_history": [], "directions": []}
    return json.loads(state_path.read_text())


def save_state(problem_dir: Path, state: dict):
    (problem_dir / "directions.json").write_text(json.dumps(state, indent=2) + "\n")


# ─── record scanning ─────────────────────────────────────────────────────────


def scan_records(problem_dir: Path, cfg: dict) -> list[dict]:
    """
    Returns sorted list of record dicts.
    Each dict has: cost, mtime, name, lane (int or None).
    """
    pat = re.compile(cfg["record_cost_pattern"])
    lane_pat = re.compile(r"_lane(\d+)")
    records = []
    for p in problem_dir.glob(cfg["record_glob"]):
        m = pat.search(p.name)
        if not m:
            continue
        lm = lane_pat.search(p.name)
        lane = int(lm.group(1)) if lm else None
        records.append({
            "cost": int(m.group(1)),
            "mtime": p.stat().st_mtime,
            "name": p.name,
            "lane": lane,
        })
    return sorted(records, key=lambda r: r["mtime"])


# ─── experiment scanning ──────────────────────────────────────────────────────


def scan_experiments(problem_dir: Path, cfg: dict) -> list[dict]:
    """
    Returns sorted list of experiment dicts.
    Each dict has: name, mtime, lane (int or None).
    Lane is parsed from prefix exp_{lane_id}_...
    """
    exps = []
    for p in problem_dir.glob(cfg["experiment_glob"]):
        m = re.match(r"exp_(\d+)_", p.stem)
        lane = int(m.group(1)) if m else None
        exps.append({"name": p.stem, "mtime": p.stat().st_mtime, "lane": lane})
    return sorted(exps, key=lambda e: e["mtime"])


# ─── event log ───────────────────────────────────────────────────────────────


def append_event(problem_dir: Path, event: dict):
    """Append one structured event to events.jsonl."""
    event.setdefault("ts", time.time())
    with open(problem_dir / "events.jsonl", "a") as f:
        f.write(json.dumps(event) + "\n")


# ─── token tracking ───────────────────────────────────────────────────────────


def read_tokens_used(problem_dir: Path) -> int:
    log_path = problem_dir / "token_log.jsonl"
    if not log_path.exists():
        return 0
    total = 0
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            total += json.loads(line).get("tokens", 0)
        except json.JSONDecodeError:
            pass
    return total


# ─── plateau detection ────────────────────────────────────────────────────────


def compute_plateau(improvement_history: list[dict], sensitivity: float) -> tuple[bool, float]:
    """
    Compares last interval rate vs average of all earlier interval rates.
    Returns (is_plateau: bool, score: float).
    Score near 0 = plateau, near 1 = improving well.
    Needs >= 3 history entries to fire.
    """
    if len(improvement_history) < 3:
        return False, 1.0
    hist = sorted(improvement_history, key=lambda h: h["ts"])
    rates = []
    for i in range(1, len(hist)):
        dt = max((hist[i]["ts"] - hist[i - 1]["ts"]) / 60, 0.1)
        dc = hist[i - 1]["cost"] - hist[i]["cost"]  # positive = improvement
        rates.append(dc / dt)
    if len(rates) < 2:
        return False, 1.0
    last_rate = rates[-1]
    avg_earlier = sum(rates[:-1]) / len(rates[:-1])
    if avg_earlier <= 0:
        return True, 0.0
    score = last_rate / avg_earlier
    return score < sensitivity, round(score, 3)


# ─── stall detection ──────────────────────────────────────────────────────────


def is_lane_stalled(lane_id: int, lane_entry: dict, records: list[dict],
                    exps: list[dict], cfg: dict) -> bool:
    """
    A lane is stalled when BOTH:
    - Minutes since last record (for that lane) OR last experiment file for that lane
      >= stall_minutes
    - Experiment files written for that lane since last record >= stall_min_files
    """
    stall_minutes = cfg["stall_minutes"]
    stall_min_files = cfg["stall_min_files"]
    now = time.time()

    # Records for this lane
    lane_records = [r for r in records if r["lane"] == lane_id or r["lane"] is None]
    last_record_ts = max((r["mtime"] for r in lane_records), default=0.0)

    # Experiments for this lane
    lane_exps = [e for e in exps if e["lane"] == lane_id]
    last_exp_ts = max((e["mtime"] for e in lane_exps), default=0.0)

    last_activity_ts = max(last_record_ts, last_exp_ts)
    if last_activity_ts == 0.0:
        # Lane has never written anything — use started_at
        last_activity_ts = lane_entry.get("started_at", now)

    minutes_since = (now - last_activity_ts) / 60

    # Experiments written since last record
    exps_since_record = sum(1 for e in lane_exps if e["mtime"] > last_record_ts)

    return minutes_since >= stall_minutes and exps_since_record >= stall_min_files


# ─── state sync ───────────────────────────────────────────────────────────────


def sync_improvement_history(problem_dir: Path, state: dict,
                             records: list[dict], lower_is_better: bool) -> dict:
    """Add new best records to improvement_history; emit new_record events."""
    history = state.setdefault("improvement_history", [])
    existing_ts = {h["ts"] for h in history}
    current_best = state.get("record")

    for r in records:
        if r["mtime"] in existing_ts:
            continue
        cost = r["cost"]
        if current_best is None:
            is_best = True
        elif lower_is_better:
            is_best = cost < current_best
        else:
            is_best = cost > current_best

        if is_best:
            prev = current_best
            current_best = cost
            history.append({"ts": r["mtime"], "cost": cost})
            existing_ts.add(r["mtime"])
            append_event(problem_dir, {
                "type": "new_record",
                "ts": r["mtime"],
                "cost": cost,
                "prev": prev,
                "lane": r["lane"],
                "file": r["name"],
            })

    state["record"] = current_best
    return state


# ─── decide ──────────────────────────────────────────────────────────────────


def decide(problem_dir: Path, cfg: dict, state: dict) -> dict:
    records = scan_records(problem_dir, cfg)
    exps = scan_experiments(problem_dir, cfg)
    lower = cfg["record_lower_is_better"]

    # 1. Sync improvement history
    state = sync_improvement_history(problem_dir, state, records, lower)

    # 2. Plateau detection
    is_plateau, plateau_score = compute_plateau(
        state["improvement_history"], cfg["plateau_sensitivity"]
    )

    # 3. Token budget
    tokens_used = read_tokens_used(problem_dir)
    token_budget = cfg["token_budget"]
    budget_exceeded = token_budget is not None and tokens_used >= token_budget

    # 4. Lane bookkeeping
    lanes = state.setdefault("lanes", [])
    directions = state.get("directions", [])

    active_lanes = [l for l in lanes if l["status"] == "active"]
    pending_dirs = [d for d in directions if d["status"] == "pending"]
    all_done = all(d["status"] == "done" for d in directions) if directions else False

    actions = []
    max_lanes = cfg["max_lanes"]

    # 5. Find stalled lanes
    stalled_lane_ids = []
    for lane_entry in active_lanes:
        if is_lane_stalled(lane_entry["id"], lane_entry, records, exps, cfg):
            stalled_lane_ids.append(lane_entry["id"])
            lane_recs = [r for r in records if r["lane"] == lane_entry["id"]]
            last_rec_ts = max((r["mtime"] for r in lane_recs), default=lane_entry.get("started_at", time.time()))
            mins = (time.time() - last_rec_ts) / 60
            append_event(problem_dir, {"type": "stall", "lane_id": lane_entry["id"],
                                       "minutes": round(mins, 1)})

    if is_plateau:
        append_event(problem_dir, {"type": "plateau", "score": plateau_score})

    # Emit stop + respawn for stalled lanes
    used_dir_ids = set()
    for lid in stalled_lane_ids:
        actions.append({"type": "stop_lane", "lane_id": lid})
        append_event(problem_dir, {"type": "stop", "lane_id": lid, "reason": "stall"})
        if pending_dirs and not budget_exceeded:
            next_dir = next((d for d in pending_dirs if d["id"] not in used_dir_ids), None)
            if next_dir:
                used_dir_ids.add(next_dir["id"])
                is_new = not any(l["id"] == lid for l in lanes)
                actions.append({
                    "type": "spawn_agent",
                    "lane_id": lid,
                    "direction_id": next_dir["id"],
                    "is_new_lane": is_new,
                })
                append_event(problem_dir, {"type": "spawn", "lane_id": lid,
                                           "direction_id": next_dir["id"],
                                           "direction_name": next_dir.get("name", next_dir["id"])})

    # 6. If plateau and room for more lanes and pending dirs
    current_active_count = len(active_lanes)
    if (is_plateau and current_active_count < max_lanes
            and pending_dirs and not budget_exceeded):
        remaining_pending = [d for d in pending_dirs if d["id"] not in used_dir_ids]
        for next_dir in remaining_pending:
            if current_active_count >= max_lanes:
                break
            used_ids = {l["id"] for l in lanes}
            new_lane_id = next(i for i in range(max_lanes) if i not in used_ids)
            used_dir_ids.add(next_dir["id"])
            actions.append({
                "type": "spawn_agent",
                "lane_id": new_lane_id,
                "direction_id": next_dir["id"],
                "is_new_lane": True,
            })
            append_event(problem_dir, {"type": "spawn", "lane_id": new_lane_id,
                                       "direction_id": next_dir["id"],
                                       "direction_name": next_dir.get("name", next_dir["id"]),
                                       "reason": "plateau"})
            current_active_count += 1

    # 7. No active lanes at all — bootstrap lane 0
    if not active_lanes and not any(a["type"] == "spawn_agent" for a in actions):
        if pending_dirs and not budget_exceeded:
            next_dir = next((d for d in pending_dirs if d["id"] not in used_dir_ids), None)
            if next_dir:
                actions.append({
                    "type": "spawn_agent",
                    "lane_id": 0,
                    "direction_id": next_dir["id"],
                    "is_new_lane": True,
                })
                append_event(problem_dir, {"type": "spawn", "lane_id": 0,
                                           "direction_id": next_dir["id"],
                                           "direction_name": next_dir.get("name", next_dir["id"]),
                                           "reason": "bootstrap"})

    # 8. If no pending dirs and everything stalled → ideate
    if not pending_dirs and (all_done or not active_lanes or stalled_lane_ids):
        actions.append({"type": "ideate"})
        append_event(problem_dir, {"type": "ideate"})

    # 9. Determine situation + next wakeup
    if not directions:
        situation = "bootstrap"
        next_wakeup = cfg["manager_cadence_min"]
    elif any(a["type"] == "ideate" for a in actions):
        situation = "exhausted"
        next_wakeup = cfg["manager_cadence_min"]
    elif is_plateau:
        situation = "plateau"
        next_wakeup = cfg["manager_cadence_min"] // 2 or 5
    elif stalled_lane_ids:
        situation = "stalled"
        next_wakeup = cfg["manager_cadence_min"] // 2 or 5
    else:
        situation = "running"
        next_wakeup = cfg["manager_cadence_min"]

    next_wakeup = max(5, next_wakeup)

    result = {
        "actions": actions,
        "situation": situation,
        "next_wakeup_minutes": next_wakeup,
        "metrics": {
            "record": state.get("record"),
            "is_plateau": is_plateau,
            "plateau_score": plateau_score,
            "active_lanes": len(active_lanes),
            "pending_directions": len(pending_dirs),
            "tokens_used": tokens_used,
            "token_budget": token_budget,
        },
    }
    append_event(problem_dir, {"type": "tick", "situation": situation,
                                "record": state.get("record"),
                                "plateau_score": plateau_score,
                                "next_wakeup_minutes": next_wakeup})
    return result


# ─── list problems ────────────────────────────────────────────────────────────


def list_problems(root: Path):
    found = sorted(p for p in root.iterdir()
                   if p.is_dir() and (p / "problem.json").exists())
    if not found:
        print(f"No problems found in {root}")
        return
    for p in found:
        cfg = load_cfg(p)
        print(f"  {p.name:<22} {cfg.get('description', '')}")


# ─── status ───────────────────────────────────────────────────────────────────


def print_status(problem_dir: Path, cfg: dict, state: dict):
    records = scan_records(problem_dir, cfg)
    lower = cfg["record_lower_is_better"]

    all_costs = [r["cost"] for r in records]
    if all_costs:
        best = min(all_costs) if lower else max(all_costs)
    else:
        best = state.get("record")

    directions = state.get("directions", [])
    done = [d for d in directions if d["status"] == "done"]
    active = [d for d in directions if d["status"] == "active"]
    pending = [d for d in directions if d["status"] == "pending"]

    lanes = state.get("lanes", [])
    is_plateau, plateau_score = compute_plateau(
        state.get("improvement_history", []), cfg["plateau_sensitivity"]
    )
    tokens_used = read_tokens_used(problem_dir)

    print(f"Problem   : {cfg.get('name', problem_dir.name)}")
    print(f"Record    : {best or '—'}")
    print(f"Baseline  : {cfg.get('baseline', '—')}")
    print(f"Progress  : {len(done)} done / {len(active)} active / {len(pending)} pending")
    print(f"Plateau   : {is_plateau}  (score={plateau_score})")
    print(f"Tokens    : {tokens_used:,} / {cfg['token_budget'] or 'unlimited'}")
    print(f"Lanes     : {len(lanes)} registered, {len(active)} active")
    if records:
        mins = (time.time() - records[-1]["mtime"]) / 60
        print(f"Last rec  : {mins:.0f}m ago")
    nxt = next((d for d in directions if d["status"] == "pending"), None)
    if nxt:
        print(f"Next dir  : {nxt['name']}")


# ─── main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="SutroAna optimization manager")
    parser.add_argument("problem", nargs="?", help="path to problem folder")
    parser.add_argument("--root", default=".", help="root dir for --list")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--tick", action="store_true", help="print JSON action plan")
    parser.add_argument("--status", action="store_true", help="human-readable status")
    parser.add_argument("--finish", metavar="ID", help="mark direction done")
    parser.add_argument("--score", type=int, help="score for --finish")
    parser.add_argument("--stop", action="store_true", help="write STOP_SIGNAL")
    parser.add_argument("--lane", type=int, default=None, help="lane id for --stop")
    args = parser.parse_args()

    if args.list:
        list_problems(Path(args.root).expanduser().resolve())
        return

    if not args.problem:
        parser.print_help()
        return

    problem_dir = resolve(args.problem)

    if args.stop:
        if args.lane is not None:
            sig = problem_dir / f"STOP_SIGNAL_{args.lane}"
        else:
            sig = problem_dir / "STOP_SIGNAL"
        sig.write_text("stop")
        print(f"Written: {sig}")
        return

    cfg = load_cfg(problem_dir)
    state = load_state(problem_dir)

    if args.finish:
        for d in state.get("directions", []):
            if d["id"] == args.finish:
                d["status"] = "done"
                if args.score is not None:
                    d["best_score"] = args.score
        # Also mark lane as inactive if it was running this direction
        for lane in state.get("lanes", []):
            if lane.get("direction_id") == args.finish:
                lane["status"] = "idle"
        save_state(problem_dir, state)
        score_str = f" (score={args.score:,})" if args.score is not None else ""
        print(f"Marked {args.finish!r} done{score_str}")
        return

    if args.status:
        print_status(problem_dir, cfg, state)
        return

    if args.tick:
        result = decide(problem_dir, cfg, state)
        save_state(problem_dir, state)
        print(json.dumps(result, indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
