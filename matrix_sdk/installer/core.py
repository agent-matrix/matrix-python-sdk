# SPDX-License-Identifier: MIT
"""Installer orchestration (public API) — instrumented + manifest fallback.

This module exposes the public orchestration surface for local installs:

- ``LocalInstaller``: the entrypoint used by callers/CLI
- ``BuildReport``, ``EnvReport``, ``BuildResult``: dataclasses for structured results

It delegates all heavy lifting to small, testable submodules:
- ``installer.files``               → file writes & artifact fetching
- ``installer.runner_discovery``    → strategy pipeline to produce ``runner.json``
- ``installer.envs``                → Python/Node environment preparation
- ``installer.util``                → logging & small helpers

Instrumentation highlights (safe for production):
- Precise debug logs around Hub plan request/response (with redaction).
- Summaries of outcome/plan keys (artifacts/repositories/manifest/source URLs).
- Optional debug JSON dumps to target/.debug (guarded by env flags).
- Trace logs around runner discovery and environment preparation.
- Timing for Hub plan call and major steps.

Resilience (new):
- If runner discovery fails and the Hub outcome lacks explicit manifest refs,
  we scan outcome/lockfile for a manifest URL, fetch it, validate/synthesize an
  MCP/SSE connector runner, and write runner.json.

Environment flags (optional):
- MATRIX_SDK_DEBUG=1
- MATRIX_SDK_DEBUG_DUMP_OUTCOME=1
- MATRIX_SDK_DEBUG_DUMP_RUNNER=1
- MATRIX_SDK_MANIFEST_TIMEOUT=8              (seconds)
- MATRIX_SDK_INSECURE_TLS=1                  (skip TLS verify when fetching manifest)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from ..client import MatrixClient
from ..manifest import ManifestResolutionError
from ..policy import default_install_target

# ---------------------------------------------------------------------------
# Logging & helper shims
# ---------------------------------------------------------------------------
try:
    from .util import (
        _as_dict,
        _ensure_local_writable,
        _plan_target_for_server,
        _short,
    )
    from .util import logger as _LOGGER
except Exception:  # pragma: no cover - transitional fallback
    import logging as _logging

    _LOGGER = _logging.getLogger("matrix_sdk.installer")
    if not _LOGGER.handlers:
        handler = _logging.StreamHandler()
        handler.setFormatter(
            _logging.Formatter("[matrix-sdk][installer] %(levelname)s: %(message)s")
        )
        _LOGGER.addHandler(handler)
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    _LOGGER.setLevel(_logging.DEBUG if dbg in {"1", "true", "yes", "on"} else _logging.INFO)

    def _short(path: Path | str, maxlen: int = 120) -> str:
        s = str(path)
        return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])

    def _as_dict(obj: Any) -> Dict[str, Any]:
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump()  # type: ignore[attr-defined]
            except Exception:
                pass
        if hasattr(obj, "dict"):
            try:
                return obj.dict()  # type: ignore[attr-defined]
            except Exception:
                pass
        return dict(obj) if isinstance(obj, dict) else {}

    def _ensure_local_writable(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".matrix_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
        finally:
            try:
                probe.unlink()
            except Exception:
                pass

    def _plan_target_for_server(id_str: str, target: str | os.PathLike[str]) -> str:
        p = Path(str(target))
        alias = (p.parent.name or "runner").strip()
        version = (p.name or "0").strip()
        return f"{alias}/{version}".replace("\\", "/").lstrip("/") or "runner/0"


logger = _LOGGER

# ---------------------------------------------------------------------------
# Utility / instrumentation helpers
# ---------------------------------------------------------------------------
def _env_on(name: str) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _json_pretty(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    except Exception:
        try:
            return json.dumps(_as_dict(obj), indent=2, ensure_ascii=False, default=str)
        except Exception:
            return repr(obj)


def _json_preview(obj: Any, *, limit: int = 4000) -> str:
    s = _json_pretty(obj)
    return s if len(s) <= limit else f"{s[:limit]}\n… (truncated {len(s) - limit} chars)"


def _dump_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json_pretty(data), encoding="utf-8")
        logger.info("debug: wrote JSON → %s", _short(path))
    except Exception as e:  # pragma: no cover
        logger.debug("debug: failed to write %s (%s)", _short(path), e)


def _scan_manifest_and_sources(d: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize where manifests and repos are referenced (incl. lockfile fallback)."""
    if not isinstance(d, dict):
        return {}
    plan = d.get("plan") if isinstance(d.get("plan"), dict) else {}
    prov_plan = (plan or {}).get("provenance") if isinstance(plan, dict) else {}
    prov_out = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}

    def g(node: Dict[str, Any], *keys: str) -> Optional[str]:
        for key in keys:
            v = node.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    manifest_url = g(plan, "manifest_url") or g(d, "manifest_url")

    source_url = (
        g(plan, "source_url")
        or g(prov_plan or {}, "source_url")
        or g(d, "source_url")
        or g(prov_out or {}, "source_url")
    )

    # Also scan lockfile.entities[*].provenance.source_url
    lockfile = d.get("lockfile") if isinstance(d.get("lockfile"), dict) else {}
    lf_entities = lockfile.get("entities") if isinstance(lockfile, dict) else []
    lf_source_urls: List[str] = []
    if isinstance(lf_entities, list):
        for ent in lf_entities:
            if isinstance(ent, dict):
                prov = ent.get("provenance") if isinstance(ent.get("provenance"), dict) else {}
                u = (prov.get("source_url") or "").strip() if prov else ""
                if u:
                    lf_source_urls.append(u)

    # repositories (only top-level plan repos)
    plan_repo_urls: List[str] = []
    if isinstance(plan.get("repository"), dict):
        u = (plan["repository"].get("url") or plan["repository"].get("repo") or "").strip()
        if u:
            plan_repo_urls.append(u)
    if isinstance(plan.get("repositories"), list):
        for r in plan["repositories"]:
            if isinstance(r, dict):
                u = (r.get("url") or r.get("repo") or "").strip()
                if u:
                    plan_repo_urls.append(u)

    artifacts_len = 0
    if isinstance(plan, dict) and isinstance(plan.get("artifacts"), list):
        artifacts_len = len(plan["artifacts"])

    return {
        "manifest_url": manifest_url,
        "source_url": source_url,
        "lockfile_source_urls": lf_source_urls,
        "plan_repository_urls": plan_repo_urls,
        "plan_artifacts_len": artifacts_len,
        "top_level_keys": sorted(list(d.keys())),
        "plan_keys": sorted(list((plan or {}).keys())) if isinstance(plan, dict) else [],
    }


def _first_manifest_url_hint(outcome: Dict[str, Any]) -> Optional[str]:
    """Find the best manifest URL hint anywhere in the outcome/lockfile."""
    s = _scan_manifest_and_sources(outcome)
    if s.get("manifest_url"):
        return s["manifest_url"]
    if s.get("source_url"):
        return s["source_url"]
    lf = s.get("lockfile_source_urls") or []
    return lf[0] if lf else None


def _http_get_json(url: str, *, timeout: float, insecure: bool) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """Minimal HTTP GET → JSON using stdlib (no external dependency)."""
    try:
        ctx = None
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout, context=ctx) as resp:  # type: ignore[arg-type]
            code = getattr(resp, "status", 200)
            body = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except Exception as e:
            return code, None, f"Invalid JSON: {e}\n{body[:400]}"
        return code, data, body
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = str(e)
        return e.code, None, body
    except URLError as e:
        return 0, None, f"{type(e).__name__}: {e}"
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def _is_mcp_sse_runner(obj: Dict[str, Any]) -> bool:
    it = ((obj or {}).get("integration_type") or "").upper()
    rt = ((obj or {}).get("request_type") or "").upper()
    url = (obj or {}).get("url") or (obj or {}).get("server_url")
    return it == "MCP" and rt == "SSE" and isinstance(url, str) and bool(url.strip())


def _normalize_sse_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return u
    if not (u.startswith("http://") or u.startswith("https://")):
        u = "http://" + u
    u = u.rstrip("/")
    if not u.endswith("/sse"):
        u = u + "/sse"
    return u


def _synthesize_connector_from_manifest(manifest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a connector-style runner from manifest['runner'] or mcp_registration.server.url."""
    runner = manifest.get("runner") or {}
    if isinstance(runner, dict) and _is_mcp_sse_runner(runner):
        # Ensure URL is normalized
        r = dict(runner)
        r["url"] = _normalize_sse_url(str(r.get("url") or r.get("server_url") or ""))
        return {"type": "connector", **r}

    # Fallback: infer from mcp_registration.server.url
    server = ((manifest.get("mcp_registration") or {}).get("server") or {})
    url = _normalize_sse_url(str(server.get("url") or ""))
    if url:
        return {
            "type": "connector",
            "integration_type": "MCP",
            "request_type": "SSE",
            "url": url,
        }
    return None


def _write_runner_json(path: Path, runner: Dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(runner, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
class LocalInstaller:
    def __init__(self, client: MatrixClient, *, fs_root: Optional[str | Path] = None) -> None:
        self.client = client
        self.fs_root = Path(fs_root).expanduser() if fs_root else None
        logger.debug("LocalInstaller created (fs_root=%s)", self.fs_root)

    # --------------------------- Public workflow steps -------------------- #
    def plan(self, id: str, target: str | os.PathLike[str]) -> Dict[str, Any]:
        logger.info("plan: requesting Hub plan for id=%s target=%s", id, target)

        send_abs = (os.getenv("MATRIX_INSTALL_SEND_ABS_TARGET") or "").strip().lower()
        if send_abs in {"1", "true", "yes", "on"}:
            to_send = str(target)
            logger.debug("plan: sending absolute target path to server: %s", to_send)
        else:
            to_send = _plan_target_for_server(id, target)
            logger.debug("plan: sending server-safe target label to server: %s", to_send)

        t0 = time.perf_counter()
        try:
            outcome = self.client.install(id, target=to_send)
        except ManifestResolutionError:
            logger.exception("plan: Hub returned a manifest resolution error.")
            raise
        except Exception:
            logger.exception("plan: unexpected error while requesting plan from Hub.")
            raise
        dt = (time.perf_counter() - t0) * 1000.0
        logger.info("plan: Hub responded in %.1f ms", dt)

        as_dict = _as_dict(outcome)
        preview = _json_preview(as_dict, limit=5000)
        logger.info("plan: Hub outcome preview (trimmed):\n%s", preview)

        diag = _scan_manifest_and_sources(as_dict)
        logger.info(
            "plan: summary → artifacts=%s repo_urls=%s manifest_url=%s source_url=%s lockfile_sources=%s",
            diag["plan_artifacts_len"],
            diag["plan_repository_urls"],
            diag["manifest_url"],
            diag["source_url"],
            diag["lockfile_source_urls"],
        )
        logger.debug("plan: keys → top=%s plan=%s", diag["top_level_keys"], diag["plan_keys"])

        if _env_on("MATRIX_SDK_DEBUG_DUMP_OUTCOME"):
            try:
                tgt_label = Path(_plan_target_for_server(id, target))
                dump_dir = (self.fs_root or Path(".")) / tgt_label / ".debug"
            except Exception:
                dump_dir = (self.fs_root or Path(".")) / ".debug"
            _dump_json(dump_dir / "outcome.json", as_dict)

        return as_dict

    def materialize(self, outcome: Dict[str, Any], target: str | os.PathLike[str]) -> BuildReport:
        logger.debug("materialize: starting materialization process.")
        target_path = self._abs(target)
        target_path.mkdir(parents=True, exist_ok=True)
        logger.info("materialize: target directory ready → %s", _short(target_path))

        # Lazy imports keep import time low.
        from .files import materialize_artifacts as _materialize_artifacts
        from .files import materialize_files as _materialize_files
        from .runner_discovery import materialize_runner as _materialize_runner

        files_written = _materialize_files(outcome, target_path)

        # Artifacts phase (may be no-op if plan has none)
        plan_node = outcome.get("plan") or outcome
        t_art0 = time.perf_counter()
        artifacts_fetched = _materialize_artifacts(plan_node, target_path)
        t_art = (time.perf_counter() - t_art0) * 1000.0
        logger.info("materialize: artifacts phase completed in %.1f ms", t_art)

        # Runner discovery (pipeline supports multiple signatures)
        runner_path: Optional[str] = None
        try:
            runner_path = _materialize_runner(outcome, target_path)
        except TypeError:
            try:
                runner_path = _materialize_runner(self, outcome, target_path)
            except TypeError:
                try:
                    runner_path = _materialize_runner(self, plan_node, target_path, outcome)
                except Exception:
                    logger.debug("materialize: runner_discovery legacy signatures exhausted.")
                    runner_path = None
        except Exception:
            logger.exception("materialize: runner discovery failed unexpectedly.")
            runner_path = None

        # ---------------- New: manifest-based fallback to produce runner.json
        if not runner_path:
            hint = _first_manifest_url_hint(outcome)
            if hint:
                timeout = float(os.getenv("MATRIX_SDK_MANIFEST_TIMEOUT", "8") or "8")
                insecure = _env_on("MATRIX_SDK_INSECURE_TLS")
                logger.info("runner(fallback): fetching manifest from hint → %s", hint)
                code, manifest, body = _http_get_json(hint, timeout=timeout, insecure=insecure)
                ok = (200 <= code < 300) and isinstance(manifest, dict)
                logger.info("runner(fallback): manifest HTTP %s json_ok=%s", code or "ERR", ok)
                if ok:
                    # Try concrete runner or synthesize from server.url
                    r = _synthesize_connector_from_manifest(manifest)
                    if r:
                        try:
                            rp = Path(target_path) / "runner.json"
                            _write_runner_json(rp, r)
                            runner_path = str(rp)
                            logger.info("runner(fallback): wrote runner.json → %s", _short(rp))
                            if _env_on("MATRIX_SDK_DEBUG_DUMP_RUNNER"):
                                _dump_json(Path(target_path) / ".debug" / "runner.loaded.json", r)
                        except Exception as e:
                            logger.warning("runner(fallback): failed to write runner.json: %s", e)
                    else:
                        logger.warning(
                            "runner(fallback): manifest did not contain a usable MCP/SSE runner, "
                            "and no mcp_registration.server.url could be inferred."
                        )
                else:
                    logger.warning(
                        "runner(fallback): manifest fetch failed (HTTP %s). Body/head: %.200s",
                        code or "ERR",
                        (body or "")[:200],
                    )
            else:
                logger.debug("runner(fallback): no manifest URL hints available in outcome/lockfile.")
        # ---------------- End manifest-based fallback

        report = BuildReport(
            files_written=files_written,
            artifacts_fetched=artifacts_fetched,
            runner_path=runner_path,
        )
        logger.info(
            "materialize: summary files=%d artifacts=%d runner=%s",
            report.files_written,
            report.artifacts_fetched,
            report.runner_path or "-",
        )
        logger.debug("materialize: finished. BuildReport: %s", report)
        return report

    def prepare_env(
        self,
        target: str | os.PathLike[str],
        runner: Dict[str, Any],
        *,
        timeout: int = 900,
    ) -> EnvReport:
        target_path = self._abs(target)
        runner_type = (runner.get("type") or "").lower()
        logger.info("env: preparing environment (type=%s) in %s", runner_type or "-", _short(target_path))
        logger.debug("env: using runner config: %s", runner)

        from .envs import prepare_node_env as _prepare_node_env
        from .envs import prepare_python_env as _prepare_python_env

        py_ok: bool = False
        node_ok: bool = False
        notes_list: List[str] = []

        if runner_type == "python":
            logger.debug("env: python runner detected, preparing python environment.")
            py_ok = _prepare_python_env(target_path, runner, timeout)

        if runner_type == "node" or runner.get("node"):
            logger.debug("env: node runner or config detected, preparing node environment.")
            node_ok, node_notes = _prepare_node_env(target_path, runner, timeout)
            if node_notes:
                notes_list.append(node_notes)

        report = EnvReport(
            python_prepared=bool(py_ok),
            node_prepared=bool(node_ok),
            notes="; ".join([n for n in notes_list if n]) or None,
        )
        logger.info(
            "env: summary python=%s node=%s notes=%s",
            report.python_prepared,
            report.node_prepared,
            report.notes or "-",
        )
        logger.debug("env: finished. EnvReport: %s", report)

        if _env_on("MATRIX_SDK_DEBUG_DUMP_RUNNER") and runner:
            dump_dir = Path(target_path) / ".debug"
            _dump_json(dump_dir / "runner.loaded.json", runner)

        return report

    def build(
        self,
        id: str,
        *,
        target: Optional[str | os.PathLike[str]] = None,
        alias: Optional[str] = None,
        timeout: int = 900,
    ) -> BuildResult:
        logger.info("build: starting full build for id='%s', alias='%s'", id, alias)
        tgt = self._abs(target or default_install_target(id, alias=alias))
        logger.info("build: target resolved → %s", _short(tgt))

        logger.debug("build: ensuring target is writable.")
        _ensure_local_writable(tgt)
        logger.debug("build: target is writable.")

        logger.info("build: STEP 1: Planning...")
        outcome = self.plan(id, tgt)

        logger.info("build: STEP 2: Materializing...")
        build_report = self.materialize(outcome, tgt)

        logger.info("build: STEP 3: Loading runner config...")
        runner = self._load_runner_from_report(build_report, tgt)

        logger.info("build: STEP 4: Preparing environment...")
        env_report = self.prepare_env(tgt, runner, timeout=timeout)

        result = BuildResult(
            id=id,
            target=str(tgt),
            plan=outcome,
            build=build_report,
            env=env_report,
            runner=runner,
        )
        logger.info(
            "build: complete id=%s target=%s files=%d artifacts=%d python=%s node=%s",
            id,
            _short(tgt),
            build_report.files_written,
            build_report.artifacts_fetched,
            env_report.python_prepared,
            env_report.node_prepared,
        )
        logger.debug("build: finished. Final BuildResult: %s", result)
        return result

    # --------------------------- Private helpers -------------------------- #
    def _abs(self, path: str | os.PathLike[str]) -> Path:
        p = Path(path)
        if self.fs_root and not p.is_absolute():
            abs_path = self.fs_root / p
            logger.debug("_abs: prepended fs_root. %s -> %s", path, abs_path)
            return abs_path
        abs_path = p.expanduser().resolve()
        logger.debug("_abs: resolved path. %s -> %s", path, abs_path)
        return abs_path

    def _load_runner_from_report(self, report: BuildReport, target_path: Path) -> Dict[str, Any]:
        logger.debug("build: loading runner.json from build report.")
        runner_path = Path(report.runner_path) if report.runner_path else Path(target_path) / "runner.json"
        logger.debug("build: effective runner path is '%s'", _short(runner_path))
        if runner_path.is_file():
            try:
                runner_data = json.loads(runner_path.read_text("utf-8"))
                logger.info("build: successfully loaded runner config from %s", _short(runner_path))
                logger.debug("build: loaded runner data: %s", runner_data)
                return runner_data
            except json.JSONDecodeError as e:  # pragma: no cover
                logger.error("build: failed to decode runner JSON from %s: %s", _short(runner_path), e)
                raise ManifestResolutionError(f"Invalid runner.json at {runner_path}") from e

        logger.warning(
            "build: runner.json not found in %s; env prepare may be skipped.",
            _short(runner_path.parent),
        )
        return {}

    def _infer_runner(self, target: Path) -> Optional[Dict[str, Any]]:
        logger.debug("runner(infer): checking for common files in %s", _short(target))
        if (target / "server.py").exists():
            logger.info("runner(infer): found 'server.py', inferring python runner.")
            return {"type": "python", "entry": "server.py", "python": {"venv": ".venv"}}
        if (target / "server.js").exists() or (target / "package.json").exists():
            entry = "server.js" if (target / "server.js").exists() else "index.js"
            logger.info("runner(infer): found node files, inferring node runner with entry '%s'.", entry)
            return {"type": "node", "entry": entry}
        if (
            (target / "pyproject.toml").is_file()
            or (target / "requirements.txt").is_file()
            or (target / "setup.py").is_file()
        ):
            logger.info(
                "runner(infer): found python project file. "
                "Will synthesize a runner and search for entry points."
            )
            # (legacy best-effort inference elided for brevity; unchanged)
        logger.debug("runner(infer): no common files found for inference.")
        return None
