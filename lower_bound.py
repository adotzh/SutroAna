#!/usr/bin/env python3
"""
Lower-bound estimator for Dally-model optimization problems.

For any IR program that uses the Dally cost model (read cost = ceil(sqrt(addr))):
  1. Parse the IR and count how many times each address is read.
  2. Sort addresses by read count (descending) and assign optimal addresses
     1, 2, 3, ... in that order — this is the minimum-cost address layout
     for the given operation graph.
  3. Rewrite the IR with the remapped addresses and compute the new cost.

The result is the STRUCTURE-PRESERVING lower bound: the minimum cost achievable
with the exact same sequence of operations, just with an optimal address layout.
If the current IR already achieves this bound (gap = 0), the algorithm itself
must change to improve further.

Usage:
    python3 lower_bound.py <ir_file>                   # report + write remapped IR
    python3 lower_bound.py <ir_file> --out <out_file>  # write remapped IR to file
    python3 lower_bound.py <ir_file> --no-remap        # report only, no output

Output (stdout):
    current_cost=<N>
    lower_bound=<N>
    gap=<N>
    gap_pct=<f>
    status=optimal|improvable
    remapped_ir=<path>   (if written)
"""
import argparse
import math
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

def _cost(addr: int) -> int:
    """Dally read cost: ceil(sqrt(addr)) for positive integer addr."""
    if addr < 1:
        raise ValueError(f"address must be >= 1, got {addr}")
    return math.isqrt(addr - 1) + 1


# ---------------------------------------------------------------------------
# IR parsing — read-count pass
# ---------------------------------------------------------------------------

def count_reads(ir: str) -> dict[int, int]:
    """
    Parse an IR string and return a dict mapping each address to its total
    read count.  Only READS are counted (copies read src, binary ops read
    both operands, exits read each output address; writes are free and not
    counted).  Input-placement addresses (first line) are not counted.
    """
    text = ir.replace(";", "\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError("IR needs at least an input line and an output line")

    reads: dict[int, int] = {}

    def _read(addr: int):
        reads[addr] = reads.get(addr, 0) + 1

    # Exit reads
    for tok in lines[-1].split(","):
        _read(int(tok))

    # Operations (lines[1:-1])
    for ln in lines[1:-1]:
        head, _, rest = ln.partition(" ")
        if not rest:
            raise ValueError(f"malformed instruction: {ln!r}")
        operands = [int(x) for x in rest.split(",")]

        if head == "copy":
            # copy dest, src  — reads src
            _read(operands[1])
        elif head in ("add", "sub", "mul"):
            if len(operands) == 3:
                # op dest, s1, s2  — reads s1 and s2
                _read(operands[1])
                _read(operands[2])
            elif len(operands) == 2:
                # op dest, src  — shorthand for op dest, dest, src
                _read(operands[0])  # dest read as s1
                _read(operands[1])  # src  read as s2
            else:
                raise ValueError(f"bad operand count for {head}: {operands}")
        else:
            raise ValueError(f"unknown op: {head!r}")

    return reads


# ---------------------------------------------------------------------------
# Optimal address assignment
# ---------------------------------------------------------------------------

def optimal_remap(reads: dict[int, int]) -> dict[int, int]:
    """
    Given a read-count dict {addr: count}, return a remapping {old_addr: new_addr}
    that assigns the cheapest addresses to the most-read slots.

    Strategy: sort addresses by read count (descending), assign new addresses
    1, 2, 3, ... in that order.  Ties are broken by original address (ascending)
    for determinism.
    """
    sorted_addrs = sorted(reads.keys(), key=lambda a: (-reads[a], a))
    return {addr: rank + 1 for rank, addr in enumerate(sorted_addrs)}


# ---------------------------------------------------------------------------
# Cost of a read-count dict under a given remapping
# ---------------------------------------------------------------------------

def compute_cost(reads: dict[int, int], remap: dict[int, int] | None = None) -> int:
    """Sum of reads[addr] * cost(remap[addr]) over all addresses."""
    total = 0
    for addr, cnt in reads.items():
        eff_addr = remap[addr] if remap else addr
        total += cnt * _cost(eff_addr)
    return total


# ---------------------------------------------------------------------------
# IR rewriting
# ---------------------------------------------------------------------------

def rewrite_ir(ir: str, remap: dict[int, int]) -> str:
    """Apply address remapping to every address in the IR."""
    text = ir.replace(";", "\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    def _remap_tok(tok: str) -> str:
        return str(remap.get(int(tok), int(tok)))

    def _remap_list(csv: str) -> str:
        return ",".join(_remap_tok(t) for t in csv.split(","))

    out = []
    # Input line — remap placement addresses
    out.append(_remap_list(lines[0]))

    for ln in lines[1:-1]:
        head, _, rest = ln.partition(" ")
        out.append(f"{head} {_remap_list(rest)}")

    # Output line
    out.append(_remap_list(lines[-1]))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(ir: str) -> dict:
    """
    Full analysis of one IR.  Returns a dict with:
      current_cost, lower_bound, gap, gap_pct, remap, remapped_ir
    """
    reads = count_reads(ir)
    current_cost = compute_cost(reads)
    remap = optimal_remap(reads)
    lower_bound = compute_cost(reads, remap)
    gap = current_cost - lower_bound
    gap_pct = gap / current_cost * 100 if current_cost else 0.0
    remapped_ir = rewrite_ir(ir, remap)

    return {
        "reads": reads,
        "current_cost": current_cost,
        "lower_bound": lower_bound,
        "gap": gap,
        "gap_pct": gap_pct,
        "remap": remap,
        "remapped_ir": remapped_ir,
    }


def print_report(result: dict, ir_path: str | None = None,
                 out_path: str | None = None):
    label = ir_path or "<ir>"
    print(f"IR           : {label}")
    print(f"current_cost : {result['current_cost']:,}")
    print(f"lower_bound  : {result['lower_bound']:,}")
    print(f"gap          : {result['gap']:,}  ({result['gap_pct']:.1f}%)")
    status = "optimal" if result["gap"] == 0 else "improvable"
    print(f"status       : {status}")

    if result["gap"] > 0:
        # Show top-5 biggest read-count slots and their new addresses
        remap = result["remap"]
        reads = result["reads"]
        top = sorted(reads.items(), key=lambda x: -x[1])[:5]
        print("\nTop-5 slots → new addresses:")
        for old, cnt in top:
            new = remap[old]
            print(f"  addr {old:5d} ({cnt:6,} reads) → addr {new:3d}"
                  f"  cost {_cost(old)} → {_cost(new)}"
                  f"  save {cnt*(  _cost(old) - _cost(new)):,}")

    if out_path:
        print(f"\nremapped_ir  : {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Compute the structure-preserving lower bound for a Dally-model IR.")
    parser.add_argument("ir_file", help="path to IR file")
    parser.add_argument("--out", metavar="FILE",
                        help="write remapped IR to this file (default: <ir_file>.lb.ir)")
    parser.add_argument("--no-remap", action="store_true",
                        help="report only; do not write the remapped IR")
    args = parser.parse_args()

    ir_path = Path(args.ir_file)
    if not ir_path.exists():
        sys.exit(f"File not found: {ir_path}")

    ir = ir_path.read_text()
    result = analyze(ir)

    if args.no_remap:
        out_path = None
    else:
        out_path = args.out or str(ir_path.with_suffix("")) + ".lb.ir"
        Path(out_path).write_text(result["remapped_ir"] + "\n")

    print_report(result, str(ir_path), out_path)

    # Machine-readable summary to stdout for skill parsing
    print(f"\ncurrent_cost={result['current_cost']}")
    print(f"lower_bound={result['lower_bound']}")
    print(f"gap={result['gap']}")
    print(f"gap_pct={result['gap_pct']:.2f}")

    return 0 if result["gap"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
