"""Per-language test, source and comment rules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LangSpec:
    name: str
    source_exts: tuple[str, ...]        # source file extensions
    comment_prefixes: tuple[str, ...]   # comment line prefixes

    def is_test_path(self, path: str) -> bool:
        raise NotImplementedError

    def is_source(self, path: str) -> bool:
        """Source file, not a test."""
        return path.endswith(self.source_exts) and not self.is_test_path(path)

    def is_blamable(self, content: str) -> bool:
        """Not blank, not a comment."""
        s = content.strip()
        if not s:
            return False
        return not s.startswith(self.comment_prefixes)


class _Python(LangSpec):
    def is_test_path(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        if not name.endswith(".py"):
            return False
        return (
            path.startswith("testing/")
            or "/tests/" in f"/{path}"
            or "/test/" in f"/{path}"       # singular test/ dir
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        )


class _Go(LangSpec):
    def is_test_path(self, path: str) -> bool:
        return path.endswith("_test.go")


class _JsTs(LangSpec):
    def is_test_path(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        if not name.endswith(self.source_exts):
            return False
        p = f"/{path}"
        if ("/__tests__/" in p or "/test/" in p or "/tests/" in p
                or "/spec/" in p or "/e2e/" in p):
            return True
        stem = name.rsplit(".", 1)[0]                 # foo.test.ts -> foo.test
        return stem.endswith((".test", ".spec", "-test", "-spec", "_test", "_spec"))


PYTHON = _Python("python", (".py",), ("#", '"', "'"))
GO = _Go("go", (".go",), ("//", "/*", "*"))
# JS and TS share one spec
JAVASCRIPT = _JsTs(
    "javascript",
    (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"),
    ("//", "/*", "*"),
)

SPECS: dict[str, LangSpec] = {
    s.name: s for s in (PYTHON, GO, JAVASCRIPT)
}
# aliases
SPECS["typescript"] = JAVASCRIPT
SPECS["js"] = JAVASCRIPT
SPECS["ts"] = JAVASCRIPT


def get_spec(language: str) -> LangSpec:
    try:
        return SPECS[language]
    except KeyError:
        raise SystemExit(
            f"unknown language {language!r}; known: {', '.join(sorted(SPECS))}")


def changed_test_files(repo_path, base: str, head: str,
                       language: str = "python") -> list[str]:
    """Changed test files that still exist at head."""
    import subprocess

    def _g(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo_path), *args],
                              capture_output=True, text=True,
                              errors="replace").stdout

    spec = get_spec(language)
    changed = [p for p in _g("diff", "--name-only", base, head).splitlines()
               if p and spec.is_test_path(p)]
    present = set(_g("ls-tree", "-r", "--name-only", head).splitlines())
    return [p for p in changed if p in present]
