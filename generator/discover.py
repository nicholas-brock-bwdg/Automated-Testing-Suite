"""
discover.py — Two-pass view discovery for Ignition Perspective.

Pass 1 (Filesystem): Walk views_directory for view.json files.
                     No gateway, no network, no auth required.
Pass 2 (Gateway):   Validate reachability against the live gateway via the
                    REST API + HTTP probing. Non-blocking: if the gateway is
                    unreachable the run continues with filesystem-only results.

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
from pathlib import Path
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
# PASS 1 — Filesystem
# ===========================================================================

def filesystem_pass(views_directory, exclude_views, *, debug=True):
    """
    Walk views_directory and collect view paths from view.json files.

    Each Perspective view is a folder containing a view.json file.
    The folder path relative to views_directory becomes the view path:
        {views_directory}/folder/subpage/view.json  ->  /folder/subpage

    Args:
        views_directory: Path (str or Path) to the views root.
        exclude_views:   List of view paths to exclude.
        debug:           If True, print each discovered path.

    Returns:
        Sorted, deduplicated list of view path strings (each starting with /).
    """
    views_dir = Path(views_directory)
    print(f"\n[Pass 1 — Filesystem]  Walking {views_dir.resolve()}")

    if not views_dir.exists():
        print(
            f"  WARNING: views_directory '{views_dir}' does not exist. "
            "No filesystem views discovered.",
            file=sys.stderr,
        )
        return []

    excluded  = set(exclude_views or [])
    raw_paths = []

    for view_json in sorted(views_dir.rglob("view.json")):
        rel  = view_json.parent.relative_to(views_dir)
        path = "/" + rel.as_posix()   # e.g. /folder/subpage
        raw_paths.append(path)

    paths = sorted(
        {p.rstrip("/") for p in raw_paths if p and p.rstrip("/") not in excluded}
    )

    print(f"\n  {len(raw_paths)} raw paths -> {len(paths)} after dedup/exclusion")
    if debug:
        for p in paths:
            print(f"    {p}")

    return paths


# ===========================================================================
# PASS 2 — Gateway validation (API fetch + HTTP probe)
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

    path     = f"{prefix}/{name}" if prefix else f"/{name}"
    children = node.get("children") or []

    paths = []
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


def _api_fetch(gateway_url, project_name):
    """
    Fetch the view list from the Ignition REST API.

    Returns (list[str] paths, error_str_or_None).
    On any failure returns ([], error_message) — does not raise.
    """
    endpoint = (
        f"{gateway_url}/data/perspective/views"
        f"?projectName={urllib.parse.quote(project_name)}"
    )
    print(f"\n[Pass 2 — Gateway]  GET {endpoint}")

    creds = _credentials()

    def _get(with_auth):
        if with_auth and creds:
            opener = _auth_opener(*creds, gateway_url)
        else:
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=_ssl_ctx)
            )
        req = urllib.request.Request(endpoint)
        try:
            with opener.open(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""
        except urllib.error.URLError as exc:
            return None, str(exc.reason)

    status, body = _get(with_auth=False)

    if status is None:
        return [], f"Gateway unreachable: {body}"

    if status == 401:
        if not creds:
            return [], (
                "API returned 401 but no credentials are set. "
                "Set IGNITION_TEST_USER and IGNITION_TEST_PASSWORD."
            )
        print("  Endpoint requires auth — retrying with credentials...")
        status, body = _get(with_auth=True)

    if status != 200:
        return [], f"HTTP {status} from {endpoint}"

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return [], f"API returned non-JSON: {exc}"

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

    raw_paths = _extract_paths_from_data(data)
    paths     = sorted({p.rstrip("/") for p in raw_paths if p})
    print(f"  {len(raw_paths)} raw paths -> {len(paths)} unique gateway paths")
    return paths, None


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
        requires_auth bool
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


def gateway_pass(gateway_url, project_name, fs_paths):
    """
    Validate filesystem-discovered views against the live gateway.

    Strategy:
      1. Fetch the gateway's own view list via the REST API.
      2. HTTP-probe each filesystem URL to confirm reachability and detect auth.

    Non-blocking: on any gateway failure, logs a warning and returns empty
    results so the caller can continue with filesystem-only results.

    Returns:
        (gateway_paths: list[str], probe_results: list[dict], error: str|None)
    """
    # Step 1: API fetch
    gateway_paths, api_error = _api_fetch(gateway_url, project_name)
    if api_error:
        print(
            f"\n  WARNING: Gateway API fetch failed — {api_error}\n"
            "  Continuing with filesystem-only results.",
            file=sys.stderr,
        )
        return [], [], api_error

    # Step 2: HTTP probe each filesystem path
    print(
        f"\n  Probing {len(fs_paths)} filesystem views via HTTP...\n"
        "  Note: JS-rendered auth detection requires agent-browser (future phase)."
    )

    creds              = _credentials()
    username, password = creds if creds else (None, None)
    probe_results      = []

    for path in fs_paths:
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

        probe_results.append({
            "path":          path,
            "url":           view_url,
            "reachable":     reachable,
            "requires_auth": requires_auth,
            "nav_path":      [],
        })

    return gateway_paths, probe_results, None


# ===========================================================================
# RECONCILIATION
# ===========================================================================

def reconcile(fs_paths, gateway_paths, probe_results):
    """
    Merge filesystem and gateway results into the unified view list.

    Filesystem is the source of truth for what views exist.

    Tagging rules:
      fs only (unvalidated)  -> discovered_by="filesystem",   reachable=None
      fs + gateway confirmed -> discovered_by="both",         use probe values
      gateway only           -> discovered_by="gateway_only", warning added
    """
    probe_by_path = {r["path"]: r for r in probe_results}
    gateway_set   = set(gateway_paths)
    fs_set        = set(fs_paths)

    views = []

    for path in sorted(fs_paths):
        pr = probe_by_path.get(path)

        if pr is None:
            # Gateway not available — filesystem only, reachability unknown
            entry = {
                "path":          path,
                "discovered_by": "filesystem",
                "reachable":     None,
                "requires_auth": False,
                "nav_path":      [],
                "warnings":      ["Gateway validation not performed — view unvalidated"],
            }
        else:
            warnings = []
            if not pr["reachable"]:
                warnings.append(
                    "View found on filesystem but not reachable via gateway probe"
                )
            entry = {
                "path":          path,
                "discovered_by": "both",
                "reachable":     pr["reachable"],
                "requires_auth": pr["requires_auth"],
                "nav_path":      pr.get("nav_path", []),
            }
            if warnings:
                entry["warnings"] = warnings

        views.append(entry)

    # Gateway-only: returned by gateway API but absent from filesystem
    for path in sorted(gateway_set - fs_set):
        views.append({
            "path":          path,
            "discovered_by": "gateway_only",
            "reachable":     True,
            "requires_auth": False,
            "nav_path":      [],
            "warnings":      [
                "View returned by gateway API but not found on filesystem — "
                "possible stale gateway state or uncommitted view"
            ],
        })

    return views


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def run(config, *, debug=True):
    """
    Run two-pass filesystem-first discovery.

    Pass 1: Walk views_directory for view.json files (no gateway required).
    Pass 2: Validate against live gateway API + HTTP probe (non-blocking).

    Args:
        config: loaded gateway-config.json dict
        debug:  if True, print each discovered path during the filesystem pass

    Returns:
        Reconciled list of view dicts, ready for manifest.build_manifest().
    """
    gateway_url  = config["gateway_url"].rstrip("/")
    project_name = config["project_name"]
    exclude      = config.get("exclude_views", [])
    views_dir    = config.get("views_directory", "ignition/views")

    # Pass 1: Filesystem
    fs_paths = filesystem_pass(views_dir, exclude, debug=debug)

    if not fs_paths:
        print(
            f"\n  WARNING: Filesystem pass returned no views. "
            f"Check that views_directory '{views_dir}' contains view.json files.",
            file=sys.stderr,
        )

    # Pass 2: Gateway validation (best-effort, non-blocking)
    try:
        gateway_paths, probe_results, gateway_error = gateway_pass(
            gateway_url, project_name, fs_paths
        )
    except Exception as exc:
        print(
            f"\n  WARNING: Gateway pass failed unexpectedly — {exc}\n"
            "  Continuing with filesystem-only results.",
            file=sys.stderr,
        )
        gateway_paths = []
        probe_results = []
        gateway_error = str(exc)

    # Reconcile
    views = reconcile(fs_paths, gateway_paths, probe_results)

    # Summary
    reachable_n   = sum(1 for v in views if v.get("reachable") is True)
    unvalidated_n = sum(1 for v in views if v.get("reachable") is None)
    auth_n        = sum(1 for v in views if v.get("requires_auth"))
    gw_only_n     = sum(1 for v in views if v.get("discovered_by") == "gateway_only")
    warn_n        = sum(1 for v in views if v.get("warnings"))

    print(f"\n  Discovery summary:")
    print(f"    Total views      : {len(views)}")
    print(f"    Reachable        : {reachable_n}")
    if unvalidated_n:
        print(f"    Unvalidated      : {unvalidated_n}  (gateway not available)")
    print(f"    Require auth     : {auth_n}")
    if gw_only_n:
        print(f"    Gateway-only     : {gw_only_n}  (not on filesystem)")
    if warn_n:
        print(f"    With warnings    : {warn_n}")

    return views
