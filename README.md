# SutroAna

Agent optimization loop and manager infrastructure for [sutro-problems](https://github.com/cybertronai/sutro-problems).

## The Core Idea

Most optimization work looks like this: you have an idea, you implement it, you score it, you look at the result, and you decide what to try next. That loop is bottlenecked by you — your attention, your time, and how quickly you can context-switch between thinking and coding.

SutroAna replaces the human in that loop with Claude.

The method is: give Claude a problem with a scorer (something that returns a number), and let it run the full explore-evaluate-redirect cycle autonomously. You watch. When it gets stuck or runs out of ideas, it generates new ones. When a lane stalls, it kills it and tries a different angle. When progress plateaus, it opens a second lane to pursue a parallel direction. The loop runs until the budget is spent or the problem is solved.

**Why this works with Claude specifically:**

Claude can do all the parts of the loop that used to require you:
- Read a codebase and understand the cost model
- Propose concrete, non-obvious optimization directions
- Implement an experiment, run it, interpret the result
- Decide whether to iterate or pivot
- Write structured observations so the next agent has context

The harness just scaffolds the memory (what's been tried), the routing (which agent works on what), and the signals (when to stop, when to spawn more). Claude does the actual research.

**Why you want a harness at all:**

Without one, a single Claude session hits context limits, loses track of what it tried, and can't parallelize. The harness gives Claude persistent state across wakeups, independent lanes that don't interfere, and a feedback signal (the scorer) it can trust over raw intuition. It turns a single-shot conversation into a long-running research process.

## What it does

Runs an autonomous optimization loop over any problem that has a scorer:
- If no directions exist yet, **ideates** — analyzes the problem and proposes concrete directions to try
- Launches explorer agents that implement and score ideas
- Detects stalls and redirects agents to the next direction automatically
- Tracks the record history and direction queue

## Usage

```bash
# Start or continue the optimization loop for a problem
/optimize ../sutro-problems/matmul

# Watch progress in the terminal
python3 monitor.py ../sutro-problems/matmul --watch

# CLI state utilities
python3 manager.py ../sutro-problems/matmul --status
python3 manager.py ../sutro-problems/matmul --finish <direction-id> --score <cost>
python3 manager.py ../sutro-problems/matmul --stop
python3 manager.py --list --root ../sutro-problems
```

## How a problem is defined

Each problem folder needs:

| File | Purpose |
|------|---------|
| `problem.json` | Config: record glob, cost pattern, baseline, stall thresholds |
| `directions.json` | Direction queue with prompts embedded per entry |
| `agent_prompt.md` | Base context injected into every explorer agent prompt |

If `directions.json` is missing or empty, `/optimize` runs an **ideation phase**
first — reads the problem, establishes a baseline, and generates the direction queue.

## Loop Overview

```
User → /optimize <problem>
         │
         ▼
   ┌─────────────┐
   │  Phase 0    │  Read directions.json
   │  Check      │  Has directions with prompts?
   └──────┬──────┘
          │ No                  Yes
          ▼                      ▼
   ┌─────────────┐        ┌─────────────┐
   │  Phase 1    │        │  Phase 2    │
   │  Ideation   │───────▶│    Tick     │
   └─────────────┘        └──────┬──────┘
                                 │
                          manager.py --tick
                          returns action plan
                                 │
                          ┌──────▼──────┐
                          │  Phase 3    │
                          │  Execute    │
                          └─────────────┘
```

## Full Loop

```mermaid
flowchart TD
    User(["/optimize <problem>"]) --> P0

    subgraph P0["Phase 0 — Check"]
        Check{directions.json\nexists with prompts?}
    end

    subgraph P1["Phase 1 — Ideation"]
        ReadProblem["Read problem files\n(README, scorer, experiments)"]
        Baseline["Establish baseline score"]
        GenDirs["Generate 5–8 directions\nordered by expected impact"]
        WriteFiles["Write:\n• directions.json\n• problem.json\n• agent_prompt.md"]
        ReadProblem --> Baseline --> GenDirs --> WriteFiles
    end

    subgraph P2["Phase 2 — Tick (manager.py --tick)"]
        ScanRec["scan_records()\n↳ files: records/record_*.ir"]
        SyncHist["sync_improvement_history()\n↳ emit new_record events"]
        Plateau["compute_plateau()\ncompare last rate vs avg earlier"]
        Tokens["read_tokens_used()\ntoken_log.jsonl"]
        Stall["is_lane_stalled()?\nN min + M experiment files"]
        Decide["decide(): build action plan\n+ determine situation"]
        ScanRec --> SyncHist --> Plateau --> Tokens --> Stall --> Decide
    end

    subgraph P3["Phase 3 — Execute Actions"]
        StopLane["stop_lane\n→ write STOP_SIGNAL_<id>"]
        SpawnAgent["spawn_agent\n→ launch explorer background agent\n→ mark direction active in directions.json"]
        Ideate["ideate\n→ read done directions + scores\n→ generate 4–6 new directions\n→ append to directions.json"]
    end

    subgraph Explorer["Explorer Agent (per lane)"]
        CheckStop{"STOP_SIGNAL\nexists?"}
        RunExp["Implement & run experiment\nexp_<lane>_*.py"]
        BeatRecord{"New best\nscore?"}
        SaveRecord["Write records/record_<cost>_lane<id>.ir"]
        LogTokens["Append token_log.jsonl"]
        LogEvent["Append events.jsonl"]
        CheckStop -->|Yes| Halt["Delete signal, exit"]
        CheckStop -->|No| RunExp
        RunExp --> BeatRecord
        BeatRecord -->|Yes| SaveRecord --> LogEvent
        BeatRecord -->|No| LogTokens
        SaveRecord --> LogTokens
        LogTokens --> CheckStop
    end

    subgraph Monitor["monitor.py"]
        Tail["--tail\nlive event stream"]
        Watch["--watch\nfull dashboard"]
        Oneline["--oneline\ntmux status bar"]
    end

    subgraph Files["Filesystem State"]
        DJ[("directions.json\n(record, lanes, directions,\nimprovement_history)")]
        EJ[("events.jsonl\n(tick, spawn, stop, plateau,\nstall, new_record, ideate)")]
        TJ[("token_log.jsonl")]
        PJ[("problem.json\n(config + thresholds)")]
        REC[("records/record_*.ir")]
    end

    P0 --> Check
    Check -->|No| P1
    Check -->|Yes| P2
    P1 --> WriteFiles --> P2

    Decide -->|actions JSON| P3
    P3 --> StopLane & SpawnAgent & Ideate

    SpawnAgent --> Explorer
    Explorer -.->|writes| REC & TJ & EJ

    Decide -.->|reads| DJ & REC & TJ
    Decide -.->|writes| EJ & DJ

    Ideate -->|re-runs| P2

    StopLane -.->|STOP_SIGNAL file| CheckStop
    SaveRecord -.->|record file| ScanRec

    Decide -->|next_wakeup_minutes| Wakeup["ScheduleWakeup\n→ /optimize fires again"]
    Wakeup --> P2

    EJ & REC & DJ & TJ -.->|reads| Monitor
```

## Manager Decision Logic

```mermaid
flowchart LR
    subgraph decide["manager.py decide()"]
        direction TB
        A["1. scan records + experiments"] --> B
        B["2. sync improvement_history\n   emit new_record events"] --> C
        C["3. compute_plateau()\n   last_rate / avg_earlier_rates\n   needs ≥ 3 history entries"] --> D
        D["4. check token budget"] --> E
        E["5. find stalled lanes\n   (≥ stall_minutes AND ≥ stall_min_files\n   experiments since last record)"] --> F

        F{Stalled lanes?} -->|Yes| G["stop_lane + respawn\nwith next pending direction"]
        F -->|No| H

        G --> H{Plateau AND\nroom for more\nlanes?}
        H -->|Yes| I["spawn new lane\nwith next pending direction"]
        H -->|No| J

        I --> J{No active lanes\nand no spawns yet?}
        J -->|Yes| K["bootstrap lane 0\nwith first pending direction"]
        J -->|No| L

        K --> L{No pending dirs\nand stalled/exhausted?}
        L -->|Yes| M["ideate — generate\nnew directions"]
        L -->|No| N["determine situation:\nrunning / plateau /\nstalled / exhausted / bootstrap"]

        M --> N
        N --> O["set next_wakeup_minutes:\nplateau/stall → cadence÷2\nrunning → cadence\n(min 5m)"]
    end
```

## Situation State Machine

```mermaid
stateDiagram-v2
    [*] --> bootstrap : no directions.json

    bootstrap --> running : ideation complete,\nlane 0 spawned
    running --> plateau : last improvement rate\n< sensitivity × avg rate
    running --> stalled : lane silent ≥ stall_minutes\nAND ≥ stall_min_files exps
    plateau --> running : new lane spawned,\nrecord improves
    stalled --> running : lane restarted\non next direction
    running --> exhausted : all directions done\nand lanes stalled
    exhausted --> running : ideation generates\nnew directions
```

## Key Files

| File | Owner | Purpose |
|------|-------|---------|
| `problem.json` | human / ideation | Config: globs, thresholds, budget |
| `directions.json` | manager | Queue of directions + lane state + improvement history |
| `agent_prompt.md` | ideation | Base context injected into every explorer prompt |
| `events.jsonl` | manager + explorers | Structured event log (tick, spawn, stop, record, plateau…) |
| `token_log.jsonl` | explorers | Per-experiment token usage |
| `records/record_*.ir` | explorers | Best solutions found, cost encoded in filename |
| `exp_<lane>_*.py` | explorers | Experiment files (activity signal for stall detection) |
| `STOP_SIGNAL_<lane>` | manager | Tells a running explorer to halt and exit |

## Directory layout

```
SutroAna/
  manager.py                  # CLI state utility
  monitor.py                  # read-only progress dashboard
  .claude/
    skills/
      optimize/
        SKILL.md              # /optimize skill — the manager logic
```

## Related repos

- [sutro-problems](https://github.com/cybertronai/sutro-problems) — problem definitions
- [SutroYaro](https://github.com/cybertronai/SutroYaro) — sparse parity research workspace
