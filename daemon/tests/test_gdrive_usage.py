#!/usr/bin/env python3
import asyncio
import json
import pytest
import httpx

from daemon.gdrive_usage import (
    PAGE_DRIVE,
    MAX_GDRIVE_PAYLOAD_BYTES,
    fetch_gdrive_quota,
    get_credentials_path,
)

def test_missing_credentials_returns_ok_false(tmp_path, monkeypatch):
    monkeypatch.setenv("GDRIVE_CREDENTIALS_PATH", str(tmp_path / "nonexistent.json"))
    res = asyncio.run(fetch_gdrive_quota())
    assert res["p"] == PAGE_DRIVE
    assert res["ok"] is False
    assert res["err"] == "no_creds"

def test_valid_quota_payload_fits_wire_bounds(tmp_path, monkeypatch):
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps({
        "client_id": "test_client",
        "client_secret": "test_secret",
        "refresh_token": "test_refresh"
    }))
    monkeypatch.setenv("GDRIVE_CREDENTIALS_PATH", str(creds_file))

    # Mock responses
    def custom_transport(request: httpx.Request) -> httpx.Response:
        if "oauth2.googleapis.com/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "fake_access_token"})
        elif "about" in str(request.url):
            return httpx.Response(200, json={
                "storageQuota": {
                    "limit": "107374182400",
                    "usage": "21474836480",
                    "usageInDrive": "18000000000",
                    "usageInDriveTrash": "500000000"
                }
            })
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(custom_transport))
    res = asyncio.run(fetch_gdrive_quota(client=client))
    
    assert res["p"] == PAGE_DRIVE
    assert res["ok"] is True
    assert res["limit"] == 107374182400
    assert res["used"] == 21474836480
    assert res["trash"] == 500000000
    
    encoded = json.dumps(res, separators=(",", ":")).encode()
    assert len(encoded) <= MAX_GDRIVE_PAYLOAD_BYTES
