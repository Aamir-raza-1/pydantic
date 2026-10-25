#!/usr/bin/env python3
"""Validate a per-repo profile (YAML or JSON) against profiles/schema.json.

  profile_check.py profiles/<owner>__<repo>.yaml

The loader and rules live in engine/profile.py (the engine uses the same check).
Exit 1 with a list of problems if invalid.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from engine.profile import SCHEMA, read, validate  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    errs = validate(read(sys.argv[1]), json.load(open(SCHEMA)))
    for e in errs:
        print("ERROR", e)
    print("profile valid" if not errs else "%d errors" % len(errs))
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
