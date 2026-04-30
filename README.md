# SutroAna

Agent optimization loop and manager infrastructure for [sutro-problems](https://github.com/cybertronai/sutro-problems).

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
