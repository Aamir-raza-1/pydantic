#!/usr/bin/env python3
"""Rewrite junit testcase `file` attributes to canonical repo paths (symlinks resolved), in place.

  python3 ci_trace/canon_junit.py <repo> junit/*.xml
"""
import os
import sys
import xml.etree.ElementTree as ET

repo = os.path.realpath(sys.argv[1])
for x in sys.argv[2:]:
    tree = ET.parse(x)
    n = 0
    for tc in tree.getroot().iter("testcase"):
        f = tc.get("file")
        if not f:
            cls = tc.get("classname", "")
            parts = cls.split(".")
            # classname "tests.pydantic_core.test_x.TestCls" -> try the longest prefix that is a file
            for i in range(len(parts), 0, -1):
                cand = "/".join(parts[:i]) + ".py"
                if os.path.exists(os.path.join(repo, cand)):
                    f = cand
                    break
        if f and os.path.exists(os.path.join(repo, f)):
            real = os.path.relpath(os.path.realpath(os.path.join(repo, f)), repo)
            tc.set("file", real)
            n += 1
    tree.write(x)
    print("%s: %d testcases canonicalized" % (x, n))
