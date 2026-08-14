#!/usr/bin/env python3
"""Hostinger VPS sources: Docker container summary (page 2) and VPS metrics (page 3).

Talks to the official Hostinger API (developers.hostinger.com). The VPS is
resolved once from the account's virtual-machine list (first running VM) and
cached for the process lifetime, together with its RAM/disk totals so the
metrics page can turn absolute byte readings into percentages.

Token comes from HOSTINGER_API_TOKEN or the daemon config file
(hostinger_token=...). Runs under SourceCache: never blocks the BLE loop,
and any failure returns an {"ok": False} payload rather than raising.

Payload formats (strictly under 240 bytes):
Page 2 (Docker):
{
  "p": 2,
  "ok": true,
  "total": 43,
  "up": 43,
  "proj": 13,
  "down": []         # up to 3 down/unhealthy container names
}

Page 3 (VPS):
{
  "p": 3,
  "ok": true,
  "cpu": 18,         # CPU %
  "ram": 42,         # RAM %
  "disk": 55,        # Disk %
  "up_d": 45         # Uptime days
}
"""

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

PAGE_DOCKER = 2
PAGE_VPS = 3
PAGE_VPS_DETAIL = 8
PAGE_DOCKER_PROJECTS = 9
MAX_PAYLOAD_BYTES = 240

API_BASE = "https://developers.hostinger.com/api/vps/v1"

def get_hostinger_token() -> Optional[str]:
    """Retrieve Hostinger API token from environment or config file."""
    if token := os.environ.get("HOSTINGER_API_TOKEN"):
        return token

    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    config_file = local_appdata / "CodingAssistant" / "config"
    if config_file.exists():
        try:
            for line in config_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("hostinger_token=") or line.startswith("vps_token="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return None


# Resolved once per process. A VM swap (new id) needs a daemon restart, which
# matches how rarely it happens. Carries the identity fields the VPS detail
# page shows alongside the totals the metrics math needs.
_vm: Dict[str, Any] = {}


async def _resolve_vm(client: httpx.AsyncClient,
                      token: str) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Returns (err, vm): err is None on success, else a wire error code.

    The codes are deliberately distinct because the firmware prunes on some
    and not others: auth_failed (bad/revoked token) and no_vm (account has no
    VPS) mean "this source will never produce data here" and prune; http_NNN
    is transient and keeps the page in the cycle.
    """
    if _vm.get("id"):
        return None, _vm
    resp = await client.get(f"{API_BASE}/virtual-machines",
                            headers={"Authorization": f"Bearer {token}"})
    if resp.status_code in (401, 403):
        return "auth_failed", None
    if resp.status_code != 200:
        return f"http_{resp.status_code}", None
    vms = resp.json() or []
    running = [v for v in vms if v.get("state") == "running"] or vms
    if not running:
        return "no_vm", None
    v = running[0]
    ipv4 = (v.get("ipv4") or [{}])[0].get("address", "")
    _vm.update({
        "id": v["id"],
        "ram_mb": int(v.get("memory", 0)),
        "disk_mb": int(v.get("disk", 0)),
        "hostname": v.get("hostname", ""),
        "ip": ipv4,
        "plan": v.get("plan", ""),
    })
    return None, _vm


def _ascii_name(name: str) -> str:
    """Clip to 19 chars with an ASCII '..' - the device fonts are ASCII-only
    and its DOCKER_NAME_COLS buffer holds 19 chars + NUL."""
    name = name.encode("ascii", "replace").decode()
    return name if len(name) <= 19 else name[:17] + ".."


async def fetch_docker_summary(client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Page-2 half of fetch_docker_pages, kept for callers that want one page."""
    return (await fetch_docker_pages(client))["summary"]


def _project_rows(projects) -> tuple[list, int]:
    """Pre-rendered per-project rows for the drill-down, sick projects first.

    "name up/total" per row; a degraded project gets a leading "!" so the
    firmware can colour it without parsing.
    """
    scored = []
    for proj in projects:
        containers = proj.get("containers") or []
        total = len(containers)
        up = sum(1 for c in containers
                 if c.get("state") == "running"
                 and "unhealthy" not in (c.get("health") or ""))
        name = _ascii_name(proj.get("name") or "?")
        prefix = "!" if up < total else ""
        scored.append((up < total, f"{prefix}{name} {up}/{total}"))
    scored.sort(key=lambda t: not t[0])          # degraded projects first
    rows = [row for _, row in scored]
    return rows[:6], max(0, len(rows) - 6)


async def fetch_docker_pages(client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Both Docker pages from ONE /docker call: the page-2 summary and the
    page-9 per-project drill-down. Returned as a pair so the SourceCache holds
    a single consistent snapshot; build_page_payload picks the requested half.
    """
    token = get_hostinger_token()
    if not token:
        return {"summary": {"p": PAGE_DOCKER, "ok": False, "err": "no_token"},
                "projects": {"p": PAGE_DOCKER_PROJECTS, "ok": False, "err": "no_token"}}

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)

    try:
        err, vm = await _resolve_vm(client, token)
        if err:
            return {"summary": {"p": PAGE_DOCKER, "ok": False, "err": err},
                    "projects": {"p": PAGE_DOCKER_PROJECTS, "ok": False, "err": err}}

        resp = await client.get(f"{API_BASE}/virtual-machines/{vm['id']}/docker",
                                headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            e = f"http_{resp.status_code}"
            return {"summary": {"p": PAGE_DOCKER, "ok": False, "err": e},
                    "projects": {"p": PAGE_DOCKER_PROJECTS, "ok": False, "err": e}}

        projects = resp.json() or []
        total = up = 0
        down_names = []
        for proj in projects:
            for c in proj.get("containers") or []:
                total += 1
                healthy = (c.get("state") == "running"
                           and "unhealthy" not in (c.get("health") or ""))
                if healthy:
                    up += 1
                else:
                    down_names.append(_ascii_name(c.get("name") or "unknown"))

        summary = {
            "p": PAGE_DOCKER, "ok": True,
            "total": total, "up": up, "proj": len(projects),
            "down": down_names[:3],
        }
        if len(json.dumps(summary, separators=(",", ":")).encode()) > MAX_PAYLOAD_BYTES:
            summary["down"] = summary["down"][:1]

        rows, more = _project_rows(projects)
        proj_page = {"p": PAGE_DOCKER_PROJECTS, "ok": True, "rows": rows, "more": more}
        while (len(json.dumps(proj_page, separators=(",", ":")).encode())
               > MAX_PAYLOAD_BYTES and proj_page["rows"]):
            proj_page["rows"] = proj_page["rows"][:-1]
            proj_page["more"] += 1

        return {"summary": summary, "projects": proj_page}
    except Exception as e:
        msg = str(e)[:30]
        return {"summary": {"p": PAGE_DOCKER, "ok": False, "err": msg},
                "projects": {"p": PAGE_DOCKER_PROJECTS, "ok": False, "err": msg}}
    finally:
        if close_client:
            await client.aclose()


def _latest(series: Dict[str, Any]) -> float:
    """Newest sample from a metrics time series {"usage": {ts: value}}."""
    usage = (series or {}).get("usage") or {}
    if not usage:
        return 0.0
    return float(usage[max(usage, key=int)])


async def fetch_vps_metrics(client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Page-3 half of fetch_vps_pages, kept for callers that want one page."""
    return (await fetch_vps_pages(client))["metrics"]


def _gb(nbytes: float) -> float:
    return round(nbytes / (1024.0 ** 3), 1)


async def fetch_vps_pages(client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Both VPS pages from ONE /metrics call over the last 15 min: the page-3
    percentage bars and the page-8 detail (identity, network traffic, absolute
    GB figures). Paired so both halves describe the same instant.
    """
    token = get_hostinger_token()
    if not token:
        return {"metrics": {"p": PAGE_VPS, "ok": False, "err": "no_token"},
                "detail": {"p": PAGE_VPS_DETAIL, "ok": False, "err": "no_token"}}

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)

    try:
        err, vm = await _resolve_vm(client, token)
        if err:
            return {"metrics": {"p": PAGE_VPS, "ok": False, "err": err},
                    "detail": {"p": PAGE_VPS_DETAIL, "ok": False, "err": err}}

        now = _dt.datetime.now(_dt.timezone.utc)
        # 30 minutes, not less: the API samples sparsely and a tight window
        # can come back empty, which would render every figure as a false 0.
        params = {
            "date_from": (now - _dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date_to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        resp = await client.get(f"{API_BASE}/virtual-machines/{vm['id']}/metrics",
                                params=params,
                                headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            e = f"http_{resp.status_code}"
            return {"metrics": {"p": PAGE_VPS, "ok": False, "err": e},
                    "detail": {"p": PAGE_VPS_DETAIL, "ok": False, "err": e}}

        m = resp.json() or {}
        ram_used = _latest(m.get("ram_usage"))
        disk_used = _latest(m.get("disk_space"))
        ram_total = vm["ram_mb"] * 1024.0 * 1024.0
        disk_total = vm["disk_mb"] * 1024.0 * 1024.0
        up_days = int(_latest(m.get("uptime"))) // 86400

        metrics = {
            "p": PAGE_VPS, "ok": True,
            "cpu": int(round(_latest(m.get("cpu_usage")))),
            "ram": int(round(ram_used / ram_total * 100)) if ram_total else 0,
            "disk": int(round(disk_used / disk_total * 100)) if disk_total else 0,
            "up_d": up_days,
        }
        detail = {
            "p": PAGE_VPS_DETAIL, "ok": True,
            "host": vm.get("hostname", "").encode("ascii", "replace").decode()[:24],
            "ip": vm.get("ip", ""),
            "plan": vm.get("plan", "")[:12],
            # Traffic over the sampled window, MB (the API reports bytes).
            "ni": round(_latest(m.get("incoming_traffic")) / 1e6, 1),
            "no": round(_latest(m.get("outgoing_traffic")) / 1e6, 1),
            "ru": _gb(ram_used), "rt": _gb(ram_total),
            "du": _gb(disk_used), "dt": _gb(disk_total),
            "up_d": up_days,
        }
        return {"metrics": metrics, "detail": detail}
    except Exception as e:
        msg = str(e)[:30]
        return {"metrics": {"p": PAGE_VPS, "ok": False, "err": msg},
                "detail": {"p": PAGE_VPS_DETAIL, "ok": False, "err": msg}}
    finally:
        if close_client:
            await client.aclose()
