#!/usr/bin/env python3
"""Build the file -> tests map from traced runs (generic version of the HA tracer's analyze.py).

Input: artifact folders trace-group-*/ (coverage.sqlite, tests-*.jsonl, opens.log.gz, junit*.xml,
timing.txt) from the "Trace tests" workflow.

Two sources, merged per test:
  coverage  per-test coverage contexts (pytest-cov --cov-context=test): Python files executed
  ebpf      file opens (bpftrace) inside the test's time window on the same worker PID

Paths are canonical repo paths: symlinks are resolved against the checkout (--repo), so a test
collected through a symlinked folder (pydantic: tests/pydantic_core -> pydantic-core/tests) is
keyed by its real path, the one PRs change.

Outputs (--out): report.json, map_testfiles.json.gz, test_file_seconds.json (traced run),
test_file_seconds_baseline.json (untraced run, if junit-baseline.xml exists),
executed_lines.json.gz {repo file: [lines run by any test or at import]} (mutant targets).

Usage: python3 ci_trace/analyze.py <artifacts_dir> --root /home/runner/work/<repo>/<repo>/ --repo . [--out out]
"""
import argparse
import bisect
import collections
import functools
import glob
import gzip
import json
import os
import re
import sqlite3
import statistics
import xml.etree.ElementTree as ET

IGNORED_PREFIXES = ("/proc/", "/sys/", "/dev/", "/etc/", "/run/", "/tmp/", "/usr/share/", "/var/")
SITE = re.compile(r"/site-packages/([^/]+)")


@functools.lru_cache(maxsize=None)
def canonical(rel, repo):
    """Repo-relative path with symlinks resolved (stays as given when the checkout has no such file)."""
    if not repo:
        return rel
    full = os.path.join(repo, rel)
    if not os.path.lexists(full):
        return os.path.normpath(rel)
    real = os.path.relpath(os.path.realpath(full), os.path.realpath(repo))
    return rel if real.startswith("..") else real


def canonical_nodeid(nodeid, repo):
    f, sep, rest = nodeid.partition("::")
    return canonical(f, repo) + sep + rest


def normalize(path, roots, repo):
    """('repo', relpath) | ('pkg', distribution-ish name) | None for noise."""
    if not path.startswith("/"):
        return ("repo", canonical(os.path.normpath(path), repo))
    rel = next((path[len(r):] for r in roots if path.startswith(r)), None)
    if rel is not None:
        if os.path.basename(rel).startswith((".coverage", "junit")):
            return None  # the measurement tools' own output files
        if rel.startswith((".venv/", "venv/", ".git/", "trace/", "target/")) or "__pycache__" in rel or "/target/" in rel:
            m = SITE.search(path)
            return ("pkg", m.group(1)) if m else None
        return ("repo", canonical(rel, repo))
    m = SITE.search(path)
    if m:
        return ("pkg", m.group(1))
    return None


def load_windows(group_dir, repo):
    windows = collections.defaultdict(list)  # pid -> [(start, end, nodeid, outcome)]
    for f in glob.glob(os.path.join(group_dir, "tests-*.jsonl")):
        for line in open(f):
            t = json.loads(line)
            windows[t["pid"]].append((t["start_ns"], t["end_ns"], canonical_nodeid(t["nodeid"], repo), t["outcome"]))
    for pid in windows:
        windows[pid].sort()
    return windows


def ebpf_deps(group_dir, windows, roots, repo):
    deps = collections.defaultdict(set)
    path = os.path.join(group_dir, "opens.log.gz")
    if not os.path.exists(path):
        return deps, 0, 0
    starts = {pid: [w[0] for w in ws] for pid, ws in windows.items()}
    seen = attributed = 0
    with gzip.open(path, "rt", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split(" ", 2)
            if len(parts) != 3 or not parts[0].isdigit():
                continue
            ns, pid, fpath = int(parts[0]), int(parts[1]), parts[2]
            seen += 1
            ws = windows.get(pid)
            if not ws:
                continue
            i = bisect.bisect_right(starts[pid], ns) - 1
            if i < 0 or ns > ws[i][1]:
                continue
            norm = normalize(fpath, roots, repo)
            if norm:
                attributed += 1
                deps[ws[i][2]].add(norm)
    return deps, seen, attributed


def coverage_deps(group_dir, roots, repo, executed):
    """Per-test files from coverage contexts; also fills executed {file: set(lines)} over all contexts."""
    deps = collections.defaultdict(set)
    unattributed = set()
    db = os.path.join(group_dir, "coverage.sqlite")
    if not os.path.exists(db):
        return deps, unattributed
    con = sqlite3.connect(db)
    files = {i: canonical(next((p[len(r):] for r in roots if p.startswith(r)), p), repo)
             for i, p in con.execute("select id, path from file")}
    contexts = dict(con.execute("select id, context from context"))
    has_bits = con.execute("select count(*) from sqlite_master where name='line_bits'").fetchone()[0]
    if has_bits:
        from coverage.numbits import numbits_to_nums  # noqa: PLC0415 (only needed for line data)
        for file_id, ctx_id, bits in con.execute("select file_id, context_id, numbits from line_bits"):
            executed[files[file_id]].update(numbits_to_nums(bits))
            ctx = contexts.get(ctx_id, "")
            if not ctx:
                unattributed.add(files[file_id])
                continue
            deps[canonical_nodeid(ctx.rsplit("|", 1)[0], repo)].add(("repo", files[file_id]))
    else:
        for file_id, ctx_id, fromno, tono in con.execute("select file_id, context_id, fromno, tono from arc"):
            executed[files[file_id]].update(x for x in (fromno, tono) if x > 0)
            ctx = contexts.get(ctx_id, "")
            if not ctx:
                unattributed.add(files[file_id])
                continue
            deps[canonical_nodeid(ctx.rsplit("|", 1)[0], repo)].add(("repo", files[file_id]))
    return deps, unattributed


def junit_seconds(path, repo):
    out = collections.Counter()
    for tc in ET.parse(path).getroot().iter("testcase"):
        f = tc.get("file") or tc.get("classname", "").replace(".", "/") + ".py"
        out[canonical(f, repo)] += float(tc.get("time") or 0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts")
    ap.add_argument("--root", action="append", required=True, help="absolute checkout path on the runner (repeatable)")
    ap.add_argument("--repo", default=".", help="checkout used to resolve symlinks")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    roots = [r.rstrip("/") + "/" for r in args.root]
    repo = os.path.abspath(args.repo)

    tests = {}
    collect_windows = set()
    cov_all, ebpf_all = collections.defaultdict(set), collections.defaultdict(set)
    import_time_only, executed = set(), collections.defaultdict(set)
    timing, opens_seen, opens_attr = {}, 0, 0
    secs, secs_base = collections.Counter(), collections.Counter()
    groups = sorted(glob.glob(os.path.join(args.artifacts, "trace-group-*")))
    for gdir in groups:
        windows = load_windows(gdir, repo)
        for ws in windows.values():
            for _, _, nodeid, outcome in ws:
                if outcome == "collect":
                    collect_windows.add(nodeid)  # a test file's import (collection) window, not a test
                else:
                    tests[nodeid] = outcome
        c, unattr = coverage_deps(gdir, roots, repo, executed)
        e, seen, attr = ebpf_deps(gdir, windows, roots, repo)
        opens_seen += seen
        opens_attr += attr
        import_time_only |= unattr
        for k, v in c.items():
            cov_all[k] |= v
        for k, v in e.items():
            ebpf_all[k] |= v
        t = os.path.join(gdir, "timing.txt")
        if os.path.exists(t):
            timing[os.path.basename(gdir)] = dict(l.strip().split("=", 1) for l in open(t) if "=" in l)
        if os.path.exists(os.path.join(gdir, "junit.xml")):
            secs.update(junit_seconds(os.path.join(gdir, "junit.xml"), repo))
        if os.path.exists(os.path.join(gdir, "junit-baseline.xml")):
            secs_base.update(junit_seconds(os.path.join(gdir, "junit-baseline.xml"), repo))

    # tests + test-file collection windows (key = the file path itself)
    merged = {n: cov_all.get(n, set()) | ebpf_all.get(n, set()) for n in set(tests) | collect_windows | {k for k in cov_all if "::" not in k}}
    by_file = collections.defaultdict(set)
    for n, ds in merged.items():
        for kind, name in ds:
            by_file["%s:%s" % (kind, name)].add(n)

    ebpf_only_noncode = collections.Counter()
    tests_with_noncode = 0
    for n in tests:
        extra = {d for d in ebpf_all.get(n, set()) - cov_all.get(n, set()) if d[0] == "repo" and not d[1].endswith(".py")}
        if extra:
            tests_with_noncode += 1
        for d in extra:
            ebpf_only_noncode[d[1]] += 1
    tfiles = sorted({n.split("::")[0] for n in tests} | {k for k in merged if "::" not in k})
    no_deps = [n for n in tests if not merged[n]]
    collect_only = collections.Counter()  # repo files a test file needs only through its import (collection)
    for t in tfiles:
        during_tests = set().union(*[merged[n] for n in tests if n.split("::")[0] == t] or [set()])
        collect_only[t] = len(merged.get(t, set()) - during_tests)
    report = {
        "tests": len(tests),
        "test_files": len(tfiles),
        "groups": len(groups),
        "outcomes": collections.Counter(tests.values()),
        "timing": timing,
        "tests_without_any_dependency": len(no_deps),
        "test_files_with_collection_window": len(collect_windows),
        "test_files_with_deps_only_at_collection": sum(1 for v in collect_only.values() if v),
        "deps_only_at_collection_median_per_file": statistics.median(collect_only.values()) if collect_only else None,
        "tests_without_any_dependency_sample": sorted(no_deps)[:20],
        "deps_per_test_median": {
            "coverage": statistics.median([len(cov_all.get(n, ())) for n in tests]) if tests else None,
            "ebpf": statistics.median([len(ebpf_all.get(n, ())) for n in tests]) if tests else None,
            "merged": statistics.median([len(merged[n]) for n in tests]) if tests else None,
        },
        "ebpf_opens_seen": opens_seen,
        "ebpf_opens_attributed_to_tests": opens_attr,
        "tests_reading_noncode_files_invisible_to_coverage": tests_with_noncode,
        "top_noncode_files_invisible_to_coverage": ebpf_only_noncode.most_common(25),
        "files_executed_only_outside_tests (import time)": len(import_time_only),
        "repo_files_in_map": sum(1 for k in by_file if k.startswith("repo:")),
        "packages_by_tests": sorted(((k[4:], len(v)) for k, v in by_file.items() if k.startswith("pkg:")), key=lambda kv: -kv[1])[:25],
        "seconds_total": {"traced": round(sum(secs.values()), 1), "baseline": round(sum(secs_base.values()), 1)},
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "test_file_seconds.json"), "w") as f:
        json.dump(dict(secs), f)
    if secs_base:
        with open(os.path.join(args.out, "test_file_seconds_baseline.json"), "w") as f:
            json.dump(dict(secs_base), f)
    with open(os.path.join(args.out, "report.json"), "w") as f:
        json.dump(report, f, indent=2, default=list)
        f.write("\n")
    tpos = {t: i for i, t in enumerate(tfiles)}
    with gzip.open(os.path.join(args.out, "map_testfiles.json.gz"), "wt") as f:
        json.dump({"test_files": tfiles,
                   "file_to_test_files": {k: sorted({tpos[n.split("::")[0]] for n in v}) for k, v in by_file.items()}}, f)
    with gzip.open(os.path.join(args.out, "executed_lines.json.gz"), "wt") as f:
        json.dump({k: sorted(v) for k, v in executed.items()}, f)
    print(json.dumps(report, indent=2, default=list))


if __name__ == "__main__":
    main()
