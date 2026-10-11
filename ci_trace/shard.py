#!/usr/bin/env python3
"""Split the collected tests into N groups of whole test files, balanced by test count.

  python -m pytest --collect-only -q > collected.txt
  python3 ci_trace/shard.py collected.txt 6 > buckets.txt    # one line per group: space-separated files
"""
import collections
import sys

counts = collections.Counter()
for line in open(sys.argv[1]):
    line = line.strip()
    if "::" in line and not line.startswith(("=", "<")):
        counts[line.split("::")[0]] += 1
n = int(sys.argv[2])
groups = [[0, []] for _ in range(n)]
for f, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
    g = min(groups, key=lambda g: g[0])
    g[0] += c
    g[1].append(f)
for c, files in groups:
    print(" ".join(sorted(files)))
print("%d tests in %d files -> %s" % (sum(counts.values()), len(counts), [g[0] for g in groups]), file=sys.stderr)
