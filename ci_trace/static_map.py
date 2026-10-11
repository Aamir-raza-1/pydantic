#!/usr/bin/env python3
"""Static import closure per test file, with the repo's own Python (CI). Wraps static_imports.build
(a copy of ci-casestudies engine/static_imports.py) so no profile loader is needed on the runner.

  python ci_trace/static_map.py . --source pydantic --source pydantic-core/python --test tests --test pydantic-core/tests \
      --import-root pydantic-core/python --import-root pydantic-core --out static.json
Also reports files the parser could not read (they would silently drop out of the map).
"""
import argparse
import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import static_imports  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("repo")
ap.add_argument("--source", action="append", default=[])
ap.add_argument("--test", action="append", default=[])
ap.add_argument("--import-root", action="append", default=[])
ap.add_argument("--out", default="static.json")
a = ap.parse_args()
bad = []
for top in a.source + a.test:
    for d, dn, fn in os.walk(os.path.join(a.repo, top)):
        for n in fn:
            if n.endswith(".py"):
                p = os.path.join(d, n)
                try:
                    ast.parse(open(p, encoding="utf-8").read())
                except (SyntaxError, UnicodeDecodeError) as e:
                    bad.append("%s: %s" % (os.path.relpath(p, a.repo), e))
result, n = static_imports.build(a.repo, a.source, a.test, re.compile(r"(^|/)test_[^/]*\.py$"), a.import_root or None)
sizes = sorted(len(v) for v in result.values())
print("python %s; modules %d; test files %d; closure median %d p90 %d max %d; unparseable %d %s" % (
    sys.version.split()[0], n, len(result), sizes[len(sizes) // 2], sizes[int(len(sizes) * 0.9)], sizes[-1], len(bad), bad[:5]))
json.dump(result, open(a.out, "w"))
