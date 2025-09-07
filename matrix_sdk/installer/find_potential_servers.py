#!/usr/bin/env python3
# ===================================================================
# file: matrix_sdk/installer/find_potential_servers.py (Refactored)
# ===================================================================
# SPDX-License-Identifier: MIT
"""
Heuristically locate and rank likely server entry points in a project tree
for Python, Node.js/TypeScript, and Go.

This high-performance, cross-platform script uses a pluggable scanner
architecture to identify candidate files based on naming conventions,
file content, and project-level configuration (e.g., package.json).

Output (default, back-compat):
  - <relative/path>

CLI:
  --top N      limit to best N results (default: 10)
  --json       emit JSON array [{path, score, reasons}]
  --debug      print scoring reasons to stderr
  --ext        extra extensions to scan (comma-separated, e.g., ".sh,.rb")

Environment toggles:
  MATRIX_FINDER_MAX_FILES        (int, default 8000)      – hard cap on files scanned
  MATRIX_FINDER_MAX_READ_BYTES   (int, default 131072)    – bytes read per file
  MATRIX_FINDER_SKIP_DIRS        (csv)                    – extra dirs to skip
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

# Optional TOML parser (stdlib on Python 3.11+; no hard dependency for speed)
try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    tomllib = None

# --------------------------- Configuration --------------------------- #

MAX_FILES_DEFAULT = int(os.getenv("MATRIX_FINDER_MAX_FILES", "8000"))
MAX_READ_BYTES = int(os.getenv("MATRIX_FINDER_MAX_READ_BYTES", str(128 * 1024)))

DEFAULT_SKIP_DIRS: Set[str] = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "out",
    ".idea",
    ".vscode",
    ".DS_Store",
    ".cache",
    "target",
    "bin",
    "obj",
}

# --------------------------- Data Model ------------------------------ #


@dataclass
class Candidate:
    """Represents a potential server entry point file."""

    path: Path
    rel_path: Path
    score: int = 0
    reasons: List[str] = field(default_factory=list)

    def add_score(self, points: int, reason: str):
        self.score += points
        self.reasons.append(f"{reason}+{points}")

    def __lt__(self, other: "Candidate") -> bool:
        # Sort by score (desc), then path depth (asc), then lexicographically
        if self.score != other.score:
            return self.score > other.score
        depth_self = len(self.rel_path.parts)
        depth_other = len(other.rel_path.parts)
        if depth_self != depth_other:
            return depth_self < depth_other
        return self.rel_path.as_posix() < other.rel_path.as_posix()


# ------------------- Pluggable Scanner Architecture -------------------- #


class BaseScanner:
    """Base class for different scanning strategies."""

    def __init__(self, root: Path, debug: bool = False):
        self.root = root
        self.debug = debug

    def scan(self) -> List[Candidate]:
        raise NotImplementedError


# ------------------- Utilities & shared helpers ------------------------ #

_SHEBANG_PY = re.compile(rb"^#!.*\bpython[0-9.]*\b", re.I)
_SHEBANG_NODE = re.compile(rb"^#!.*\b(node|bun|deno)\b", re.I)


def _shebang_score(data: bytes) -> Tuple[int, Optional[str]]:
    if _SHEBANG_PY.match(data):
        return 3, "shebang:python"
    if _SHEBANG_NODE.match(data):
        return 3, "shebang:node"
    return 0, None


def _relpath(root: Path, p: Path) -> Path:
    try:
        return p.resolve().relative_to(root.resolve())
    except Exception:
        return Path(p.name)


def _safe_walk(
    root: Path, skip_dirs: Set[str]
) -> Iterator[Tuple[Path, List[str], List[str]]]:
    """os.walk with directory pruning for speed & safety (no symlink traversal)."""
    for cur, dirs, files in os.walk(root, topdown=True, followlinks=False):
        keep: List[str] = []
        for d in list(dirs):
            # Skip huge/generated/hidden folders except explicitly whitelisted
            if d in skip_dirs:
                continue
            if d.startswith(".") and d not in {".well-known"}:
                continue
            keep.append(d)
        dirs[:] = keep
        yield Path(cur), dirs, files


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text("utf-8", "ignore"))
    except Exception:
        return None


def _read_head(path: Path, limit: int = MAX_READ_BYTES) -> bytes:
    try:
        with path.open("rb") as f:
            return f.read(limit)
    except Exception:
        return b""


# ------------------------- Project-level scanners ------------------------- #


class PackageJsonScanner(BaseScanner):
    """Creates candidates from package.json scripts & fields; adds dep bias."""

    FILENAME = "package.json"

    def scan(self) -> List[Candidate]:
        path = self.root / self.FILENAME
        if not path.is_file():
            return []

        obj = _load_json(path) or {}
        deps = {**(obj.get("dependencies") or {}), **(obj.get("devDependencies") or {})}
        dep_bias = 0
        # Frameworks & MCP libs add bias
        for name, w in {
            "express": 8,
            "fastify": 8,
            "h3": 6,
            "@anthropic-ai/mcp": 12,
            "@modelcontextprotocol/sdk": 12,
            "mcp": 10,
        }.items():
            if name in deps:
                dep_bias += w

        candidates: List[Candidate] = []

        # Scripts that reference a file; prefer start/serve/dev/server/run
        scripts = obj.get("scripts", {}) or {}
        for sname in ("start", "serve", "dev", "server", "run"):
            val = str(scripts.get(sname, "")).strip()
            if not val:
                continue
            # Look for <tool> <file>.* extension
            m = re.search(r"(\S+\.(?:js|ts|mjs|cjs))\b", val)
            if m:
                entry = m.group(1)
                p = (self.root / entry).resolve()
                if p.is_file():
                    c = Candidate(p, _relpath(self.root, p))
                    c.add_score(15 + dep_bias, f"pkg:script:{sname}")
                    candidates.append(c)

        # 'main' / 'module' / 'exports' (string only)
        for key in ("main", "module", "exports"):
            v = obj.get(key)
            if isinstance(v, str) and v:
                p = (self.root / v).resolve()
                if p.is_file():
                    c = Candidate(p, _relpath(self.root, p))
                    c.add_score(7 + dep_bias, f"pkg:{key}")
                    candidates.append(c)

        return candidates


class PyProjectScanner(BaseScanner):
    """Adds Python project bias from pyproject.toml dependencies (fast & safe)."""

    FILENAME = "pyproject.toml"

    def scan(self) -> List[Candidate]:
        path = self.root / self.FILENAME
        if not path.is_file():
            return []

        score = 0
        reasons: List[str] = []

        try:
            if tomllib:  # exact parse when available
                obj = tomllib.loads(path.read_text("utf-8", "ignore"))
                # poetry
                poetry_deps = ((obj.get("tool") or {}).get("poetry") or {}).get(
                    "dependencies"
                ) or {}
                # PEP 621
                proj_deps = (obj.get("project") or {}).get("dependencies") or []
                dep_names = set(map(str.lower, poetry_deps.keys())) | {
                    str(x).split()[0].lower() for x in proj_deps if isinstance(x, str)
                }
                for nm in (
                    "fastapi",
                    "uvicorn",
                    "flask",
                    "starlette",
                    "quart",
                    "sanic",
                    "litestar",
                    "mcp",
                    "fastmcp",
                ):
                    if nm in dep_names:
                        score += 3
                        reasons.append(f"pyproject:{nm}")
            else:  # fallback: string search
                lower = path.read_text("utf-8", "ignore").lower()
                for nm in (
                    "fastapi",
                    "uvicorn",
                    "flask",
                    "starlette",
                    "quart",
                    "sanic",
                    "litestar",
                    "mcp",
                    "fastmcp",
                ):
                    if nm in lower:
                        score += 2
                        reasons.append(f"pyproject:{nm}")
        except Exception:
            pass

        if score:
            # bias-candidate: applied to per-file Python candidates by the orchestrator
            return [Candidate(path, _relpath(self.root, path), score, reasons)]
        return []


class RequirementsScanner(BaseScanner):
    """Adds bias if requirements.txt indicates web/MCP usage."""

    FILENAME = "requirements.txt"

    def scan(self) -> List[Candidate]:
        path = self.root / self.FILENAME
        if not path.is_file():
            return []

        try:
            txt = path.read_text("utf-8", "ignore").lower()
        except Exception:
            return []

        score = 0
        reasons: List[str] = []
        for nm in (
            "fastapi",
            "uvicorn",
            "flask",
            "starlette",
            "quart",
            "sanic",
            "litestar",
            "mcp",
            "fastmcp",
            "sse-starlette",
            "aiohttp-sse",
        ):
            if nm in txt:
                score += 2
                reasons.append(f"requirements:{nm}")

        if score:
            return [Candidate(path, _relpath(self.root, path), score, reasons)]
        return []


class GoModScanner(BaseScanner):
    """Adds bias if go.mod references web/MCP frameworks."""

    FILENAME = "go.mod"

    def scan(self) -> List[Candidate]:
        path = self.root / self.FILENAME
        if not path.is_file():
            return []

        try:
            txt = path.read_text("utf-8", "ignore")
        except Exception:
            return []

        score = 0
        reasons: List[str] = []
        for nm in (
            "github.com/modelcontextprotocol/sdk-go",
            "github.com/gin-gonic/gin",
            "github.com/gofiber/fiber",
            "github.com/labstack/echo",
            "github.com/go-chi/chi",
            "github.com/gorilla/mux",
            "github.com/r3labs/sse",
            "github.com/gin-contrib/sse",
        ):
            if nm.lower() in txt.lower():
                score += 4
                reasons.append(f"go.mod:{nm}")

        if score:
            return [Candidate(path, _relpath(self.root, path), score, reasons)]
        return []


# ------------------------- File content scanner --------------------------- #


class FileContentScanner(BaseScanner):
    """Scans source files for language-specific keywords (fast path)."""

    # Performance: pre-compiled regexes (much faster than many `in` checks)
    HINTS_PY = re.compile(
        b"|".join(
            [
                b"FastAPI",
                b"fastapi",
                b"Flask",
                b"flask",
                b"uvicorn",
                b"starlette",
                b"aiohttp",
                b"quart",
                b"sanic",
                b"litestar",
                b"StreamingResponse",
                b"text/event-stream",
                b"/sse",
                b"/_sse",
                b"EventSource",
                b"__name__ == '__main__'",
                b'__name__ == "__main__"',
                b"\bmcp\b",
                b"Model Context Protocol",
                b"fastmcp",
            ]
        ),
        re.I,
    )

    HINTS_JS = re.compile(
        b"|".join(
            [
                b"express()",
                b"require('express')",
                b'require("express")',
                b"from 'express'",
                b'from "express"',
                b"fastify()",
                b"from 'fastify'",
                b'from "fastify"',
                b"createServer",
                b"\\.listen\\(",
                b"text/event-stream",
                b"/sse",
                b"/_sse",
                b"EventSource",
                b"ReadableStream",
                b"server-sent events",
                b"import { createServer }",
                b"export default",
                b"@anthropic-ai/mcp",
                b"@modelcontextprotocol/sdk",
                b"\bmcp\b",
                b"Model Context Protocol",
            ]
        ),
        re.I,
    )

    HINTS_GO = re.compile(
        b"|".join(
            [
                b"package main",
                b"func main()",
                b"net/http",
                b"http.ListenAndServe",
                b"http.HandleFunc",
                b"github.com/gin-gonic/gin",
                b"github.com/gofiber/fiber",
                b"github.com/labstack/echo",
                b"github.com/go-chi/chi",
                b"github.com/gorilla/mux",
                b"github.com/r3labs/sse",
                b"github.com/gin-contrib/sse",
                b"text/event-stream",
                b"/sse",
                b"/_sse",
                b"EventSource",
                b"github.com/modelcontextprotocol/sdk-go",
                b"Model Context Protocol",
            ]
        ),
        re.I,
    )

    FILENAME_SCORES: Dict[str, int] = {
        "main.go": 12,
        "server.py": 10,
        "app.py": 10,
        "main.py": 10,
        "run.py": 8,
        "wsgi.py": 8,
        "asgi.py": 8,
        "mcp_server.py": 12,
        "server.js": 10,
        "index.js": 8,
        "app.js": 8,
        "api.js": 8,
        "server.cjs": 8,
        "index.cjs": 6,
        "server.mjs": 8,
        "index.mjs": 6,
        "server.ts": 10,
        "index.ts": 8,
        "app.ts": 8,
        "api.ts": 8,
    }
    FILENAME_STEM_SCORES: Dict[str, int] = {"server": 5, "app": 5, "api": 5, "main": 5}

    EXTENSIONS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".go"}

    def scan_file(self, path: Path) -> Optional[Candidate]:
        ext = path.suffix.lower()
        if ext not in self.EXTENSIONS:
            return None

        c = Candidate(path, _relpath(self.root, path))

        # 1) File-name scores
        if path.name in self.FILENAME_SCORES:
            c.add_score(self.FILENAME_SCORES[path.name], f"name:{path.name}")
        elif path.stem in self.FILENAME_STEM_SCORES:
            c.add_score(self.FILENAME_STEM_SCORES[path.stem], f"name_stem:{path.stem}")

        # 2) Content & shebang
        head = _read_head(path)
        regex = (
            self.HINTS_PY
            if ext == ".py"
            else (
                self.HINTS_JS
                if ext in {".js", ".mjs", ".cjs", ".ts", ".tsx"}
                else (self.HINTS_GO if ext == ".go" else None)
            )
        )
        if regex:
            # unique matches to avoid double-counting
            hits = set(m.lower() for m in regex.findall(head))
            if hits:
                c.add_score(len(hits) * 2, "content_hints")

        s, tag = _shebang_score(head)
        if s:
            c.add_score(s, tag or "shebang")

        # 3) Light generic hints of servers
        if ext in {".js", ".mjs", ".cjs", ".ts", ".tsx"} and b".listen(" in head:
            c.add_score(3, "js:listen")
        if ext == ".py" and b"app.run(" in head:
            c.add_score(2, "py:app.run")
        if ext == ".go" and b"ListenAndServe" in head:
            c.add_score(3, "go:ListenAndServe")

        return c if c.score > 0 else None


# ------------------------- Orchestrator Class ------------------------ #


class ServerFinder:
    """Orchestrates the discovery of potential server files."""

    def __init__(self, root: Path, top_n: int, exts: Set[str], debug: bool):
        self.root = root.resolve()
        self.top_n = max(1, int(top_n))
        self.extensions = set(FileContentScanner.EXTENSIONS) | {
            e for e in exts if e.startswith(".")
        }
        self.debug = debug
        self.skip_dirs = DEFAULT_SKIP_DIRS | {
            s.strip()
            for s in (os.getenv("MATRIX_FINDER_SKIP_DIRS") or "").split(",")
            if s.strip()
        }
        self.results: Dict[Path, Candidate] = {}

    def _add_or_update_candidate(self, new_candidate: Candidate):
        """Adds a candidate or updates the score of an existing one."""
        rel_path = new_candidate.rel_path
        cur = self.results.get(rel_path)
        if cur is None or new_candidate.score > cur.score:
            self.results[rel_path] = new_candidate
        else:
            # Merge scores/reasons to accumulate independent evidence
            cur.score += new_candidate.score
            cur.reasons.extend(new_candidate.reasons)

    def _walk_files(self) -> Iterator[Path]:
        """Efficiently walk the directory tree, yielding relevant files."""
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(
            self.root, topdown=True, followlinks=False
        ):
            # Prune directories in-place (significant perf boost)
            dirnames[:] = [
                d for d in dirnames if d not in self.skip_dirs and not d.startswith(".")
            ]

            for filename in filenames:
                if file_count >= MAX_FILES_DEFAULT:
                    if self.debug:
                        print(
                            f"[debug] Reached file limit of {MAX_FILES_DEFAULT}",
                            file=sys.stderr,
                        )
                    return

                path = Path(dirpath) / filename
                if path.suffix.lower() in self.extensions:
                    file_count += 1
                    yield path

    def _runner_json_hint(self) -> Optional[Candidate]:
        """Strongly favor an explicit runner.json entry if present."""
        p = self.root / "runner.json"
        if not p.is_file():
            return None
        try:
            obj = json.loads(p.read_text("utf-8", "ignore"))
            entry: Optional[str] = None
            if isinstance(obj, dict):
                if isinstance(obj.get("entry"), str) and obj["entry"]:
                    entry = obj["entry"]
                else:
                    proc = obj.get("process") or {}
                    cmd = proc.get("command")
                    if isinstance(cmd, list) and cmd:
                        for t in cmd:
                            if isinstance(t, str) and any(
                                t.endswith(suf)
                                for suf in (".py", ".js", ".ts", ".go", ".mjs", ".cjs")
                            ):
                                entry = t
                                break
            if entry:
                ep = (self.root / entry).resolve()
                c = Candidate(ep, _relpath(self.root, ep), 100, ["runner.json:entry"])
                return c
        except Exception:
            return None
        return None

    def _go_cmd_main_hints(self) -> List[Candidate]:
        """Conventional Go layout: cmd/<name>/main.go."""
        out: List[Candidate] = []
        cmd_dir = self.root / "cmd"
        if cmd_dir.is_dir():
            for child in cmd_dir.iterdir():
                if child.is_dir():
                    mg = child / "main.go"
                    if mg.is_file():
                        out.append(
                            Candidate(
                                mg.resolve(),
                                _relpath(self.root, mg),
                                20,
                                ["go:cmd/*/main.go"],
                            )
                        )
        return out

    def run(self) -> List[Candidate]:
        """Execute all scanning strategies and return sorted results."""
        if not self.root.is_dir():
            return []

        # 1) Project-level scanners for bias & direct hints
        project_scanners: List[BaseScanner] = [
            PackageJsonScanner(self.root, self.debug),
            PyProjectScanner(self.root, self.debug),
            RequirementsScanner(self.root, self.debug),
            GoModScanner(self.root, self.debug),
        ]

        python_bias = 0
        for scanner in project_scanners:
            for c in scanner.scan():
                # Harvest Python bias from pyproject/requirements to apply later to .py
                if scanner.__class__ in {
                    PyProjectScanner,
                    RequirementsScanner,
                } and c.path.name in {"pyproject.toml", "requirements.txt"}:
                    python_bias = max(python_bias, c.score)
                else:
                    self._add_or_update_candidate(c)

        # 2) runner.json and Go cmd/*/main.go conventional hints
        if c := self._runner_json_hint():
            self._add_or_update_candidate(c)
        for c in self._go_cmd_main_hints():
            self._add_or_update_candidate(c)

        # 3) Per-file scanning (content/name) with caps
        content_scanner = FileContentScanner(self.root, self.debug)
        for path in self._walk_files():
            c = content_scanner.scan_file(path)
            if not c:
                continue
            if python_bias and path.suffix.lower() == ".py":
                c.add_score(python_bias, "pyproject/requirements_bias")
            self._add_or_update_candidate(c)

        ranked = sorted(self.results.values())
        return ranked[: self.top_n]


# ------------------------- CLI Entry Point --------------------------- #


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parses arguments and runs the server finder."""
    parser = argparse.ArgumentParser(
        description="Find potential MCP server entry points (Python, Node/TS, Go).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "root", nargs="?", default=".", help="Project root directory to scan."
    )
    parser.add_argument(
        "--top", type=int, default=10, help="Return the top N candidates."
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit results as a JSON array."
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print scoring reasons to stderr."
    )
    parser.add_argument(
        "--ext",
        type=str,
        default="",
        help="Comma-separated extra extensions (e.g., .sh,.rb).",
    )

    args = parser.parse_args(argv)

    try:
        root_path = Path(args.root).resolve(strict=True)
        extra_exts = {
            e.strip() for e in args.ext.split(",") if e.strip().startswith(".")
        }

        finder = ServerFinder(
            root=root_path, top_n=args.top, exts=extra_exts, debug=args.debug
        )
        results = finder.run()

        if args.json:
            payload = [
                {"path": c.rel_path.as_posix(), "score": c.score, "reasons": c.reasons}
                for c in results
            ]
            print(json.dumps(payload, indent=2))
        else:
            for c in results:
                print(f"- {c.rel_path.as_posix()}")

        if args.debug:
            print("\n--- Debug Info ---", file=sys.stderr)
            for c in results:
                print(
                    (
                        f"[debug] score={c.score:<4} path={c.rel_path.as_posix():<40} "
                        f"reasons={', '.join(c.reasons)}"
                    ),
                    file=sys.stderr,
                )

        return 0
    except FileNotFoundError:
        print(f"Error: Root directory not found at '{args.root}'", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
