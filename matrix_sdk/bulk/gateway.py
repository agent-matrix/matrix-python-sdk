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

    def __init__(
        self, base_url: str, token: Optional[str] = None, timeout: float = 20.0
    ):
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

    def _prepare_payload(self, manifest: Any) -> Dict[str, Any]:
        """Normalize the manifest into a JSON-safe dictionary."""
        if isinstance(manifest, ServerManifest):
            return manifest.to_jsonable()
        if isinstance(manifest, dict):
            # Defensive: ensure any stray non-JSON types are stringified
            return json.loads(json.dumps(manifest, default=str))

        # Attempt Pydantic v2, then v1 serialization
        for method_name in ("model_dump_json", "json"):
            if hasattr(manifest, method_name):
                try:
                    dump_method = getattr(manifest, method_name)
                    return json.loads(dump_method(by_alias=True, exclude_none=True))
                except Exception:
                    continue  # Try the next method if serialization fails

        raise TypeError(f"Unsupported manifest type: {type(manifest)!r}")

    def _parse_response(self, resp: httpx.Response) -> Dict[str, Any]:
        """Parse a successful response, returning JSON or a success object."""
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"ok": True, "raw": resp.text}

    def _get_error_details(
        self, e: httpx.HTTPError
    ) -> tuple[int | str, Any, str | None]:
        """Extracts status, body, and request ID from an HTTP error."""
        if hasattr(e, "response") and e.response is not None:
            response = e.response
            status = response.status_code
            rid = response.headers.get("x-request-id") or response.headers.get(
                "request-id"
            )
            try:
                body = response.json()
            except json.JSONDecodeError:
                body = {"error": response.text or str(e)}
            return status, body, rid
        else:
            # Network/transport error without a response
            return "?", str(e), None

    def _should_fallback_to_form(self, e: httpx.HTTPStatusError) -> bool:
        """Determines if a failed JSON request should be retried as a form."""
        status, body, _ = self._get_error_details(e)

        if status not in (400, 415, 422):
            return False

        msg = ""
        if isinstance(body, dict):
            msg = str(body.get("message") or body.get("detail") or body)

        missing_or_bad_name = (
            ("Missing required field" in msg and "'name'" in msg)
            or ("name" in msg and "missing" in msg.lower())
            or ("name" in msg and "invalid" in msg.lower())
        )
        return missing_or_bad_name

    async def _try_form_upsert(
        self,
        client: httpx.AsyncClient,
        url: str,
        original_headers: Dict[str, str],
        payload: Dict[str, Any],
        original_error: httpx.HTTPStatusError,
    ) -> Dict[str, Any]:
        """Attempt #2: form-encoded POST for legacy admin endpoints."""
        form = _make_admin_form(payload)
        if not form.get("name"):
            # No way to build a valid form request; bubble up original error
            status, body, rid = self._get_error_details(original_error)
            error_message = (
                f"Gateway upsert failed (no name for form fallback): "
                f"HTTP {status}, request_id={rid}, body={body}"
            )
            raise RuntimeError(error_message) from original_error

        # For form submit, switch content-type; keep other headers
        form_headers = {
            k: v for k, v in original_headers.items() if k.lower() != "content-type"
        }

        try:
            resp2 = await client.post(url, headers=form_headers, data=form)
            resp2.raise_for_status()
            return self._parse_response(resp2)
        except httpx.HTTPError as e2:
            # The form fallback also failed.
            code2, body2, rid2 = self._get_error_details(e2)
            raise RuntimeError(
                f"Gateway form upsert failed: {code2}, request_id={rid2}, body={body2}"
            ) from e2

    async def upsert_server(
        self, manifest: Union[ServerManifest, Dict[str, Any]], *, idempotency_key: str
    ) -> Dict[str, Any]:
        url = f"{self.base}/admin/servers"
        hdrs = dict(self.headers)
        hdrs["Idempotency-Key"] = idempotency_key
        payload = self._prepare_payload(manifest)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                # --- Attempt #1: JSON POST (preferred modern API) ---
                resp = await client.post(url, headers=hdrs, json=payload)
                resp.raise_for_status()
                return self._parse_response(resp)

            except httpx.HTTPStatusError as e:
                # JSON post failed; check if we should fallback to a form post
                if self._should_fallback_to_form(e):
                    return await self._try_form_upsert(client, url, hdrs, payload, e)

                # Not a fallback scenario, so it's a genuine error.
                status, body, rid = self._get_error_details(e)
                raise RuntimeError(
                    f"Gateway upsert failed: HTTP {status}, request_id={rid}, body={body}"
                ) from e

            except httpx.HTTPError as e:
                # Network / transport errors
                raise RuntimeError(f"Gateway request error: {e}") from e
