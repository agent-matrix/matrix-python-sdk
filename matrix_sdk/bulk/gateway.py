# matrix_sdk/bulk/gateway.py
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional, Union

import httpx

from .models import ServerManifest

# -------------------------- name sanitization --------------------------

_ALLOWED_NAME = re.compile(r"^[A-Za-z0-9_.\-\s]+$")
_SANITIZE_NAME = re.compile(r"[^A-Za-z0-9_.\-\s]+")


def _clean_name(raw: str) -> str:
    """
    Make a safe server name matching gateway rules:
      - keep letters/digits/underscore/dot/hyphen/space
      - collapse whitespace, trim
      - cut to 255 chars
      - if empty after cleaning, synthesize a stable fallback
    """
    if not raw:
        return "server-" + hashlib.sha1(b"default").hexdigest()[:8]
    s = _SANITIZE_NAME.sub(" ", raw)
    s = re.sub(r"\s+", " ", s).strip()
    s = s[:255]
    if not s or not _ALLOWED_NAME.match(s):
        s = "server-" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:8]
    return s


def _clean_desc(raw: Any) -> str:
    """
    Best-effort description cleaner (control chars → space; 4096-char cap).
    """
    text = str(raw or "")
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", " ", text)
    return text.strip()[:4096]


def _make_admin_form(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Map our normalized manifest to the minimal Admin form fields.
    The legacy /admin/servers create endpoint expects form keys like:
      - name (required)
      - description (optional)
      - icon (optional)
      - associatedTools / associatedResources / associatedPrompts (optional)
    We only send what we know; 'name' is mandatory.
    """
    # Prefer manifest 'name', then 'id', then endpoint URL as a last resort
    raw_name = (
        payload.get("name")
        or payload.get("id")
        or ((payload.get("endpoint") or {}).get("url"))
        or "server"
    )
    name = _clean_name(str(raw_name))

    desc = _clean_desc(payload.get("summary") or payload.get("description"))

    # Optional icon — only include if it looks like a URL the gateway will accept
    icon = payload.get("icon") or (payload.get("endpoint", {}) or {}).get("icon") or ""
    icon = str(icon) if icon is not None else ""
    icon_ok = icon.startswith(("http://", "https://", "ws://", "wss://"))

    form: Dict[str, str] = {"name": name}
    if desc:
        form["description"] = desc
    if icon_ok:
        form["icon"] = icon

    # If you maintain associations, you can include them here as CSV values:
    # form["associatedTools"] = ",".join(tool_ids)
    # form["associatedResources"] = ",".join(resource_ids)
    # form["associatedPrompts"] = ",".join(prompt_ids)

    return form


class GatewayAdminClient:
    """Client for MCP Gateway Admin API.

    Primary path: POST {base}/admin/servers
      - Some gateways accept JSON (preferred modern API)
      - Others accept only form URL-encoded
    This client posts JSON first, then auto-falls back to form if needed.
    """

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 20.0):
        if not base_url:
            raise ValueError("base_url is required")
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def upsert_server(
        self, manifest: Union[ServerManifest, Dict[str, Any]], *, idempotency_key: str
    ) -> Dict[str, Any]:
        url = f"{self.base}/admin/servers"
        hdrs = dict(self.headers)
        hdrs["Idempotency-Key"] = idempotency_key

        # --- Build a JSON-safe payload (no AnyUrl or other non-JSON types) ---
        if isinstance(manifest, ServerManifest):
            payload: Dict[str, Any] = manifest.to_jsonable()
        elif isinstance(manifest, dict):
            # Defensive: ensure any stray non-JSON types are stringified
            payload = json.loads(json.dumps(manifest, default=str))
        else:
            # Pydantic BaseModel (v2 or v1) fallback if other models are passed in
            try:
                payload = json.loads(
                    manifest.model_dump_json(by_alias=True, exclude_none=True)  # type: ignore[attr-defined]
                )  # Pydantic v2
            except Exception:
                try:
                    payload = json.loads(
                        manifest.json(by_alias=True, exclude_none=True)  # type: ignore[attr-defined]
                    )  # Pydantic v1
                except Exception as e:
                    raise TypeError(f"Unsupported manifest type: {type(manifest)!r}") from e

        # --- Attempt #1: JSON POST (preferred modern API) ---
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(url, headers=hdrs, json=payload)
                resp.raise_for_status()
                try:
                    return resp.json()
                except Exception:
                    return {"ok": True, "raw": resp.text}

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                # Try to detect legacy/form-only endpoints (422/400/415 with "name" missing/invalid)
                try:
                    body = e.response.json()
                except Exception:
                    body = {"error": e.response.text if e.response is not None else str(e)}

                msg = ""
                if isinstance(body, dict):
                    msg = str(body.get("message") or body.get("detail") or body)

                missing_or_bad_name = (
                    ("Missing required field" in msg and "'name'" in msg)
                    or ("name" in msg and "missing" in msg.lower())
                    or ("name" in msg and "invalid" in msg.lower())
                )
                should_fallback = status in (400, 415, 422) and missing_or_bad_name

                if not should_fallback:
                    rid = e.response.headers.get("x-request-id") or e.response.headers.get("request-id")
                    raise RuntimeError(
                        f"Gateway upsert failed: HTTP {status}, request_id={rid}, body={body}"
                    ) from e

                # --- Attempt #2: form-encoded POST (legacy admin create) ---
                form = _make_admin_form(payload)
                if not form.get("name"):
                    # No reasonable way to build a form request; bubble up original error
                    rid = e.response.headers.get("x-request-id") or e.response.headers.get("request-id")
                    raise RuntimeError(
                        f"Gateway upsert failed (no name for form fallback): HTTP {status}, request_id={rid}, body={body}"
                    ) from e

                # For form submit, switch content-type; keep Accept and auth headers
                form_headers = {k: v for k, v in hdrs.items() if k.lower() != "content-type"}

                try:
                    resp2 = await client.post(url, headers=form_headers, data=form)
                    resp2.raise_for_status()
                    try:
                        return resp2.json()
                    except Exception:
                        return {"ok": True, "raw": resp2.text}
                except httpx.HTTPError as e2:
                    rid2 = None
                    body2: Any
                    if hasattr(e2, "response") and e2.response is not None:
                        rid2 = e2.response.headers.get("x-request-id") or e2.response.headers.get("request-id")
                        try:
                            body2 = e2.response.json()
                        except Exception:
                            body2 = e2.response.text
                        code2 = e2.response.status_code
                    else:
                        body2 = str(e2)
                        code2 = "?"
                    raise RuntimeError(
                        f"Gateway form upsert failed: {code2}, request_id={rid2}, body={body2}"
                    ) from e2

            except httpx.HTTPError as e:
                # Network / transport errors
                raise RuntimeError(f"Gateway request error: {e}") from e
