#!/usr/bin/env python3
"""Django runtests output -> JUnit XML. Usage: django_to_junit.py LOG OUT."""
import re
import sys
import xml.sax.saxutils as su

# `method (path) ... result`, not line-anchored
_LINE = re.compile(r"(?P<method>\w+)\s+\((?P<path>[\w.]+)\)\s+\.\.\.\s*(?P<rest>.*)$")
_RESULT = re.compile(r"\b(ok|OK|FAIL|ERROR|skipped|expected failure|unexpected success)\s*$")

_MAP = {
    "ok": None, "OK": None,
    "FAIL": "failure",
    "ERROR": "error",
    "skipped": "skipped",
    "expected failure": "skipped",
    "unexpected success": "failure",
}


def key(method: str, path: str) -> tuple[str, str]:
    """(classname, name), identical for both Django line formats."""
    if path.endswith("." + method):          # new format
        path = path[: -(len(method) + 1)]
    return path, method


def parse_runtests(text: str) -> dict:
    """{(classname, name): junit tag or None}."""
    cases: dict[tuple[str, str], str | None] = {}
    pending: tuple[str, str] | None = None      # result on a later line
    for line in text.splitlines():
        line = line.rstrip()
        m = _LINE.search(line.strip())
        if m:
            k = key(m.group("method"), m.group("path"))
            r = _RESULT.search(m.group("rest") or "")
            if r:
                cases[k] = _MAP[r.group(1)]
                pending = None
            else:
                # result comes later
                pending = k
            continue
        if pending is not None:
            r = _RESULT.search(line.strip())
            if r:
                cases[pending] = _MAP[r.group(1)]
                pending = None
    return cases


def main() -> None:
    log_path, out_path = sys.argv[1], sys.argv[2]
    cases = parse_runtests(open(log_path, errors="replace").read())

    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             f'<testsuite name="django" tests="{len(cases)}">']
    for (classname, name), tag in cases.items():
        c = (f'  <testcase classname="{su.quoteattr(classname)[1:-1]}"'
             f' name="{su.quoteattr(name)[1:-1]}"')
        lines.append(c + "/>" if tag is None else c + f"><{tag}/></testcase>")
    lines.append("</testsuite>")
    open(out_path, "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
