---
name: optimize
description: Run the optimization loop for a problem. If no directions exist yet, runs an ideation phase first to generate them. Then calls manager.py --tick, executes its action plan (stop lanes, spawn explorers, ideate), and schedules the next wakeup. Usage: /optimize <path-to-problem>
---

# optimize

Run the optimization loop for a problem folder.

Usage: /optimize <path-to-problem>   (e.g. /optimize ../sutro-problems/matmul)

---

## Phase 0 — Check if directions exist

Read `<problem>/directions.json`. If the file does not exist, or `directions` is
empty, or no entry has a `prompt` field: go to **Phase 1 (Ideation)**.
Otherwise go to **Phase 2 (Tick)**.

---

## Phase 1 — Ideation (no directions defined yet)

The goal is to understand the problem deeply enough to propose concrete directions.

1. **Read everything in `<problem>/`**: README.md, scorer code, any existing
   experiments, and `problem.json` (if it exists).

2. **Understand the cost model**: What is being minimized or maximized? What does
   the scorer reward and penalize? What writes are free? Which reads are expensive?

3. **Establish a baseline**: Run the simplest reference implementation to get a
   concrete starting number.

4. **Analyze the baseline**: Where is cost concentrated? What operations dominate?
   Use this to find the highest-leverage targets.

5. **Generate 5–8 concrete directions**, ordered by expected impact. For each:
   - Short `id` (kebab-case) and human-readable `name`
   - One-line `summary`
   - Concrete `prompt` — exactly what to implement and why it should help
   - Honest estimate of likelihood to help

6. **Write `<problem>/directions.json`**:
   ```json
   {
     "record": <baseline score>,
     "record_file": null,
     "lanes": [],
     "improvement_history": [],
     "directions": [
       {
         "id": "short-id",
         "name": "Human-readable name",
         "status": "pending",
         "best_score": null,
         "summary": "One-line summary",
         "prompt": "Detailed instructions for the explorer agent..."
       }
     ]
   }
   ```

7. **Write `<problem>/problem.json`** if missing:
   ```json
   {
     "name": "<problem>",
     "description": "...",
     "record_glob": "records/record_*.ext",
     "record_cost_pattern": "record_(\\d+)",
     "record_lower_is_better": true,
     "baseline": <score>,
     "max_lanes": 2,
     "plateau_sensitivity": 0.2,
     "stall_minutes": 15,
     "stall_min_files": 3,
     "token_budget": null,
     "experiment_glob": "exp_*.py"
   }
   ```

8. **Write `<problem>/agent_prompt.md`** with the base context every explorer needs:
   problem statement, cost model, how to run the scorer, where to save records,
   file naming conventions (see Explorer conventions below).

Then continue to **Phase 2**.

---

## Phase 2 — Tick

Call the manager to get the current action plan:

```bash
python3 <SutroAna>/manager.py <problem> --tick
```

This outputs a JSON object:
```json
{
  "actions": [...],
  "situation": "running|plateau|stalled|exhausted|bootstrap",
  "next_wakeup_minutes": 10,
  "metrics": {
    "record": 73602,
    "is_plateau": false,
    "plateau_score": 0.85,
    "active_lanes": 1,
    "pending_directions": 2,
    "tokens_used": 14200,
    "token_budget": null
  }
}
```

**Print the metrics** to the user in a short dashboard:
- Current record, baseline delta
- Active/pending directions count
- Plateau score (and whether it fired)
- Tokens used / budget

---

## Phase 3 — Execute actions

Execute each action in `actions` in order:

### `stop_lane`
```json
{"type": "stop_lane", "lane_id": 1}
```
Write the file `<problem>/STOP_SIGNAL_<lane_id>` with content `"stop"`.
The running explorer checks for this file and halts.

### `spawn_agent`
```json
{"type": "spawn_agent", "lane_id": 0, "direction_id": "cascade-tiling", "is_new_lane": true}
```
1. Clear any stale `<problem>/STOP_SIGNAL_<lane_id>` (delete if it exists).
2. Read `directions.json` to find the direction with `id == direction_id`.
3. Build the explorer prompt (see **Explorer prompt structure** below).
4. Update `directions.json`: set direction `status` → `"active"`, and add/update
   lane entry:
   ```json
   {"id": <lane_id>, "status": "active", "direction_id": "<direction_id>", "started_at": <unix_ts>}
   ```
5. Launch as a **background agent** with the built prompt.

### `ideate`
```json
{"type": "ideate"}
```
All directions are exhausted. Run a new ideation cycle:
1. Read the full directions history (all done entries with `best_score`).
2. Identify what was tried and what worked / didn't.
3. Generate a fresh batch of 4–6 new directions (append to `directions`, do not
   replace existing ones — keep the history intact).
4. Write updated `directions.json`.
5. Continue to the next tick immediately (re-run Phase 2).

---

## Explorer prompt structure

Combine these parts in order:

**Part 1** — Contents of `<problem>/agent_prompt.md`

**Part 2** — "What has been tried" section:
```
## What has been tried

<for each done direction>
- <name>: <summary>  best_score=<score>
```

**Part 3** — This direction's task (from `directions[id].prompt`):
```
## Your task — Lane <lane_id>

<prompt field>
```

**Part 4** — Always append these conventions verbatim:

```
## Explorer conventions

You are working in lane <lane_id>. Follow these rules exactly:

1. **STOP_SIGNAL**: At the start of each new experiment file's `__main__` block,
   check whether `<problem>/STOP_SIGNAL_<lane_id>` exists. If so, delete it,
   print "STOP_SIGNAL — halting.", and exit. This lets the manager redirect you.

2. **Experiment file naming**: Name every experiment file `exp_<lane_id>_<desc>.py`
   (e.g. `exp_0_cascade_v2.py`). This lets the manager attribute activity to
   your lane.

3. **Record file naming**: When you beat the current record, save the solution as
   `records/record_<cost>_lane<lane_id>.ir` (or whatever extension the problem
   uses). Create the `records/` directory if needed.

4. **Token logging**: After each experiment run, append a line to
   `<problem>/token_log.jsonl`:
   ```
   {"ts": <unix_timestamp>, "lane": <lane_id>, "exp": "<filename>", "tokens": <count>}
   ```
   Use an estimate of tokens consumed during that run.

5. **Iterate**: Keep exploring variations within your direction until you get a
   STOP_SIGNAL or exhaust the search space.
```

---

## After executing all actions

1. Report to the user:
   - Situation string from tick output
   - Which directions are now active (and in which lane)
   - Current record and how much better than baseline
   - How to monitor: `python3 <SutroAna>/monitor.py <problem> --watch`

2. Schedule the next wakeup using `next_wakeup_minutes` from tick output:
   ```
   /schedule <next_wakeup_minutes>m /optimize <problem>
   ```
   (Use ScheduleWakeup tool with `delaySeconds = next_wakeup_minutes * 60`.)
