#!/usr/bin/env python3
"""This is what happens when you do `python -m minisweagent`.

This is a foresight-only trim of mini-swe-agent; the entry point is the
`mini-extra` dispatcher (whose only registered subcommand is `foresight`).
"""

from minisweagent.run.mini_extra import main

if __name__ == "__main__":
    main()
