#!/usr/bin/env python3
"""Parse Claude/Codex JSON output into steps.

Adapted from slop-code-bench's trajectory parsers
(https://github.com/SprocketLab/slop-code-bench), MIT License,
Copyright (c) 2025 gabeorlanski. See LICENSE.slop-code-bench.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ParseError(Exception):
    """Raised when trajectory parsing fails."""


@dataclass
class UserStep:
    content: str
    step_type: str = "user"


@dataclass
class AgentStep:
    content: str
    step_type: str = "agent"


@dataclass
class ThinkingStep:
    content: str
    step_type: str = "thinking"


@dataclass
class ToolUseStep:
    type: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    step_type: str = "tool_use"


@dataclass
class Trajectory:
    agent_type: str
    steps: list = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "agent_type": self.agent_type,
            "metadata": self.metadata,
            "steps": [_step_as_dict(s) for s in self.steps],
        }

    def to_messages(self) -> list[dict]:
        """Render steps as role-based messages for the mini trajectory file."""
        msgs: list[dict] = []
        for s in self.steps:
            if isinstance(s, UserStep):
                msgs.append({"role": "user", "content": s.content})
            elif isinstance(s, ThinkingStep):
                msgs.append({"role": "assistant", "content": f"<thinking>\n{s.content}\n</thinking>"})
            elif isinstance(s, AgentStep):
                msgs.append({"role": "assistant", "content": s.content})
            elif isinstance(s, ToolUseStep):
                msgs.append({"role": "assistant",
                             "content": f"[tool:{s.type}] {json.dumps(s.arguments, default=str)}"})
                if s.result is not None:
                    msgs.append({"role": "user", "content": f"<tool_result>\n{_stringify(s.result)}\n</tool_result>"})
        return msgs


def _step_as_dict(step) -> dict:
    d = {"step_type": step.step_type}
    if isinstance(step, ToolUseStep):
        d.update({"type": step.type, "arguments": step.arguments, "result": _stringify(step.result)})
    else:
        d["content"] = step.content
    return d


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _iter_events(text: str):
    for line_num, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # Tolerate interleaved non-JSON noise (e.g. install logs); skip it.
            continue


# --- Claude Code (stream-json) -------------------------------------------------
def parse_claude_code(text: str) -> Trajectory:
    steps: list = []
    metadata: dict = {}
    pending: dict[str, int] = {}  # tool_use_id -> step index

    for event in _iter_events(text):
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            metadata = {
                "model": event.get("model"),
                "tools": event.get("tools", []),
                "session_id": event.get("session_id"),
                "cwd": event.get("cwd"),
            }
        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                btype = block.get("type")
                if btype == "thinking" and block.get("thinking"):
                    steps.append(ThinkingStep(content=block["thinking"]))
                elif btype == "text" and block.get("text"):
                    steps.append(AgentStep(content=block["text"]))
                elif btype == "tool_use":
                    steps.append(ToolUseStep(type=block.get("name", ""),
                                             arguments=block.get("input", {}) or {}, result=None))
                    if block.get("id"):
                        pending[block["id"]] = len(steps) - 1
        elif etype == "user":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    tid = block.get("tool_use_id", "")
                    if tid in pending:
                        idx = pending.pop(tid)
                        if isinstance(steps[idx], ToolUseStep):
                            steps[idx].result = block.get("content", "")
        elif etype == "result":
            metadata["total_cost_usd"] = event.get("total_cost_usd")
            metadata["num_turns"] = event.get("num_turns")

    return Trajectory(agent_type="claude_code", steps=steps, metadata=metadata)


# --- Codex (exec --json) -------------------------------------------------------
def _unwrap_bash(command: str) -> str:
    if command.startswith("/bin/bash"):
        m = re.search(r"-[lc]+\s+['\"](.+)", command, re.DOTALL)
        if m:
            return m.group(1).rstrip("'\"")
        return re.sub(r"^/bin/bash\s+-[lc]+\s+", "", command)
    return command


def parse_codex(text: str) -> Trajectory:
    steps: list = []
    metadata: dict = {}

    for event in _iter_events(text):
        etype = event.get("type")
        if etype == "thread.started":
            metadata["thread_id"] = event.get("thread_id")
            if "model" in event:
                metadata["model"] = event["model"]
        elif etype == "item.completed":
            item = event.get("item", {})
            itype = item.get("type")
            if itype == "reasoning" and item.get("text"):
                steps.append(ThinkingStep(content=item["text"]))
            elif itype == "command_execution":
                steps.append(ToolUseStep(
                    type="bash",
                    arguments={"command": _unwrap_bash(item.get("command", "")),
                               "exit_code": item.get("exit_code")},
                    result=item.get("aggregated_output", ""),
                ))
            elif itype == "file_change":
                steps.append(ToolUseStep(type="file_change",
                                         arguments={"changes": item.get("changes", [])}, result=None))
            elif itype == "agent_message" and item.get("text"):
                steps.append(AgentStep(content=item["text"]))

    return Trajectory(agent_type="codex", steps=steps, metadata=metadata)


_PARSERS = {"claude_code": parse_claude_code, "codex": parse_codex}


def parse_cli_trajectory(backend: str, *, text: str | None = None,
                         path: Path | None = None) -> Trajectory:
    """Parse CLI stdout (text or path) into steps."""
    if text is None:
        if path is None:
            raise ParseError("parse_cli_trajectory needs text= or path=")
        text = Path(path).read_text(errors="replace")
    parser = _PARSERS.get(backend)
    if parser is None:
        raise ParseError(f"no trajectory parser for backend {backend!r}")
    return parser(text)
