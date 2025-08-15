# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import tarfile
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .client import MatrixClient
from .manifest import ManifestResolutionError
from .policy import default_install_target

# New modular fetchers (structured git + http archives/files)
try:
    from .gitfetch import fetch_git_artifact, GitFetchError
except Exception:  # pragma: no cover
    fetch_git_artifact = None  # type: ignore
    class GitFetchError(RuntimeError):  # type: ignore
        pass

try:
    from .archivefetch import fetch_http_artifact, ArchiveFetchError
except Exception:  # pragma: no cover
    fetch_http_artifact = None  # type: ignore
    class ArchiveFetchError(RuntimeError):  # type: ignore
        pass

# --- MODIFICATION: Import the new python_builder tool ---
try:
    from . import python_builder
except ImportError:
    python_builder = None # type: ignore
# --- END MODIFICATION ---

# --------------------------------------------------------------------------------------
# Logging (library-safe): use module logger; only attach a handler if MATRIX_SDK_DEBUG=1
# --------------------------------------------------------------------------------------
logger = logging.getLogger("matrix_sdk.installer")


def _maybe_configure_logging() -> None:
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    if dbg in ("1", "true", "yes", "on"):
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("[matrix-sdk][installer] %(levelname)s: %(message)s")
            )
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)


_maybe_configure_logging()


def _short(path: Path | str, maxlen: int = 120) -> str:
    s = str(path)
    return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])


def _log_tree(root: Path, depth: int = 2) -> None:
    """Lightweight directory tree logger for debugging."""
    try:
        root = Path(root)
        logger.debug("tree: %s", root)
        for p in sorted(root.glob("**/*")):
            rel = p.relative_to(root)
            if len(rel.parts) > depth:
                continue
            if p.is_dir():
                logger.debug("  [D] %s/", rel)
            else:
                logger.debug("  [F] %s (%d bytes)", rel, p.stat().st_size)
    except Exception as e:
        logger.debug("tree logging skipped: %s", e)


def _is_valid_runner_schema(runner: Dict[str, Any], logger: logging.Logger) -> bool:
    """Basic schema validation for a runner.json-like object."""
    if not isinstance(runner, dict):
        logger.debug("runner validation: failed (not a dict)")
        return False

    # Essential keys for any runner
    if not runner.get("type") or not runner.get("entry"):
        logger.warning(
            "runner validation: failed (missing required 'type' or 'entry' keys in %r)",
            list(runner.keys()),
        )
        return False
    logger.debug("runner validation: schema appears valid")
    return True


@dataclass(frozen=True)
class BuildReport:
    files_written: int = 0
    artifacts_fetched: int = 0
    runner_path: Optional[str] = None


@dataclass(frozen=True)
class EnvReport:
    python_prepared: bool = False
    node_prepared: bool = False
    notes: Optional[str] = None


@dataclass(frozen=True)
class BuildResult:
    id: str
    target: str
    plan: Dict[str, Any]
    build: BuildReport
    env: EnvReport
    runner: Dict[str, Any]


class LocalInstaller:
    """
    Orchestrates a local install:
      plan()        → call Hub '/catalog/install' (no side-effects)
      materialize() → write files/artifacts, ensure runner config
      prepare_env() → python venv / node install per runner config
      build()       → plan + materialize + prepare_env
    """

    def __init__(
        self, client: MatrixClient, *, fs_root: Optional[str | Path] = None
    ) -> None:
        self.client = client
        self.fs_root = Path(fs_root).expanduser() if fs_root else None
        logger.debug("LocalInstaller created (fs_root=%s)", self.fs_root)

    # ---- Public API ----------------------------------------------------------

    def plan(self, id: str, target: str | os.PathLike[str]) -> Dict[str, Any]:
        """
        Perform the Hub-side install planning and return a plain dict outcome
        regardless of whether the client returns a Pydantic model.
        """
        logger.info("plan: requesting Hub plan for id=%s target=%s", id, target)
        outcome = self.client.install(id, target=target)
        out = _as_dict(outcome)
        logger.debug(
            "plan: received keys=%s (results=%s, artifacts=%s, files=%s)",
            list(out.keys()),
            isinstance(out.get("results"), list) and len(out["results"]),
            isinstance(out.get("plan", {}).get("artifacts"), list)
            and len(out["plan"]["artifacts"]),
            isinstance(out.get("plan", {}).get("files"), list)
            and len(out["plan"]["files"]),
        )
        return out

    def _materialize_files(self, outcome: Dict[str, Any], target_path: Path) -> int:
        """Finds file descriptions in the outcome and writes them to the target directory."""
        logger.info("materialize: writing declared files → %s", target_path)
        files_written = 0
        candidates: list[dict] = []

        # Prefer top-level 'files' if present (rare)
        if isinstance(outcome.get("files_written"), list):
            logger.debug(
                "materialize: top-level files_written present (len=%d)",
                len(outcome["files_written"]),
            )

        # Look for embedded 'files' collections
        plan_node = outcome.get("plan") or {}
        if isinstance(plan_node, dict) and "files" in plan_node:
            v = plan_node.get("files")
            if isinstance(v, list):
                candidates.extend([x for x in v if isinstance(x, dict)])

        res_node = outcome.get("results") or []
        if isinstance(res_node, list):
            for step in res_node:
                if isinstance(step, dict):
                    v = step.get("files")
                    if isinstance(v, list):
                        candidates.extend([x for x in v if isinstance(x, dict)])

        if "files" in outcome:
            v = outcome.get("files")
            if isinstance(v, list):
                candidates.extend([x for x in v if isinstance(x, dict)])

        logger.debug("materialize: %d file candidate(s) found", len(candidates))

        # Write
        for f in candidates:
            path = f.get("path") or f.get("rel") or f.get("dest")
            if not path:
                logger.debug("materialize: skipping file without path/rel/dest: %r", f)
                continue

            p = (target_path / path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)

            if (content_b64 := f.get("content_b64")) is not None:
                logger.debug("materialize: writing (b64) %s", _short(p))
                p.write_bytes(base64.b64decode(content_b64))
            elif (content := f.get("content")) is not None:
                logger.debug("materialize: writing (text) %s", _short(p))
                p.write_text(content, encoding="utf-8")
            else:
                logger.debug("materialize: touching empty file %s", _short(p))
                p.touch()
            files_written += 1

        logger.info("materialize: files written = %d", files_written)
        return files_written

    def _materialize_artifacts(self, plan: Dict[str, Any], target_path: Path) -> int:
        """
        Downloads artifacts and writes/extracts them into the target directory.

        Supports:
        - kind == "git" (structured) via gitfetch.fetch_git_artifact
        - url-based file/archive via archivefetch.fetch_http_artifact
        """
        artifacts_fetched = 0
        artifacts = plan.get("artifacts") or plan.get("plan", {}).get("artifacts") or []
        if not artifacts:
            logger.info("materialize: no artifacts declared")
            return 0

        logger.info("materialize: fetching %d artifact(s)", len(artifacts))

        for a in artifacts or []:
            if not isinstance(a, dict):
                logger.debug("artifact: skipping non-dict entry: %r", a)
                continue

            # ========================================================================
            # WORKAROUND SHIM: Handle legacy git artifacts with a 'command' field
            # This block derives a modern 'spec' from an older 'command' string.
            # This should be removed once the Hub sends proper structured specs.
            # ========================================================================
            spec = a.get("spec") or {}
            if a.get("kind") == "git" and (not spec or not spec.get("repo")):
                cmd = (a.get("command") or "").strip()
                if cmd.startswith("git clone "):
                    parts = cmd.split()
                    try:
                        # Naive parse: git clone <repo> --branch <ref> ...
                        repo_idx = parts.index("clone") + 1
                        repo = parts[repo_idx]
                        ref = None
                        if "--branch" in parts:
                            ref_idx = parts.index("--branch") + 1
                            ref = parts[ref_idx]

                        if not (repo.startswith("http") or repo.startswith("git@")):
                            raise ValueError("Parsed token does not look like a repo URL")

                        new_spec = {"repo": repo, "ref": ref or "main"}
                        a["spec"] = new_spec  # Mutate artifact for the next step
                        logger.warning(
                            "artifact(git): SHIM: Derived spec from legacy 'command' field: %r",
                            new_spec,
                        )
                    except (ValueError, IndexError) as e:
                        logger.error(
                            "artifact(git): SHIM: Could not parse legacy 'command' (%s); need a valid 'spec'", e
                        )
            # ========================================================================
            # END WORKAROUND SHIM
            # ========================================================================


            # 1) Structured GIT artifact
            if a.get("kind") == "git":
                if fetch_git_artifact is None:
                    raise ManifestResolutionError("git artifact not supported (gitfetch module missing)")
                spec = a.get("spec") or {}
                logger.info("artifact(git): spec=%r", spec)
                try:
                    fetch_git_artifact(
                        spec=spec,  # type: ignore[arg-type]
                        target=target_path,
                        git_bin=os.getenv("MATRIX_GIT_BIN", "git"),
                        allow_hosts=None,  # fallback to module defaults/env
                        timeout=int(os.getenv("MATRIX_GIT_TIMEOUT", "180")),
                        logger=None,  # git module has its own logger
                    )
                    artifacts_fetched += 1
                except GitFetchError as e:
                    logger.error("artifact(git): %s", e)
                    raise ManifestResolutionError(str(e)) from e
                continue

            # 2) URL-based file/archive
            url = a.get("url")
            if url:
                if fetch_http_artifact is None:
                    logger.debug("artifact(http): helper missing; using inline fetch for %s", url)
                    artifacts_fetched += self._fetch_http_minimal(a, target_path)
                    continue

                dest = a.get("path") or a.get("dest")
                sha256 = a.get("sha256")
                unpack_flag = bool(a.get("unpack", False))
                logger.info("artifact(http): url=%s dest=%s unpack=%s", url, dest or "-", unpack_flag)
                try:
                    fetch_http_artifact(
                        url=url,
                        target=target_path,
                        dest=dest,
                        sha256=str(sha256) if sha256 is not None else None,
                        unpack=unpack_flag,
                        timeout=int(os.getenv("MATRIX_HTTP_FETCH_TIMEOUT", "60")),
                        logger=logger,
                    )
                    artifacts_fetched += 1
                except ArchiveFetchError as e:
                    logger.error("artifact(http): %s", e)
                    raise ManifestResolutionError(str(e)) from e
                continue

            # Unknown artifact type
            logger.warning("artifact: skipping unknown entry (neither kind=git nor url): %r", a)

        logger.info("materialize: artifacts fetched = %d", artifacts_fetched)
        return artifacts_fetched

    # ---- Minimal inline HTTP fetch fallback (if archivefetch not available) ----
    def _fetch_http_minimal(self, a: Dict[str, Any], target_path: Path) -> int:
        url = a.get("url")
        dest = a.get("path") or a.get("dest")
        sha256 = a.get("sha256")
        unpack_flag = bool(a.get("unpack", False))
        if not url:
            return 0

        with httpx.Client(timeout=60.0, follow_redirects=True) as http:
            logger.info("artifact: GET %s", url)
            resp = http.get(url)
            resp.raise_for_status()
            data = resp.content
            size = len(data)
            logger.debug("artifact: downloaded %d bytes from %s", size, url)

            # Integrity check
            if sha256:
                digest = hashlib.sha256(data).hexdigest().lower()
                if digest != str(sha256).lower():
                    logger.error(
                        "artifact: sha256 mismatch url=%s expected=%s got=%s",
                        url,
                        sha256,
                        digest,
                    )
                    raise ManifestResolutionError(f"Checksum mismatch for artifact: {url}")
                logger.debug("artifact: sha256 verified OK")

            # Persist raw artifact if a 'dest' path was requested
            if dest:
                p = (target_path / dest).resolve()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                logger.debug("artifact: wrote raw → %s", _short(p))

            # Decide whether to unpack
            is_zip = (dest or "").lower().endswith(".zip") or url.lower().endswith(".zip")
            is_targz = any((dest or url).lower().endswith(suf) for suf in (".tar.gz", ".tgz", ".tar"))
            should_unpack = unpack_flag or is_zip or is_targz
            logger.debug(
                "artifact: unpack? %s (zip=%s, tar=%s, flag=%s)",
                should_unpack,
                is_zip,
                is_targz,
                unpack_flag,
            )

            if should_unpack:
                logger.info("artifact: unpacking into %s", _short(target_path))
                bio = io.BytesIO(data)
                if is_zip:
                    with zipfile.ZipFile(bio) as zf:
                        zf.extractall(path=target_path)
                        logger.debug("artifact: zip extracted %d members", len(zf.namelist()))
                elif is_targz:
                    mode = "r:gz" if (dest or url).lower().endswith((".tar.gz", ".tgz")) else "r:"
                    with tarfile.open(fileobj=bio, mode=mode) as tf:
                        tf.extractall(path=target_path)
                        logger.debug("artifact: tar extracted members")
                _maybe_flatten_extracted_tree(target_path)
                _log_tree(target_path, depth=2)

        return 1

    def _materialize_runner(
        self, outcome: Dict[str, Any], target_path: Path
    ) -> Optional[str]:
        """Ensures a runner config file exists, validating its schema."""
        plan_node = outcome.get("plan") or {}

        # --- Candidate discovery ---
        # 1) Use runner object from outcome['plan'] if provided and valid
        if isinstance(plan_node, dict) and (runner_obj := plan_node.get("runner")):
            try:
                # Accept both dict and model-like objects
                if hasattr(runner_obj, "model_dump"): runner_obj = runner_obj.model_dump()
                elif hasattr(runner_obj, "dict"): runner_obj = runner_obj.dict()
            except Exception: pass
            
            if isinstance(runner_obj, dict) and _is_valid_runner_schema(runner_obj, logger):
                runner_path = target_path / "runner.json"
                runner_path.write_text(json.dumps(runner_obj, indent=2), encoding="utf-8")
                logger.info("runner: wrote runner.json from valid plan object -> %s", _short(runner_path))
                return str(runner_path)
            else:
                logger.warning("runner: object in 'plan.runner' was found but failed schema validation.")

        # 2) Check for a specific runner file path in the plan (e.g., 'runner_file')
        runner_file_name = "runner.json"  # Default
        if isinstance(plan_node, dict) and (custom_file := plan_node.get("runner_file")):
            runner_file_name = str(custom_file)
            logger.info("runner: plan specifies custom runner file: '%s'", runner_file_name)

        runner_path = target_path / runner_file_name
        if runner_path.exists():
            try:
                runner_data = json.loads(runner_path.read_text(encoding="utf-8"))
                if _is_valid_runner_schema(runner_data, logger):
                    logger.info("runner: found existing and valid runner file -> %s", _short(runner_path))
                    # If a custom name was used, create the standard runner.json for consistency
                    if runner_file_name != "runner.json":
                        (target_path / "runner.json").write_text(runner_path.read_text(encoding="utf-8"))
                        logger.debug("runner: created standard 'runner.json' from custom file")
                    return str(runner_path)
                else:
                    logger.warning("runner: file at %s exists but failed schema validation.", _short(runner_path))
            except json.JSONDecodeError:
                logger.warning("runner: file at %s exists but is not valid JSON.", _short(runner_path))
        
        # 3) If not found, infer a runner from the project structure and validate it
        inferred = self._infer_runner(target_path)
        if inferred and _is_valid_runner_schema(inferred, logger):
            final_runner_path = target_path / "runner.json"
            final_runner_path.write_text(json.dumps(inferred, indent=2), encoding="utf-8")
            logger.info("runner: inferred a valid runner.json -> %s", _short(final_runner_path))
            return str(final_runner_path)
        
        logger.warning("runner: a valid runner config was not found and could not be inferred")
        return None


    def materialize(
        self, outcome: Dict[str, Any], target: str | os.PathLike[str]
    ) -> BuildReport:
        """Write files, fetch artifacts, and produce runner.json if needed."""
        target_path = self._abs(target)
        target_path.mkdir(parents=True, exist_ok=True)
        logger.info("materialize: target directory ready → %s", _short(target_path))

        files_written = self._materialize_files(outcome, target_path)
        # Prefer "plan" section if present
        plan_node = outcome.get("plan", outcome)
        artifacts_fetched = self._materialize_artifacts(plan_node, target_path)
        runner_path = self._materialize_runner(outcome, target_path)

        br = BuildReport(
            files_written=files_written,
            artifacts_fetched=artifacts_fetched,
            runner_path=runner_path,
        )
        logger.info(
            "materialize: summary files=%d artifacts=%d runner=%s",
            br.files_written,
            br.artifacts_fetched,
            br.runner_path or "-",
        )
        return br

    # --- MODIFICATION: Replace the entire prepare_env method ---
    def prepare_env(
        self, target: str | os.PathLike[str], runner: Dict[str, Any], *, timeout: int = 900
    ) -> EnvReport:
        """Create a python venv and/or run node install as requested by runner."""
        t = self._abs(target)
        python_prepared = False
        node_prepared = False
        notes: list[str] = []

        typ = (runner.get("type") or "").lower()
        logger.info("env: preparing environment (type=%s) in %s", typ or "-", _short(t))

        if typ == "python":
            rp = runner.get("python") or {}
            venv_dir = rp.get("venv") or ".venv"
            venv_path = t / venv_dir
            if not venv_path.exists():
                logger.info("env: creating venv → %s", _short(venv_path))
                venv.create(
                    venv_path,
                    with_pip=True,
                    clear=False,
                    symlinks=True,
                    upgrade_deps=False,
                )
            pybin = _python_bin(venv_path)

            # =========================================================================
            # NEW: Use the dedicated python_builder tool to handle dependency installation
            # =========================================================================
            if python_builder:
                logger.info("env: using modern python_builder to install dependencies...")
                installed_ok = python_builder.run_python_build(
                    target_path=t,
                    python_executable=pybin,
                    logger=logger,
                    timeout=timeout,
                )
                if not installed_ok:
                    logger.warning(
                        "env: python_builder found no installable files (pyproject.toml, requirements.txt, or Makefile)."
                    )
            else:
                # Fallback to legacy logic if python_builder.py is missing
                logger.warning("env: python_builder not found, falling back to legacy requirements.txt check.")
                reqs = rp.get("requirements")
                if isinstance(reqs, list) and reqs:
                    cmd = [pybin, "-m", "pip", "install"] + list(reqs)
                    _run(cmd, cwd=t, timeout=timeout)
                elif (t / "requirements.txt").exists():
                    cmd = [pybin, "-m", "pip", "install", "-r", "requirements.txt"]
                    _run(cmd, cwd=t, timeout=timeout)
                else:
                    # Default health-check if no requirements found
                    cmd = [pybin, "-m", "pip", "--version"]
                    _run(cmd, cwd=t, timeout=timeout)
            # =========================================================================
            # END NEW LOGIC
            # =========================================================================

            python_prepared = True

        if typ == "node" or (typ == "python" and runner.get("node")):
            np = runner.get("node") or {}
            pm = np.get("package_manager") or _detect_package_manager(t)
            install_args = np.get("install_args") or []
            if pm:
                cmd = [pm, "install"] + list(install_args)
                logger.info("env: %s install in %s", pm, _short(t))
                _run(cmd, cwd=t, timeout=timeout)
                node_prepared = True
            else:
                note = "node requested but no package manager detected"
                logger.warning("env: %s", note)
                notes.append(note)

        er = EnvReport(
            python_prepared=python_prepared,
            node_prepared=node_prepared,
            notes="; ".join(notes) or None,
        )
        logger.info(
            "env: summary python=%s node=%s notes=%s",
            er.python_prepared,
            er.node_prepared,
            er.notes or "-",
        )
        return er
    # --- END MODIFICATION ---

    def build(
        self,
        id: str,
        *,
        target: Optional[str | os.PathLike[str]] = None,
        alias: Optional[str] = None,
        timeout: int = 900,
    ) -> BuildResult:
        tgt = self._abs(target or default_install_target(id, alias=alias))
        tgt.mkdir(parents=True, exist_ok=True)
        logger.info("build: target resolved → %s", _short(tgt))

        outcome = self.plan(id, tgt)  # normalized to dict
        br = self.materialize(outcome, tgt)

        # Load runner for prepare step
        runner: Dict[str, Any] = {}
        runner_path_str = br.runner_path
        
        # If materialize gave us a custom path, it might not be runner.json
        if runner_path_str and Path(runner_path_str).exists():
             runner_file_to_load = Path(runner_path_str)
        else:
             runner_file_to_load = tgt / "runner.json"
        
        if runner_file_to_load.exists():
            try:
                runner = json.loads(runner_file_to_load.read_text(encoding="utf-8"))
                logger.debug("build: loaded runner from %s", _short(runner_file_to_load))
            except json.JSONDecodeError:
                 logger.error("build: failed to decode runner JSON from %s", _short(runner_file_to_load))
                 runner = {}
        else:
            logger.warning("build: runner.json not found; env prepare may be skipped")
            runner = {}

        er = self.prepare_env(tgt, runner, timeout=timeout)
        result = BuildResult(
            id=id, target=str(tgt), plan=outcome, build=br, env=er, runner=runner
        )
        logger.info(
            "build: complete id=%s target=%s files=%d artifacts=%d python=%s node=%s",
            id,
            _short(tgt),
            br.files_written,
            br.artifacts_fetched,
            er.python_prepared,
            er.node_prepared,
        )
        return result

    # ---- Internals -----------------------------------------------------------

    def _abs(self, path: str | os.PathLike[str]) -> Path:
        p = Path(path)
        if not p.is_absolute() and self.fs_root:
            p = self.fs_root / p
        rp = p.expanduser().resolve()
        logger.debug("path: resolved %s → %s", path, rp)
        return rp

    def _infer_runner(self, target: Path) -> Dict[str, Any] | None:
        if (target / "server.py").exists():
            logger.debug("runner: inferring python runner (server.py present)")
            return {
                "type": "python",
                "entry": "server.py",
                "transport": "sse",
                "python": {"venv": ".venv"},
                "env": {},
            }
        if (target / "server.js").exists() or (target / "package.json").exists():
            logger.debug("runner: inferring node runner (server.js/package.json present)")
            return {
                "type": "node",
                "entry": "server.js" if (target / "server.js").exists() else "index.js",
                "transport": "sse",
                "node": {},
                "env": {},
            }
        logger.debug("runner: could not infer (no server.py/js or package.json)")
        return None


def _python_bin(venv_path: Path) -> str:
    if os.name == "nt":
        return str(venv_path / "Scripts" / "python.exe")
    return str(venv_path / "bin" / "python")


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> None:
    logger.debug("exec: %s (cwd=%s, timeout=%ss)", " ".join(map(str, cmd)), _short(cwd), timeout)
    subprocess.run(cmd, cwd=str(cwd), check=True, timeout=timeout)


def _detect_package_manager(path: Path) -> Optional[str]:
    if (path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (path / "yarn.lock").exists():
        return "yarn"
    if (path / "package-lock.json").exists() or (path / "package.json").exists():
        return "npm"
    return None


def _as_dict(obj: Any) -> Dict[str, Any]:
    """
    Normalize possible Pydantic models or dataclasses to a plain dict.
    """
    if obj is None:
        return {}
    # Pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()  # type: ignore[no-any-return]
        except Exception:
            logger.debug("as_dict: model_dump failed; falling back")
    # Pydantic v1
    if hasattr(obj, "dict"):
        try:
            return obj.dict()  # type: ignore[no-any-return]
        except Exception:
            logger.debug("as_dict: dict() failed; falling back")
    # Dataclasses
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(obj):
            return asdict(obj)  # type: ignore[no-any-return]
    except Exception:
        logger.debug("as_dict: dataclass conversion failed; falling back")
    # Already a mapping?
    if isinstance(obj, dict):
        return obj
    # Last resort: JSON round-trip for simple objects
    try:
        return json.loads(json.dumps(obj))
    except Exception:
        logger.debug("as_dict: JSON round-trip failed; returning wrapper")
        return {"value": obj}


# --------------------------------------------------------------------------------------
# Archive layout helper: flatten single top-level directory (common GitHub ZIP pattern)
# --------------------------------------------------------------------------------------
def _maybe_flatten_extracted_tree(target_path: Path) -> None:
    """
    If the target contains exactly one subdirectory and nothing else (excluding
    the raw artifact file we might have saved), move that subdirectory's
    contents up to the target root.

    This handles the common GitHub ZIP pattern: <name>-<version>/...
    """
    try:
        entries = [p for p in target_path.iterdir() if p.is_dir()]
        if len(entries) != 1:
            logger.debug(
                "flatten: skip (dirs at root: %d) → %s",
                len(entries),
                _short(target_path),
            )
            return
        sub = entries[0]
        logger.info("flatten: moving %s/* up into %s", sub.name, _short(target_path))
        for child in sub.iterdir():
            dest = target_path / child.name
            if dest.exists():
                logger.debug("flatten: skipping existing %s", _short(dest))
                continue
            shutil.move(str(child), str(dest))
        try:
            sub.rmdir()
        except Exception:
            logger.debug("flatten: could not remove now-empty %s (ignored)", sub)
    except Exception as e:
        logger.debug("flatten: skipped due to error: %s", e)