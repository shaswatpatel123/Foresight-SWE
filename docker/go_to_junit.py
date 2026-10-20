#!/usr/bin/env python3
"""Usage: go_to_junit.py OUT.xml EVENTS.jsonl [EVENTS2.jsonl ...]"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

# terminal actions, by precedence
_TERMINAL = ("fail", "skip", "pass")

# plain-text "FAIL <pkg> [build failed]"
_BUILD_FAIL = re.compile(r"^FAIL\s+(\S+)\s+\[\S+ failed\]")


def parse(paths: list[str]):
    """(results, pkg_output, pkg_saw_test) from one or more -json streams."""
    results: dict[tuple[str, str], str] = {}
    output: dict[tuple[str, str], list[str]] = defaultdict(list)
    pkg_status: dict[str, str] = {}
    pkg_output: dict[str, list[str]] = defaultdict(list)
    pkg_saw_test: set[str] = set()

    for path in paths:
        with open(path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or not line.startswith("{"):
                    # keep build-failure lines
                    m = _BUILD_FAIL.match(line)
                    if m:
                        pkg_status[m.group(1)] = "fail"
                        pkg_output[m.group(1)].append(line + "\n")
                    continue          # skip non-JSON lines
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pkg, test = ev.get("Package"), ev.get("Test")
                action = ev.get("Action")
                if not pkg:
                    continue
                if test:
                    pkg_saw_test.add(pkg)
                    if action == "output":
                        output[(pkg, test)].append(ev.get("Output") or "")
                    elif action in _TERMINAL:
                        results[(pkg, test)] = action
                else:
                    if action == "output":
                        pkg_output[pkg].append(ev.get("Output") or "")
                    elif action in _TERMINAL:
                        pkg_status[pkg] = action
    return results, output, pkg_status, pkg_output, pkg_saw_test


def build(results, output, pkg_status, pkg_output, pkg_saw_test) -> ET.Element:
    suite = ET.Element("testsuite", {"name": "go"})
    n_fail = n_skip = 0

    for (pkg, test), action in sorted(results.items()):
        tc = ET.SubElement(suite, "testcase", {"classname": pkg, "name": test})
        if action == "fail":
            n_fail += 1
            ET.SubElement(tc, "failure", {"message": "test failed"}).text = \
                "".join(output[(pkg, test)])[-8000:]
        elif action == "skip":
            n_skip += 1
            ET.SubElement(tc, "skipped", {"message": "skipped"})

    # failed with no tests: synthetic [package] case
    for pkg, status in sorted(pkg_status.items()):
        if status == "fail" and pkg not in pkg_saw_test:
            tc = ET.SubElement(suite, "testcase",
                               {"classname": pkg, "name": "[package]"})
            ET.SubElement(tc, "error",
                          {"message": "package reported no tests and failed"}).text = \
                "".join(pkg_output[pkg])[-8000:]
            n_fail += 1

    total = len(suite.findall("testcase"))
    suite.set("tests", str(total))
    suite.set("failures", str(n_fail))
    suite.set("skipped", str(n_skip))
    suite.set("errors", "0")
    return suite


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    out, streams = sys.argv[1], sys.argv[2:]
    suite = build(*parse(streams))
    ET.ElementTree(suite).write(out, encoding="utf-8", xml_declaration=True)
    print(f"go_to_junit: {suite.get('tests')} testcases "
          f"({suite.get('failures')} failed, {suite.get('skipped')} skipped) -> {out}")


if __name__ == "__main__":
    main()
