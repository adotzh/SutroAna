---
name: lower-bound
description: Compute the structure-preserving lower bound for a Dally-model IR and, if there is a gap, generate the optimally-remapped IR that closes it. Usage: /lower-bound <ir-file-or-problem-dir>
---

# lower-bound

Compute the minimum achievable cost for a given IR under the Dally model, and
try to close the gap if one exists.

Usage:
  /lower-bound <path-to-ir-file>          — analyse one specific IR
  /lower-bound <path-to-problem-dir>      — analyse the current record for that problem

---

## What this skill does

The Dally read cost is `ceil(sqrt(addr))`.  For any fixed sequence of operations
(the "algorithm structure"), the only free variable is which address each logical
slot occupies.  The optimal assignment is:

> Sort all slots by read count (descending). Assign addresses 1, 2, 3, … in that
> order.

The cost under this assignment is the **structure-preserving lower bound** — the
minimum cost achievable without changing the algorithm.

- **Gap = 0**: the algorithm is address-optimal.  Only a structural change
  (different loop order, fewer operations, new tiling scheme) can improve it
  further.
- **Gap > 0**: the current address layout is suboptimal.  The remapped IR closes
  the gap exactly and is a provably valid new record.

---

## Phase 1 — Resolve the IR file

If the argument is a **problem directory**:
1. Read `problem.json` to get `record_glob` and `record_cost_pattern`.
2. Glob for record files (`records/record_*.ir` by default).
3. Pick the best record (lowest cost if `record_lower_is_better`, else highest).
4. If no records exist, look for the best IR in `submissions/` using the same
   cost pattern.
5. Set `<ir_file>` to the chosen path.

If the argument is an **IR file**, use it directly.

---

## Phase 2 — Run the lower-bound tool

```bash
SUTROANA=/Users/azhiboedova/PetProjects/SutroGroup/SutroAna
python3 $SUTROANA/lower_bound.py <ir_file> [--out <remapped_ir_file>]
```

Parse the machine-readable lines from stdout:
```
current_cost=<N>
lower_bound=<N>
gap=<N>
gap_pct=<f>
```

---

## Phase 3 — Report to the user

Print a clear summary:

```
IR:            <path>
Current cost:  <current_cost>
Lower bound:   <lower_bound>    (structure-preserving, same ops + optimal addrs)
Gap:           <gap>  (<gap_pct>%)
Status:        optimal | improvable
```

If `status = optimal`:
```
The current IR is already address-optimal.
To improve further, the algorithm structure itself must change —
e.g. different tiling, fewer operations, or a new loop order.
```

If `status = improvable`:
```
The remapped IR closes the gap exactly.
Saved to: <remapped_ir_file>
```
Then proceed to Phase 4.

---

## Phase 4 — Verify and save (only if gap > 0)

1. **Verify** the remapped IR by running the problem's scorer.

   For matmul problems, call the appropriate scorer:
   ```python
   import sys
   sys.path.insert(0, '/Users/azhiboedova/PetProjects/SutroGroup/sutro-problems')
   import matmul
   ir = open('<remapped_ir_file>').read()
   cost = matmul.score_16x16(ir)   # or score_4x4 / score_1x1 as appropriate
   assert cost == lower_bound, f"scorer returned {cost}, expected {lower_bound}"
   ```

   For other problems, look for a scorer in the problem directory or derive it
   from `problem.json`.

2. If verification passes and `cost < current_record`:
   - Save the remapped IR as `<problem_dir>/records/record_<cost>_lb.ir`
   - Append to `events.jsonl`:
     ```json
     {"ts": <unix_ts>, "type": "new_record", "cost": <cost>,
      "prev": <current_cost>, "lane": "lb", "file": "records/record_<cost>_lb.ir"}
     ```
   - Report: **New record set by address remapping alone.**

3. If verification fails (cost doesn't match), report the discrepancy and do
   NOT save.  This signals a bug in the IR parser or scorer.

---

## Phase 5 — Interpretation guidance

After reporting the result, always add a brief interpretation:

**If gap = 0 (algorithm is optimal):**
- State which components dominate cost (from the read-count breakdown).
- Explain what structural change would be needed to reduce each component.
- Example: "sC at addrs 7–38 costs 20,736 (28%). To reduce this, you'd need
  fewer accumulation steps per cell — possible with a Winograd-style factoring."

**If gap > 0 (improvable by remapping):**
- Show the top-3 slots that benefit most from remapping.
- Explain why the original IR didn't have them at cheap addresses.
- Example: "tmp was at addr 769 (cost 28) but is read 3,840 times.
  Moving it to addr 1 (cost 1) saves 103,680."

---

## Notes

- The lower bound is **exact and achievable** — the remapped IR always produces
  the predicted cost (verified by the scorer).
- The bound is **structure-preserving**: it assumes the same ops in the same order,
  only the address mapping changes.  A different algorithm may achieve a lower cost
  still.
- For problems without the Dally model, this skill does not apply.
