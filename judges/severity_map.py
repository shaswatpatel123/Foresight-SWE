#!/usr/bin/env python3
"""Map impact, reach, detectability to a severity tier."""
from __future__ import annotations

import json
from pathlib import Path

CALIB = Path(__file__).resolve().parents[1] / "pass-1" / "calibration_set.json"


def derive_severity(impact: str, reach: str, detectability: str) -> str:
    silent = detectability == "silent"
    if impact == "corruption":
        return "critical" if reach == "broad" else "major"
    if impact == "broken":
        if reach == "broad":
            return "major"
        if reach == "narrow":
            return "major" if silent else "minor"
        return "minor"  # rare
    # degraded / cosmetic
    return "minor"


def main() -> None:
    data = json.loads(CALIB.read_text())
    chains = data["chains"]
    resolved = [c for c in chains if c.get("status") == "resolved"]
    contested = [c for c in chains if c.get("status") != "resolved"]

    print(f"Calibration check — {len(resolved)} resolved chains "
          f"({len(contested)} contested, excluded)\n")
    hdr = f"{'instance':24s} {'impact':10s} {'reach':7s} {'detect':7s} | {'derived':8s} {'audit':8s}  ok"
    print(hdr); print("-" * len(hdr))
    ok = 0
    for c in resolved:
        a = c["audit"]
        got = derive_severity(a["impact"], a["reach"], a["detectability"])
        want = a["tier"]
        match = got == want
        ok += match
        print(f"{c['instance_id']:24s} {a['impact']:10s} {a['reach']:7s} "
              f"{a['detectability']:7s} | {got:8s} {want:8s}  {'✓' if match else '✗ MISMATCH'}")
    print("-" * len(hdr))
    print(f"\n{ok}/{len(resolved)} reproduce the hand-audited tier"
          + ("  ✅ PASS" if ok == len(resolved) else "  ❌ FAIL"))
    if contested:
        print("\ncontested (need resolution, not scored):")
        for c in contested:
            a = c["audit"]
            print(f"  {c['instance_id']:24s} {a}")


if __name__ == "__main__":
    main()
