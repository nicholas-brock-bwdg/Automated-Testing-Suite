"""
discover.py — Two-pass view discovery for Ignition Perspective.

Pass 1 (API):     Queries the Ignition REST API for the complete view tree.
Pass 2 (Browser): Validates reachability and detects auth requirements via
                  HTTP probing + HTML scanning. Full JS-rendered auth detection
                  (via agent-browser) is a planned future enhancement.

Entry point: run(config) -> list[dict]
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# Accept self-signed / internal CA certificates common in local Ignition installs.
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Credentials helper
# ---------------------------------------------------------------------------

def _credentials() -> Optional[tuple]:
    """Read test credentials from env vars. Returns (user, password) or None."""
    user = os.environ.get("IGNITION_TEST_USER", "").strip()
    pwd  = os.environ.get("IGNITION_TEST_PASSWORD", "").strip()
    return (user, pwd) if user else None


def _auth_opener(username, password, realm_url):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, realm_url, username, password)
    return urllib.request.build_opener(
        urllib.request.HTTPBasicAuthHandler(mgr),
        urllib.request.HTTPSHandler(context=_ssl_ctx),
    )


# ===========================================================================
# PASS 1 — API
# ===========================================================================

def _flatten_tree(node, prefix=""):
    """
    Recursively flatten the nested view tree the Ignition API returns.

    Handles:
        { "name": "Home", "resourceType": "...", "children": [...] }
    """
    name = node.get("name") or node.get("id") or ""
    if not name:
        return []

    path = f"{prefix}/{name}" if prefix else f"/{name}"
    children = node.get("children") or []

    paths = []
    # Leaf: has resourceType or no children
    if node.get("resourceType") or not children:
        paths.append(path)
    for child in children:
        paths.extend(_flatten_tree(child, path))
    return paths


def _extract_paths_from_data(data):
    """
    Convert whatever shape the API returned into a flat list of view paths.
    Handles: list[str], list[dict], {views:[...]}, tree {name,children}, unknown.
    """
    paths = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                p = item if item.startswith("/") else f"/{item}"
                paths.append(p)
            elif isinstance(item, dict):
                p = item.get("path") or item.get("name") or item.get("id")
                if p:
                    p = p if p.startswith("/") else f"/{p}"
                    paths.append(p)

    elif isinstance(data, dict):
        if "views" in data:
            raw = data["views"]
            for item in (raw if isinstance(raw, list) else []):
                if isinstance(item, str):
                    paths.append(item if item.startswith("/") else f"/{item}")
                elif isinstance(item, dict):
                    p = item.get("path") or item.get("name")
                    if p:
                        paths.append(p if p.startswith("/") else f"/{p}")

        elif "children" in data or "name" in data:
            paths = _flatten_tree(data)

        else:
            print(
                f"  WARNING: Unrecognised API response shape. "
                f"Keys: {list(data.keys())}. Attempting string extraction.",
                file=sys.stderr,
            )

            def _strings(obj):
                if isinstance(obj, str) and "/" in obj and len(obj) < 200:
                    yield obj
                elif isinstance(obj, dict):
                    for v in obj.values():
                        yield from _strings(v)
                elif isinstance(obj, list):
                    for v in obj:
                        yield from _strings(v)

            for s in _strings(data):
                paths.append(s if s.startswith("/") else f"/{s}")

    return paths


def api_pass(gateway_url, project_name, exclude_views, *, debug=True):
    """
    Query the Ignition REST API for the complete view list.

    Endpoint: GET {gateway_url}/data/perspective/views?projectName={project}

    Auth: tries unauthenticated first; retries with basic auth on HTTP 401.
    Returns a sorted, deduplicated list of view paths.
    Paths in exclude_views are filtered out.
    Logs the raw response structure so shape issues can be debugged without
    a full re-run if the API returns an unexpected format.
    """
    endpoint = (
        f"{gateway_url}/data/perspective/views"
        f"?projectName={urllib.parse.quote(project_name)}"
    )
    print(f"\n[Pass 1 — API]  GET {endpoint}")

    creds = _credentials()

    def _get(with_auth):
        if with_auth and creds:
            opener = _auth_opener(*creds, gateway_url)
        else:
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_ctx))
        req = urllib.request.Request(endpoint)
        try:
            with opener.open(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    status, body = _get(with_auth=False)
    if status == 401:
        if not creds:
            raise RuntimeError(
                "API returned 401 but no credentials are set. "
                "Set IGNITION_TEST_USER and IGNITION_TEST_PASSWORD."
            )
        print("  Endpoint requires auth — retrying with credentials...")
        status, body = _get(with_auth=True)

    if status != 200:
        raise RuntimeError(
            f"API pass failed: HTTP {status} from {endpoint}\n"
            "  Check IGNITION_GATEWAY_URL, IGNITION_PROJECT_NAME, and credentials."
        )

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"API returned non-JSON: {exc}") from exc

    # ----- Log raw structure for debugging -----
    print("  Raw API response:")
    if isinstance(data, list):
        print(f"    Type: list   Length: {len(data)}")
        if data and isinstance(data[0], dict):
            print(f"    First item keys: {list(data[0].keys())}")
        elif data and isinstance(data[0], str):
            print(f"    First item (str): {data[0]!r}")
    elif isinstance(data, dict):
        print(f"    Type: dict   Keys: {list(data.keys())}")
    print(f"    Content (first 1000 chars): {json.dumps(data)[:1000]}")

    # ----- Extract and normalise -----
    raw_paths = _extract_paths_from_data(data)
    excluded  = set(exclude_views or [])
    paths = sorted(
        {p.rstrip("/") for p in raw_paths if p and p.rstrip("/") not in excluded}
    )

    print(
        f"\n  {len(raw_paths)} raw paths → {len(paths)} after dedup/exclusion"
    )
    if debug:
        for p in paths:
            print(f"    {p}")

    return paths


# ===========================================================================
# PASS 2 — Browser (HTTP probe + HTML scan)
# ===========================================================================

_LOGIN_URL_SIGNALS = ["login", "/auth", "status=401", "status=403"]
_LOGIN_HTML_SIGNALS = [
    "perspective-login",
    'id="login"',
    'class="login',
    "ignition-login",
    "loginForm",
    "/main?referrer=",
    "session/auth",
    "Please log in",
]


def _probe(url, username=None, password=None):
    """
    Make a single HTTP request to url, following redirects.

    Returns a dict:
        status        int
        final_url     str
        reachable     bool
        requires_auth bool   — HTTP-level redirect or HTML scan
        error         str|None
    """
    result = {
        "status":        0,
        "final_url":     url,
        "reachable":     False,
        "requires_auth": False,
        "error":         None,
    }

    if username and password:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, username, password)
        opener = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(mgr),
            urllib.request.HTTPSHandler(context=_ssl_ctx),
        )
    else:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_ctx))

    html_preview = ""
    try:
        with opener.open(url, timeout=15) as resp:
            result["status"]    = resp.status
            result["final_url"] = resp.url
            html_preview        = resp.read(8192).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error"]  = f"HTTP {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        result["error"] = str(exc.reason)
        return result

    s     = result["status"]
    final = result["final_url"]

    if s in (401, 403):
        result["requires_auth"] = True
        result["reachable"]     = True
    elif s == 404:
        result["reachable"] = False
    elif s < 400:
        result["reachable"] = True
        if any(sig in final.lower() for sig in _LOGIN_URL_SIGNALS):
            result["requires_auth"] = True
        elif any(sig in html_preview for sig in _LOGIN_HTML_SIGNALS):
            result["requires_auth"] = True

    return result


def browser_pass(gateway_url, project_name, api_paths):
    """
    Best-effort browser pass: probe each API-discovered view via HTTP.

    For each view:
      1. Probe unauthenticated — detect HTTP-level auth redirects or errors.
      2. If auth was signalled, probe again with credentials to confirm reachability.

    Limitation: Perspective is a React SPA. Auth implemented purely client-side
    (no HTTP redirect) will not be detected here. Full JS-rendered detection
    requires Playwright or agent-browser (planned for a future phase).

    Returns a list of result dicts, one per view.
    """
    print(
        f"\n[Pass 2 — Browser]  Probing {len(api_paths)} views...\n"
        "  Note: JS-rendered auth detection requires agent-browser (future phase)."
    )

    creds            = _credentials()
    username, password = creds if creds else (None, None)
    results = []

    for path in api_paths:
        view_url = (
            f"{gateway_url}/data/perspective/client"
            f"/{urllib.parse.quote(project_name)}{path}"
        )

        unauth = _probe(view_url)

        auth_probe = None
        if (unauth["requires_auth"] or not unauth["reachable"]) and username:
            auth_probe = _probe(view_url, username, password)

        reachable     = unauth["reachable"]
        requires_auth = unauth["requires_auth"]

        if auth_probe and auth_probe["reachable"] and not unauth["reachable"]:
            requires_auth = True
            reachable     = True

        status_label = (
            f"error: {unauth['error']}" if unauth["error"]
            else f"HTTP {unauth['status']}"
        )
        icon      = "\u2713" if reachable else "\u2717"
        auth_note = " [auth required]" if requires_auth else ""
        print(f"    {icon}  {path:<45}  {status_label}{auth_note}")

        results.append({
            "path":          path,
            "url":           view_url,
            "reachable":     reachable,
            "requires_auth": requires_auth,
            "nav_path":      [],   # populated by agent-browser in future phase
        })

    return results


# ===========================================================================
# RECONCILIATION
# ===========================================================================

def reconcile(api_paths, browser_results):
    """
    Merge API and browser results into the unified view list for the manifest.

    Tagging rules:
      API-only              -> discovered_by="api",    reachable assumed True
      Both (reachable)      -> discovered_by="both",   use browser values
      Both (not reachable)  -> discovered_by="both",   warning added
      Browser-only          -> discovered_by="browser", warning added
    """
    by_path     = {r["path"]: r for r in browser_results}
    api_set     = set(api_paths)
    browser_set = set(by_path.keys())

    views = []

    for path in sorted(api_paths):
        br = by_path.get(path)

        if br is None:
            entry = {
                "path":          path,
                "discovered_by": "api",
                "reachable":     True,
                "requires_auth": False,
                "nav_path":      [],
            }
        else:
            warnings = []
            if not br["reachable"]:
                warnings.append(
                    "View found in API but not reachable via browser probe"
                )
            entry = {
                "path":          path,
                "discovered_by": "both",
                "reachable":     br["reachable"],
                "requires_auth": br["requires_auth"],
                "nav_path":      br.get("nav_path", []),
            }
            if warnings:
                entry["warnings"] = warnings

        views.append(entry)

    # Browser-only: reachable via navigation but absent from API
    for path in sorted(browser_set - api_set):
        br = by_path[path]
        views.append({
            "path":          path,
            "discovered_by": "browser",
            "reachable":     True,
            "requires_auth": br["requires_auth"],
            "nav_path":      br.get("nav_path", []),
            "warnings":      ["View reachable via browser but absent from API"],
        })

    return views


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def run(config, *, debug=True):
    """
    Run full two-pass discovery against the live gateway.

    Args:
        config: loaded gateway-config.json dict
        debug:  if True, print each discovered path during the API pass

    Returns:
        Reconciled list of view dicts, ready for manifest.build_manifest().

    Raises:
        RuntimeError on unrecoverable API pass failure.
    """
    gateway_url  = config["gateway_url"].rstrip("/")
    project_name = config["project_name"]
    exclude      = config.get("exclude_views", [])

    # Pass 1: API
    api_paths = api_pass(gateway_url, project_name, exclude, debug=debug)

    if not api_paths:
        print(
            "\n  WARNING: API pass returned no views. "
            "Check IGNITION_PROJECT_NAME and gateway connectivity.",
            file=sys.stderr,
        )

    # Pass 2: Browser (best-effort)
    try:
        browser_results = browser_pass(gateway_url, project_name, api_paths)
    except Exception as exc:
        print(
            f"\n  WARNING: Browser pass failed — {exc}\n"
            "  Continuing with API-only results.",
            file=sys.stderr,
        )
        browser_results = []

    # Reconcile
    views = reconcile(api_paths, browser_results)

    # Summary
    reachable_n = sum(1 for v in views if v.get("reachable"))
    auth_n      = sum(1 for v in views if v.get("requires_auth"))
    warn_n      = sum(1 for v in views if v.get("warnings"))

    print(f"\n  Discovery summary:")
    print(f"    Total views   : {len(views)}")
    print(f"    Reachable     : {reachable_n}")
    print(f"    Require auth  : {auth_n}")
    if warn_n:
        print(f"    With warnings : {warn_n}")

    return views
