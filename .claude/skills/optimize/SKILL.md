---
name: optimize
description: Run the optimization loop for a problem. If no directions exist yet, runs an ideation phase first to generate them. Then detects stalls and launches or redirects explorer agents. Usage: /optimize <path-to-problem>
---

# optimize

Run the optimization loop for a problem folder.

Usage: /optimize <path-to-problem>   (e.g. /optimize ../sutro-problems/matmul)

---

## Phase 0 — Check if directions exist

Read `<problem>/directions.json`. If the file does not exist, or `directions` is
empty, or no entry has a `prompt` field: go to **Phase 1 (Ideation)**.
Otherwise go to **Phase 2 (Optimization loop)**.

---

## Phase 1 — Ideation (no directions defined yet)

The goal is to understand the problem deeply enough to propose concrete directions.

1. **Read everything in `<problem>/`**: README.md, scorer code, any existing
   experiments, and `problem.json` (if it exists).

2. **Understand the cost model**: What is being minimized or maximized? What does
   the scorer reward and penalize? What are writes are free? Which reads are expensive?

3. **Establish a baseline**: Run the simplest reference implementation to get a
   concrete starting number.

4. **Analyze the baseline**: Where is cost concentrated? What operations dominate?
   Use this to find the highest-leverage targets.

5. **Generate 5–8 concrete directions**, ordered by expected impact. For each:
   - Short `name` and one-line `summary`
   - Concrete `prompt` — exactly what to implement and why it should help
   - Honest estimate of likelihood to help

6. **Write `<problem>/directions.json`**:
   ```json
   {
     "record": <baseline score>,
     "record_file": null,
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
     "stall_minutes": 15,
     "stall_min_files": 3,
     "experiment_glob": "exp_*.py"
   }
   ```

8. **Write `<problem>/agent_prompt.md`** with the base context every explorer needs:
   problem statement, cost model, how to run the scorer, where to save records, rules.

Then continue to **Phase 2**.

---

## Phase 2 — Optimization loop

1. **Read state** from `<problem>/problem.json` and `<problem>/directions.json`.

2. **Scan records** — glob `record_glob`, extract cost via `record_cost_pattern`.

3. **Scan experiments** — glob `experiment_glob`, sort by mtime.

4. **Detect stall** — stalled if BOTH:
   - minutes since last record ≥ `stall_minutes`
   - experiment files written since last record ≥ `stall_min_files`

5. **Print dashboard**:
   - Direction queue: ✅ done / 🔄 active / ⬜ pending with best scores
   - Record timeline: timestamp, score, delta vs baseline, gap between records
   - Stall status

6. **Decide**:
   - Explorer active, NOT stalled → report status only.
   - Stalled OR no active direction → write `<problem>/STOP_SIGNAL`, mark active
     direction done with its best score, pick next pending direction, build prompt,
     launch background explorer agent.
   - All directions exhausted → report this; offer to re-run Phase 1 to generate
     a new batch based on what was learned.

---

## Explorer prompt structure

Combine:
1. Contents of `<problem>/agent_prompt.md`
2. "What has been tried" — all done directions with scores and summaries
3. The `prompt` field from the next pending direction

Always prepend:
> At the start of each new experiment file's `__main__` block, check whether
> `<problem>/STOP_SIGNAL` exists. If so, delete it, print "STOP_SIGNAL — halting.",
> and exit. This lets the manager redirect you to a new direction.

Launch as a **background agent**.

---

## After launching

- Set direction status to "active" in `directions.json`
- Report: direction name, current record, how to monitor:
  `python3 <SutroAna>/monitor.py <path-to-problem> --watch`
