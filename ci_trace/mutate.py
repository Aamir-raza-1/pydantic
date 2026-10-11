"""Generate and apply mutants (small deliberate bugs) for the selection safety check.

Generic version of the HA tracer's mutate.py, with one change from the HA lesson "5 of 21 bugs
survived because they sat on lines no test runs": body mutants are only planted on lines the
traced run executed (executed_lines.json.gz from analyze.py).

generate: python ci_trace/mutate.py generate targets.json --seed 1 --out mutation --executed executed_lines.json.gz
  Per target file: up to one body mutant (negate a one-line `if` test, or `return None`) on an
  executed line, and one module mutant (change a top-level int/str constant). One mutant per
  batch (the suite is fast enough), so every failure belongs to exactly one bug.

apply: python ci_trace/mutate.py apply mutation 3          (batch 3; batch 0 = baseline, nothing applied)
apply-ids: python ci_trace/mutate.py apply-ids mutation 4  (mutant 4 alone, for isolated re-runs)
"""

from __future__ import annotations

import ast
import gzip
import json
from pathlib import Path
import random
import sys


def _candidates(path: str, executed: set[int]) -> tuple[list[dict], list[dict]]:
    src = Path(path).read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    body, module = [], []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and node.test.lineno == node.test.end_lineno and node.test.lineno in executed:
                line = lines[node.test.lineno - 1]
                seg = line[node.test.col_offset : node.test.end_col_offset]
                new = line[: node.test.col_offset] + f"not ({seg})" + line[node.test.end_col_offset :]
                body.append({"line": node.test.lineno, "op": "negate-if", "old": line, "new": new})
            elif (
                isinstance(node, ast.Return)
                and node.value is not None
                and not (isinstance(node.value, ast.Constant) and node.value.value is None)
                and node.lineno == node.end_lineno
                and node.lineno in executed
            ):
                line = lines[node.lineno - 1]
                indent = line[: len(line) - len(line.lstrip())]
                body.append({"line": node.lineno, "op": "return-none", "old": line, "new": indent + "return None"})
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) in (int, str)
            and not isinstance(node.value.value, bool)
            and node.lineno == node.end_lineno
            and not node.targets[0].id.startswith("__")
        ):
            line = lines[node.lineno - 1]
            v = node.value
            repl = repr(v.value + 1) if isinstance(v.value, int) else repr(v.value + "_mutant")
            new = line[: v.col_offset] + repl + line[v.end_col_offset :]
            module.append({"line": node.lineno, "op": "change-constant", "old": line, "new": new})
    return body, module


def _valid(path: str, m: dict) -> bool:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    lines[m["line"] - 1] = m["new"]
    try:
        ast.parse("\n".join(lines))
    except SyntaxError:
        return False
    return True


def generate(targets_file: str, seed: int, out: str, executed_file: str | None) -> None:
    rng = random.Random(seed)
    targets = json.loads(Path(targets_file).read_text())
    executed = {}
    if executed_file:
        with gzip.open(executed_file, "rt") as f:
            executed = {k: set(v) for k, v in json.load(f).items()}
    mutants, skipped = [], []
    for path in targets:
        try:
            body, module = _candidates(path, executed.get(path, set()) if executed_file else set(range(1, 10**6)))
        except (SyntaxError, FileNotFoundError) as err:
            skipped.append({"file": path, "why": str(err)})
            continue
        for kind, pool in (("body", body), ("module", module)):
            rng.shuffle(pool)
            for m in pool:
                if _valid(path, m):
                    mutants.append({"id": len(mutants) + 1, "file": path, "kind": kind, **m})
                    break
            else:
                skipped.append({"file": path, "why": "no executed %s candidate" % kind})
    batches = [[]] + [[m["id"]] for m in mutants]  # batch 0 = baseline; one mutant per batch
    Path(out).mkdir(parents=True, exist_ok=True)
    Path(out, "mutants.json").write_text(json.dumps(mutants, indent=1))
    Path(out, "batches.json").write_text(json.dumps(batches))
    Path(out, "skipped.json").write_text(json.dumps(skipped, indent=1))
    print(f"{len(mutants)} mutants from {len(targets)} files in {len(batches) - 1} batches (+ baseline); {len(skipped)} skipped")


def apply(out: str, batch: int) -> None:
    mutants = {m["id"]: m for m in json.loads(Path(out, "mutants.json").read_text())}
    ids = json.loads(Path(out, "batches.json").read_text())[batch]
    for i in ids:
        m = mutants[i]
        p = Path(m["file"])
        lines = p.read_text(encoding="utf-8").split("\n")
        assert lines[m["line"] - 1] == m["old"], f"mutant {i}: line changed"
        lines[m["line"] - 1] = m["new"]
        p.write_text("\n".join(lines), encoding="utf-8")
        print(f"applied mutant {i}: {m['file']}:{m['line']} {m['op']}")
        print(f"  - {m['old'].strip()}\n  + {m['new'].strip()}")


def apply_ids(out: str, ids: list[int]) -> None:
    """Apply specific mutants regardless of batch (for isolated re-runs)."""
    batches = json.loads(Path(out, "batches.json").read_text())
    Path(out, "batches.json").write_text(json.dumps(batches + [ids]))
    apply(out, len(batches))


if __name__ == "__main__":
    if sys.argv[1] == "apply-ids":
        apply_ids(sys.argv[2], [int(x) for x in sys.argv[3].split(",") if x])
    elif sys.argv[1] == "generate":
        ex = sys.argv[sys.argv.index("--executed") + 1] if "--executed" in sys.argv else None
        generate(sys.argv[2], int(sys.argv[sys.argv.index("--seed") + 1]), sys.argv[sys.argv.index("--out") + 1], ex)
    else:
        apply(sys.argv[2], int(sys.argv[3]))
