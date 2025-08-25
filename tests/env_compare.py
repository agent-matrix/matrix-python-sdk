#!/usr/bin/env python3
# tests/env_compare.py — truststore-aware, CI/test-safe refactor
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_URL = "https://api.matrixhub.io/"

INSPECT_SNIPPET = (
    r'''
import json, sys, ssl, platform, os, hashlib
from importlib import metadata as importlib_metadata

# --- Minimal replica of runtime trust logic (respects MATRIX_SSL_TRUST and user env) ---
_SYSTEM_CA_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/WSL
    "/etc/pki/tls/certs/ca-bundle.crt",    # RHEL/CentOS/Fedora/Alma/Rocky
    "/etc/ssl/cert.pem",                   # macOS & Alpine
    "/usr/local/etc/openssl/cert.pem",     # Homebrew on macOS (Intel)
    "/opt/homebrew/etc/openssl@3/cert.pem",# Homebrew on macOS (Apple Silicon)
    "/etc/ssl/certs/ca-bundle.crt",        # SUSE/Arch variants
)

_DEF_MODE = (os.environ.get("MATRIX_SSL_TRUST", "auto") or "auto").strip().lower()
_user_overrode = bool(os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"))

if not _user_overrode and _DEF_MODE not in {"off", "0", "false", "disabled"}:
    injected = False
    if _DEF_MODE in {"truststore", "auto"}:
        try:
            import truststore  # type: ignore
            truststore.inject_into_ssl()
            injected = True
        except Exception:
            injected = False
    if not injected and _DEF_MODE in {"system", "auto"} and platform.system() != "Windows":
        for _ca in _SYSTEM_CA_CANDIDATES:
            try:
                if os.path.exists(_ca):
                    os.environ.setdefault("SSL_CERT_FILE", _ca)
                    break
            except Exception:
                pass

# --- helpers ---

def get_versions(names):
    out = {}
    for n in names:
        try:
            out[n] = importlib_metadata.version(n)
        except Exception:
            out[n] = None
    return out


def get_certifi():
    try:
        import certifi
        ca = certifi.where()
        size = sha = None
        try:
            with open(ca, "rb") as fh:
                data = fh.read()
            size = len(data)
            sha = hashlib.sha256(data).hexdigest()
        except Exception:
            pass
        try:
            ver = importlib_metadata.version("certifi")
        except Exception:
            ver = None
        return {"version": ver, "where": ca, "size": size, "sha256": sha}
    except Exception as e:
        return {"error": repr(e)}


def probe(url: str) -> dict:
    # Try httpx, fall back to urllib
    try:
        import httpx
        try:
            with httpx.Client(timeout=5.0) as c:
                r = c.get(url)
            return {"ok": True, "via": "httpx", "status": getattr(r, "status_code", None)}
        except Exception as e:
            return {"ok": False, "via": "httpx", "error": repr(e)}
    except Exception:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=5.0) as r:
                status = getattr(r, "status", None)
            return {"ok": True, "via": "urllib", "status": status}
        except Exception as e2:
            return {"ok": False, "via": "urllib", "error": repr(e2)}

info = {}
info["executable"] = sys.executable
info["python_version"] = platform.python_version()
info["platform"] = platform.platform()
info["base_prefix"] = sys.base_prefix
info["prefix"] = sys.prefix
try:
    info["openssl_version"] = ssl.OPENSSL_VERSION
    try:
        paths = ssl.get_default_verify_paths()
        info["ssl_default_paths"] = {
            "cafile": paths.cafile,
            "capath": paths.capath,
            "openssl_cafile_env": paths.openssl_cafile_env,
            "openssl_cafile": paths.openssl_cafile,
            "openssl_capath_env": paths.openssl_capath_env,
            "openssl_capath": paths.openssl_capath,
        }
    except Exception as e:
        info["ssl_default_paths_error"] = repr(e)
except Exception as e:
    info["ssl_error"] = repr(e)

_env_keys = [
    "SSL_CERT_FILE","REQUESTS_CA_BUNDLE","CURL_CA_BUNDLE",
    "HTTPS_PROXY","HTTP_PROXY","NO_PROXY","MATRIX_SSL_TRUST"
]
info["env"] = {k: os.environ.get(k) for k in _env_keys}
_packages_to_check = [
    "httpx", "requests", "urllib3", "idna",
    "cryptography", "certifi", "truststore", "pyopenssl"
]
info["packages"] = get_versions(_packages_to_check)

# Full installed set (best-effort)
try:
    from importlib import metadata as ilm
    installed = {}
    for d in ilm.distributions():
        if name := d.metadata.get("Name"):
            installed[name.lower()] = d.version
    info["installed"] = installed
except Exception as e:
    info["installed_error"] = repr(e)

# Certifi details
info["certifi"] = get_certifi()

# Connectivity probe (optional URL via env)
url = os.environ.get("DIAG_URL", "'''
    + DEFAULT_URL
    + """")
info["probe"] = probe(url)

print(json.dumps(info, ensure_ascii=False))
"""
)


def run_inspect(python_exe: str, url: str) -> dict:
    env = os.environ.copy()
    env.setdefault("DIAG_URL", url)
    try:
        r = subprocess.run(
            [python_exe, "-c", INSPECT_SNIPPET],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        return {"error": f"Interpreter not found: {python_exe}"}
    if r.returncode != 0:
        return {
            "error": f"Inspector failed with code {r.returncode}",
            "stderr": r.stderr.strip(),
        }
    try:
        return json.loads(r.stdout.strip())
    except Exception as e:
        return {
            "error": f"Failed to parse inspector output: {e}",
            "raw": r.stdout[:500],
        }


def detect_interpreters(cli_a: str | None, cli_b: str | None) -> tuple[str, str]:
    """Pick which two interpreters to compare (base vs .venv by default)."""
    py_cur = sys.executable

    def base_python() -> Path:
        return Path(sys.base_prefix) / (
            "python.exe" if os.name == "nt" else "bin/python3"
        )

    def venv_python() -> Path:
        return Path(".venv") / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )

    if cli_a and cli_b:
        return cli_a, cli_b

    if cli_a and not cli_b:
        other = base_python()
        if not other.exists() and venv_python().exists():
            other = venv_python()
        return cli_a, str(other)

    if cli_b and not cli_a:
        other = base_python()
        if not other.exists() and venv_python().exists():
            other = venv_python()
        return str(other), cli_b

    base_guess = base_python()
    venv_guess = venv_python()

    cand_a = str(base_guess if base_guess.exists() else py_cur)
    cand_b = str(
        venv_guess
        if venv_guess.exists()
        else (py_cur if base_guess.exists() else cand_a)
    )

    if Path(cand_a) == Path(cand_b):
        alt = "python" if os.name == "nt" else "python3"
        return cand_a, alt
    return cand_a, cand_b


def summarize_diffs(a: dict, b: dict, name_a: str, name_b: str) -> str:
    lines: list[str] = []

    def add(h: str) -> None:
        lines.append(h)

    def kv(label: str, va, vb) -> None:
        lines.append(f"- {label}:")
        lines.append(f"    {name_a}: {va}")
        lines.append(f"    {name_b}: {vb}")

    add("### Interpreters")
    kv("Executable", a.get("executable"), b.get("executable"))
    kv("Python", a.get("python_version"), b.get("python_version"))
    kv("Platform", a.get("platform"), b.get("platform"))
    kv("OpenSSL", a.get("openssl_version"), b.get("openssl_version"))

    add("\n### SSL verify paths")
    kv(
        "cafile",
        a.get("ssl_default_paths", {}).get("cafile"),
        b.get("ssl_default_paths", {}).get("cafile"),
    )
    kv(
        "capath",
        a.get("ssl_default_paths", {}).get("capath"),
        b.get("ssl_default_paths", {}).get("capath"),
    )

    add("\n### Certifi")
    ca, cb = a.get("certifi", {}), b.get("certifi", {})
    kv("certifi version", ca.get("version"), cb.get("version"))
    kv("certifi where", ca.get("where"), cb.get("where"))
    kv("certifi sha256", ca.get("sha256"), cb.get("sha256"))
    kv("certifi size", ca.get("size"), cb.get("size"))

    add("\n### Key packages")
    pkgs = [
        "httpx",
        "requests",
        "urllib3",
        "idna",
        "cryptography",
        "certifi",
        "truststore",
        "pyopenssl",
    ]
    for p in pkgs:
        kv(p, a.get("packages", {}).get(p), b.get("packages", {}).get(p))

    add("\n### Env vars (certificate/proxy)")
    for k in [
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "MATRIX_SSL_TRUST",
    ]:
        kv(k, a.get("env", {}).get(k), b.get("env", {}).get(k))

    add("\n### HTTPS probe")
    pa, pb = a.get("probe", {}), b.get("probe", {})
    kv("probe.ok", pa.get("ok"), pb.get("ok"))
    kv("probe.via", pa.get("via"), pb.get("via"))
    kv(
        "probe.status/error",
        pa.get("status") or pa.get("error"),
        pb.get("status") or pb.get("error"),
    )

    return "\n".join(lines)


# --- Diagnosis Helpers ---


def _is_cert_fail(p: dict | None) -> bool:
    if not p:
        return False
    err = p.get("error") or ""
    s = str(err).lower()
    return "certificate verify failed" in s or "certificate_verify_failed" in s


def _diagnose_probe_status(pa: dict, pb: dict, name_a: str, name_b: str) -> list[str]:
    """Check probe results and return initial diagnosis."""
    a_ok, b_ok = pa.get("ok"), pb.get("ok")
    if a_ok and not b_ok and _is_cert_fail(pb):
        return [
            (
                f"- {name_b} failed TLS verification while {name_a} succeeded. "
                f"This almost always means the **trust store** (certificate bundle) "
                f"used by {name_b} is different or missing."
            )
        ]
    if b_ok and not a_ok and _is_cert_fail(pa):
        return [
            (
                f"- {name_a} failed TLS verification while {name_b} succeeded. "
                f"This points to a trust-store problem in {name_a}."
            )
        ]
    if a_ok and b_ok:
        return [
            (
                "- Both environments succeeded the HTTPS probe. The original failure may be "
                "intermittent, proxy-related, or specific to the SDK runtime parameters."
            )
        ]
    if not a_ok and not b_ok:
        return [
            (
                "- Both environments failed the HTTPS probe. This suggests a network or proxy "
                "issue affecting both."
            )
        ]
    return []


def _diagnose_certifi_details(
    ca: dict, cb: dict, name_a: str, name_b: str
) -> list[str]:
    """Check for certifi differences and health."""
    msgs: list[str] = []
    if ca.get("version") != cb.get("version"):
        msgs.append(
            f"- Different **certifi** versions: {name_a}={ca.get('version')} "
            f"vs {name_b}={cb.get('version')}."
        )

    for name, cert in [(name_a, ca), (name_b, cb)]:
        if cert.get("version") is None and "error" in cert:
            msgs.append(f"- {name} appears to **lack certifi** ({cert.get('error')}).")
        if (
            cert.get("where")
            and cert.get("size") is not None
            and cert.get("size") < 50_000
        ):
            msgs.append(
                f"- {name} certifi bundle looks **suspiciously small** "
                f"({cert.get('size')} bytes). It may be corrupted."
            )
    return msgs


def _diagnose_env_overrides(
    env_a: dict, env_b: dict, name_a: str, name_b: str
) -> list[str]:
    """Check for SSL_CERT_FILE/REQUESTS_CA_BUNDLE overrides."""
    msgs: list[str] = []
    for name, env in [(name_a, env_a), (name_b, env_b)]:
        for key in ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"]:
            if val := env.get(key):
                if not Path(val).exists():
                    msgs.append(
                        f"- {name} sets **{key}={val}** but that path does not exist. "
                        f"This will break TLS verification."
                    )
                else:
                    msgs.append(
                        f"- {name} sets **{key}={val}**. If this file lacks your CA chain "
                        f"(e.g., corporate proxy), HTTPS can fail."
                    )
    return msgs


def _diagnose_proxies(env_a: dict, env_b: dict) -> list[str]:
    """Check for proxy differences."""
    msgs: list[str] = []
    for key in ["HTTPS_PROXY", "HTTP_PROXY"]:
        if (env_a.get(key) or "") != (env_b.get(key) or ""):
            msgs.append(
                (
                    f"- Different **{key}** proxy settings between environments. "
                    "A proxy that re-signs TLS traffic requires its root CA to be trusted."
                )
            )
    return msgs


def _diagnose_openssl(a: dict, b: dict, name_a: str, name_b: str) -> list[str]:
    """Check for OpenSSL version differences."""
    if a.get("openssl_version") != b.get("openssl_version"):
        return [
            (
                f"- Different **OpenSSL** builds: {name_a}={a.get('openssl_version')} vs "
                f"{name_b}={b.get('openssl_version')}. This can change default trust paths "
                "and certificate handling."
            )
        ]
    return []


def diagnose(a: dict, b: dict, name_a: str, name_b: str) -> str:
    """Heuristic diagnosis and suggested fixes when one env fails TLS verification."""
    msgs: list[str] = []

    pa, pb = a.get("probe", {}), b.get("probe", {})
    msgs.extend(_diagnose_probe_status(pa, pb, name_a, name_b))

    ca, cb = a.get("certifi", {}), b.get("certifi", {})
    msgs.extend(_diagnose_certifi_details(ca, cb, name_a, name_b))

    env_a, env_b = a.get("env", {}), b.get("env", {})
    msgs.extend(_diagnose_env_overrides(env_a, env_b, name_a, name_b))
    msgs.extend(_diagnose_proxies(env_a, env_b))

    msgs.extend(_diagnose_openssl(a, b, name_a, name_b))

    # Suggested fixes
    fixes = [
        (
            "Inside the failing environment: run `python -m pip install -U certifi "
            "httpx requests` to refresh the CA bundle and HTTP stack."
        ),
        (
            "If you are behind a corporate proxy, export REQUESTS_CA_BUNDLE/"
            "SSL_CERT_FILE to a PEM that includes your proxy's root CA."
        ),
        (
            "On Ubuntu/WSL, ensure system CAs are present: `sudo apt-get update && "
            "sudo apt-get install -y ca-certificates && sudo update-ca-certificates`."
        ),
        (
            "Use the OS trust store via `pip install truststore` and, early in your "
            "program, `import truststore; truststore.inject_into_ssl()`."
        ),
        (
            "As a quick test, set `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` "
            "in the failing shell and retry."
        ),
    ]

    return "\n".join(msgs + ["", "**Suggested fixes:**"] + [f"- {f}" for f in fixes])


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compare two Python environments and diagnose TLS failures."
    )
    ap.add_argument("--py-a", help="Path to first Python interpreter")
    ap.add_argument("--py-b", help="Path to second Python interpreter")
    ap.add_argument(
        "--url", default=DEFAULT_URL, help=f"URL to probe (default: {DEFAULT_URL})"
    )
    args = ap.parse_args()

    py_a, py_b = detect_interpreters(args.py_a, args.py_b)
    print(f"Comparing:\n  A = {py_a}\n  B = {py_b}\n  Probe URL: {args.url}\n")

    a = run_inspect(py_a, args.url)
    b = run_inspect(py_b, args.url)

    if "error" in a:
        print(f"[A ERROR] {a['error']}")
        if "stderr" in a:
            print(a["stderr"])
        return
    if "error" in b:
        print(f"[B ERROR] {b['error']}")
        if "stderr" in b:
            print(b["stderr"])
        return

    name_a = "A"
    name_b = "B"

    print("== Differences & Details ==")
    print(summarize_diffs(a, b, name_a, name_b))
    print("\n== Diagnosis ==")
    print(diagnose(a, b, name_a, name_b))

if __name__ == "__main__":
    main()
