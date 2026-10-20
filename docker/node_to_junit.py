#!/usr/bin/env python3
"""JS JUnit -> (test file, full title) keys."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def full_title(tc: ET.Element) -> str:
    """Test name with its suite path."""
    name = (tc.get("name") or "").strip()
    cls = (tc.get("classname") or "").strip()
    if cls and cls not in name:
        return f"{cls} > {name}" if name else cls
    return name


def main() -> None:
    if len(sys.argv) < 4 or len(sys.argv) % 2 != 0:
        sys.exit("usage: node_to_junit.py OUT.xml FILE1 RAW1.xml [FILE2 RAW2.xml ...]")
    out_path, rest = sys.argv[1], sys.argv[2:]

    suite = ET.Element("testsuite", {"name": "foresight"})
    n_tests = n_fail = n_skip = 0

    for i in range(0, len(rest), 2):
        test_file, raw_path = rest[i], rest[i + 1]
        try:
            root = ET.parse(raw_path).getroot()
        except (ET.ParseError, OSError):
            # no XML: shows up as env_failed
            continue
        for tc in root.iter("testcase"):
            n_tests += 1
            attrs = {"classname": test_file, "name": full_title(tc)}
            if tc.get("time"):
                attrs["time"] = tc.get("time")
            new = ET.SubElement(suite, "testcase", attrs)
            for tag in ("failure", "error", "skipped"):
                child = tc.find(tag)
                if child is not None:
                    ET.SubElement(new, tag, dict(child.attrib))
                    if tag == "skipped":
                        n_skip += 1
                    else:
                        n_fail += 1
                    break

    suite.set("tests", str(n_tests))
    suite.set("failures", str(n_fail))
    suite.set("skipped", str(n_skip))
    ET.ElementTree(suite).write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"node_to_junit: {n_tests} testcase(s), {n_fail} failing, {n_skip} skipped "
          f"-> {out_path}")


if __name__ == "__main__":
    main()
