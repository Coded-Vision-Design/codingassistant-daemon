#!/usr/bin/env python3
"""Google Drive storage quota reader for the CodingAssistant daemon (Page 5).

Reads Google Drive storageQuota via about.get using an OAuth 2.0 refresh token.
Operates via SourceCache off the main BLE poll path to ensure non-blocking,
zero-risk operation.

Payload format (under 240 bytes):
{
  "p": 5,
  "ok": true,
  "used": 15234567890,      # bytes used in total
  "limit": 107374182400,    # total quota bytes (e.g. 100 GB)
  "trash": 523456789,       # bytes in trash
  "drive": 12234567890      # bytes used specifically in drive
}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

PAGE_DRIVE = 5
MAX_GDRIVE_PAYLOAD_BYTES = 240

def get_credentials_path() -> Path:
    """Location of Google Drive OAuth credentials."""
    override = os.environ.get("GDRIVE_CREDENTIALS_PATH")
    if override:
        return Path(override)
    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_appdata / "CodingAssistant" / "gdrive_credentials.json"


def load_credentials() -> Optional[Dict[str, Any]]:
    """Load saved OAuth credentials if they exist."""
    path = get_credentials_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "refresh_token" in data and "client_id" in data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


async def refresh_access_token(creds: Dict[str, Any], client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
    """Exchange refresh token for an access token."""
    url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": creds["client_id"],
        "client_secret": creds.get("client_secret", ""),
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }
    
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)
        close_client = True
        
    try:
        resp = await client.post(url, data=payload)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("access_token")
    except Exception:
        pass
    finally:
        if close_client:
            await client.aclose()
    return None


async def fetch_gdrive_quota(client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Fetch Google Drive quota. Returns structured payload or { 'p': 5, 'ok': False }."""
    creds = load_credentials()
    if not creds:
        return {"p": PAGE_DRIVE, "ok": False, "err": "no_creds"}

    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)
        close_client = True

    try:
        access_token = await refresh_access_token(creds, client)
        if not access_token:
            return {"p": PAGE_DRIVE, "ok": False, "err": "auth_failed"}

        url = "https://www.googleapis.com/drive/v3/about?fields=storageQuota"
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = await client.get(url, headers=headers)
        
        if resp.status_code != 200:
            return {"p": PAGE_DRIVE, "ok": False, "err": f"http_{resp.status_code}"}

        quota = resp.json().get("storageQuota", {})
        
        # Parse integers safely
        used = int(quota.get("usage", 0))
        limit = int(quota.get("limit", 0)) if "limit" in quota else 0
        trash = int(quota.get("usageInDriveTrash", 0))
        drive = int(quota.get("usageInDrive", 0))

        payload = {
            "p": PAGE_DRIVE,
            "ok": True,
            "used": used,
            "limit": limit,
            "trash": trash,
            "drive": drive,
        }
        
        # Verify wire size bound
        raw = json.dumps(payload, separators=(",", ":")).encode()
        if len(raw) > MAX_GDRIVE_PAYLOAD_BYTES:
            # Fallback to MB/GB rounded if somehow too large
            payload["used"] = used // (1024 * 1024)
            payload["limit"] = limit // (1024 * 1024)
            payload["unit"] = "MB"

        return payload

    except Exception as e:
        return {"p": PAGE_DRIVE, "ok": False, "err": str(e)[:30]}
    finally:
        if close_client:
            await client.aclose()
