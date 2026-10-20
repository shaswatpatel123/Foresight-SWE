"""Paths and target-repo registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPOS_DIR = REPO_ROOT / "repos"
DATA_DIR = REPO_ROOT / "data"
CHAINS_DIR = DATA_DIR / "chains"


# Recency tiers are measured back from this pinned date.
RECENCY_REFERENCE = "2026-08-03"
DOCKER_RECENCY_YEARS = 5


@dataclass(frozen=True)
class RepoTarget:
    name: str                       # short name, dir under repos/
    slug: str                       # GitHub owner/repo
    main_branch: str                # branch to mine
    python: str = "3.12"            # venv interpreter
    extras: tuple[str, ...] = ()    # optional-dependency groups
    extra_deps: tuple[str, ...] = ()  # extra pip packages
    clear_addopts: bool = False     # drop the repo's pytest addopts
    editable: bool = True           # install with `-e .`
    build_deps: tuple[str, ...] = ()  # pre-installed build deps
    no_build_isolation: bool = False  # build in place (compiled repos)
    changelog_dir: str = ""         # towncrier fragment dir
    changelog_file: str = ""        # changelog file to mine
    changelog_glob: str = ""        # github_stats files glob
    changelog_bugfix_cats: tuple[str, ...] = ("bugfix",)  # bugfix fragment types
    bugfix_commit_re: str = ""      # bugfix commit subject pattern
    runner: str = "pytest"          # pytest, django, node or go
    django_settings: str = "test_sqlite"  # django --settings
    recurse_submodules: bool = False  # clone with submodules
    language: str = "python"        # python, go or javascript

    # language -> default runner
    _RUNNER_BY_LANGUAGE = {"javascript": "node", "typescript": "node", "go": "go"}

    def __post_init__(self) -> None:
        if self.runner == "pytest" and self.language in self._RUNNER_BY_LANGUAGE:
            # frozen dataclass needs object.__setattr__
            object.__setattr__(self, "runner",
                               self._RUNNER_BY_LANGUAGE[self.language])

    @property
    def path(self) -> Path:
        return REPOS_DIR / self.name

    @property
    def langspec(self):
        from .langspec import get_spec
        return get_spec(self.language)

    @property
    def install_args(self) -> list[str]:
        """Args for `uv pip install`."""
        target = f".[{','.join(self.extras)}]" if self.extras else "."
        head = ["-e", target] if self.editable else [target]
        return [*head, *self.extra_deps]


TARGETS: dict[str, RepoTarget] = {
    # ---- Python
    # no hypothesis: its plugin breaks old pytest
    "pytest": RepoTarget("pytest", "pytest-dev/pytest", "main",
                        changelog_dir="changelog"),
    "networkx": RepoTarget("networkx", "networkx/networkx", "main",
                          extra_deps=("pytest", "numpy")),
    "mypy": RepoTarget("mypy", "python/mypy", "master",
                      extra_deps=("pytest", "pytest-xdist", "filelock")),
    "xarray": RepoTarget("xarray", "pydata/xarray", "main",
                        # numpy 2.5 breaks xarray import
                        extra_deps=("pytest", "numpy==2.4.6", "pandas"),
                        clear_addopts=True,  # addopts needs pytest-mypy
                        changelog_file="doc/whats-new.rst"),
    "urllib3": RepoTarget("urllib3", "urllib3/urllib3", "main",
                         extra_deps=("pytest", "pytest-timeout", "tornado", "trustme",
                                     "cryptography"),  # dummyserver test deps
                         changelog_dir="changelog"),
    "sqlfluff": RepoTarget("sqlfluff", "sqlfluff/sqlfluff", "main", extra_deps=("pytest",),
                          changelog_file="CHANGELOG.md"),
    "sqlglot": RepoTarget("sqlglot", "tobymao/sqlglot", "main", extra_deps=("pytest",),
                         changelog_file="CHANGELOG.md"),
    # Trac tickets, runtests.py; see mine_django.py
    "django": RepoTarget("django", "django/django", "main", runner="django"),
    # compiled; in-place build
    "matplotlib": RepoTarget("matplotlib", "matplotlib/matplotlib", "main",
                            extra_deps=("pytest",),
                            build_deps=("meson-python", "meson", "ninja", "pybind11",
                                        "numpy", "setuptools-scm"),
                            no_build_isolation=True,
                            changelog_glob="doc/release/**/github_stats*.rst"),
    # compiled; in-place build
    "astropy": RepoTarget("astropy", "astropy/astropy", "main",
                         extra_deps=("pytest", "pytest-astropy-header", "pytest-doctestplus"),
                         build_deps=("setuptools", "setuptools-scm", "cython", "numpy",
                                     "extension-helpers", "wheel"),
                         no_build_isolation=True,
                         changelog_file="CHANGES.rst"),
    # compiled; fixers from `BUG:` commits
    "pandas": RepoTarget("pandas", "pandas-dev/pandas", "main",
                        extra_deps=("pytest", "hypothesis", "pytest-xdist", "tzdata"),
                        # numpy pinned at build time (ABI)
                        build_deps=("meson-python", "meson", "ninja", "cython",
                                    "numpy==2.4.6", "wheel", "versioneer"),
                        no_build_isolation=True,
                        bugfix_commit_re=r"^BUG:"),
    # compiled, with submodules
    "numpy": RepoTarget("numpy", "numpy/numpy", "main",
                       extra_deps=("pytest", "hypothesis"),
                       build_deps=("meson-python", "cython", "ninja"),
                       no_build_isolation=True, recurse_submodules=True,
                       bugfix_commit_re=r"^BUG:"),
    # compiled, with submodules
    "scipy": RepoTarget("scipy", "scipy/scipy", "main",
                       extra_deps=("pytest", "hypothesis", "pytest-xdist"),
                       build_deps=("meson-python", "cython", "ninja", "pythran",
                                   "pybind11", "numpy"),
                       no_build_isolation=True, recurse_submodules=True,
                       bugfix_commit_re=r"^BUG:"),
    "httpx": RepoTarget("httpx", "encode/httpx", "master",
                       extra_deps=("pytest", "trustme", "pytest-trio", "anyio[trio]"),
                       changelog_file="CHANGELOG.md"),
    "attrs": RepoTarget("attrs", "python-attrs/attrs", "main",
                       extra_deps=("pytest", "hypothesis")),
    "packaging": RepoTarget("packaging", "pypa/packaging", "main", extra_deps=("pytest",),
                           changelog_file="CHANGELOG.rst"),
    "tomlkit": RepoTarget("tomlkit", "sdispater/tomlkit", "master", extra_deps=("pytest",),
                         changelog_file="CHANGELOG.md"),
    "starlette": RepoTarget("starlette", "encode/starlette", "main",
                           extra_deps=("pytest", "httpx", "anyio", "trio"),
                           changelog_file="docs/release-notes.md"),
    # fixers from `FIX` commits
    "sklearn": RepoTarget("sklearn", "scikit-learn/scikit-learn", "main", extra_deps=("pytest",),
                         bugfix_commit_re=r"^FIX\b"),
    # conftest imports jsonschema; addopts need pytest-benchmark
    "pydantic": RepoTarget("pydantic", "pydantic/pydantic", "main",
                          extra_deps=("pytest", "jsonschema", "dirty-equals",
                                      "pytest-mock", "pytest-examples", "faker",
                                      "pytz", "packaging", "cloudpickle",
                                      "pytest-benchmark",
                                      "pytest-run-parallel"),  # thread_unsafe marker
                          changelog_file="HISTORY.md"),
    "aiohttp": RepoTarget("aiohttp", "aio-libs/aiohttp", "master", extra_deps=("pytest",),
                         changelog_dir="CHANGES"),
    "pillow": RepoTarget("pillow", "python-pillow/Pillow", "main", extra_deps=("pytest",),
                        changelog_file="CHANGES.rst"),

    # ---- Go
    "prometheus": RepoTarget("prometheus", "prometheus/prometheus", "main", language="go",
                            changelog_file="CHANGELOG.md"),
    "gin": RepoTarget("gin", "gin-gonic/gin", "master", language="go",
                     changelog_file="CHANGELOG.md"),
    "caddy": RepoTarget("caddy", "caddyserver/caddy", "master", language="go"),
    "gitea": RepoTarget("gitea", "go-gitea/gitea", "main", language="go"),
    "argocd": RepoTarget("argocd", "argoproj/argo-cd", "master", language="go"),
    "helm": RepoTarget("helm", "helm/helm", "main", language="go"),

    # ---- JavaScript / TypeScript
    "svelte": RepoTarget("svelte", "sveltejs/svelte", "main", language="javascript",
                        changelog_file="packages/svelte/CHANGELOG.md"),
    "axios": RepoTarget("axios", "axios/axios", "v1.x", language="javascript",
                       changelog_file="CHANGELOG.md"),
    "vite": RepoTarget("vite", "vitejs/vite", "main", language="javascript",
                      changelog_file="packages/vite/CHANGELOG.md"),
    "vuecore": RepoTarget("vuecore", "vuejs/core", "main", language="javascript",
                         changelog_file="CHANGELOG.md"),
    "n8n": RepoTarget("n8n", "n8n-io/n8n", "master", language="javascript"),
    "astro": RepoTarget("astro", "withastro/astro", "main", language="javascript"),
    "hono": RepoTarget("hono", "honojs/hono", "main", language="javascript"),
}


def get_target(name: str) -> RepoTarget:
    try:
        return TARGETS[name]
    except KeyError:
        known = ", ".join(sorted(TARGETS))
        raise SystemExit(f"unknown repo target {name!r}; known targets: {known}")
