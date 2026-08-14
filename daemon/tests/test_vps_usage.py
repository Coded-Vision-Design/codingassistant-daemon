#!/usr/bin/env python3
"""Tests for daemon/vps_usage.py against the real Hostinger API shapes.

Response fixtures mirror live captures from developers.hostinger.com
(2026-08-13): /virtual-machines is a list of VMs with memory/disk in MB,
/docker is a list of compose projects with containers, /metrics is a set of
time series keyed by unix timestamp.
"""
import asyncio
import json

import httpx
import pytest

import daemon.vps_usage as vps
from daemon.vps_usage import (
    MAX_PAYLOAD_BYTES,
    PAGE_DOCKER,
    PAGE_VPS,
    fetch_docker_summary,
    fetch_vps_metrics,
)


@pytest.fixture(autouse=True)
def _fresh_vm_cache(monkeypatch):
    monkeypatch.setattr(vps, "_vm", {})


def test_missing_vps_token_returns_ok_false(monkeypatch):
    monkeypatch.delenv("HOSTINGER_API_TOKEN", raising=False)
    monkeypatch.setattr(vps, "get_hostinger_token", lambda: None)
    res_d = asyncio.run(fetch_docker_summary())
    assert res_d["p"] == PAGE_DOCKER
    assert res_d["ok"] is False
    assert res_d["err"] == "no_token"

    res_v = asyncio.run(fetch_vps_metrics())
    assert res_v["p"] == PAGE_VPS
    assert res_v["ok"] is False
    assert res_v["err"] == "no_token"


def _mock_transport(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/virtual-machines"):
        return httpx.Response(200, json=[
            {"id": 424242, "state": "running", "memory": 8192, "disk": 102400,
             "hostname": "srv424242.example.host", "plan": "KVM 2",
             "ipv4": [{"address": "192.0.2.10"}]},
        ])
    if url.endswith("/docker"):
        return httpx.Response(200, json=[
            {"name": "prod", "containers": [
                {"name": "web_proxy", "state": "running", "health": "healthy"},
                {"name": "db_postgres", "state": "running", "health": ""},
            ]},
            {"name": "staging", "containers": [
                {"name": "cache_redis", "state": "exited", "health": ""},
                {"name": "a-very-long-container-name-indeed", "state": "running",
                 "health": "unhealthy"},
            ]},
        ])
    if "/metrics" in url:
        return httpx.Response(200, json={
            "cpu_usage": {"unit": "%", "usage": {"100": 10.0, "200": 22.4}},
            "ram_usage": {"unit": "bytes", "usage": {"200": 8192 * 1024 * 1024 * 0.64}},
            "disk_space": {"unit": "bytes", "usage": {"200": 102400 * 1024 * 1024 * 0.48}},
            "uptime": {"unit": "seconds", "usage": {"200": 2592000}},
        })
    return httpx.Response(404)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_mock_transport))


def test_docker_summary_counts_and_names_the_sick(monkeypatch):
    monkeypatch.setenv("HOSTINGER_API_TOKEN", "fake_token")
    res = asyncio.run(fetch_docker_summary(client=_client()))
    assert res["p"] == PAGE_DOCKER
    assert res["ok"] is True
    assert res["total"] == 4
    assert res["up"] == 2                     # exited and unhealthy both count down
    assert res["proj"] == 2
    assert res["down"][0] == "cache_redis"
    # Long names are clipped with ASCII dots - the device fonts are ASCII-only
    # and DOCKER_NAME_COLS holds 19 chars + NUL.
    assert res["down"][1] == "a-very-long-conta.."
    assert all(ord(ch) < 128 for name in res["down"] for ch in name)
    raw = json.dumps(res, separators=(",", ":")).encode()
    assert len(raw) <= MAX_PAYLOAD_BYTES


def test_vps_metrics_latest_sample_and_percentages(monkeypatch):
    monkeypatch.setenv("HOSTINGER_API_TOKEN", "fake_token")
    res = asyncio.run(fetch_vps_metrics(client=_client()))
    assert res["p"] == PAGE_VPS
    assert res["ok"] is True
    assert res["cpu"] == 22                   # newest sample wins, not the first
    assert res["ram"] == 64                   # bytes vs the VM's 8192 MB total
    assert res["disk"] == 48                  # bytes vs the VM's 102400 MB total
    assert res["up_d"] == 30
    raw = json.dumps(res, separators=(",", ":")).encode()
    assert len(raw) <= MAX_PAYLOAD_BYTES


def test_transient_http_error_is_distinct_from_no_vm(monkeypatch):
    """A 503 must NOT read as 'this account has no VPS' - the firmware prunes
    on no_vm but keeps the page for transients."""
    monkeypatch.setenv("HOSTINGER_API_TOKEN", "fake_token")

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    res = asyncio.run(fetch_docker_summary(client=client))
    assert res["ok"] is False
    assert res["err"] == "http_503"


def test_bad_token_is_auth_failed_not_no_vm(monkeypatch):
    """401 means the token is wrong - surface it as auth, so the page prunes
    AND the log says what to fix, instead of masquerading as an empty account."""
    monkeypatch.setenv("HOSTINGER_API_TOKEN", "revoked_token")

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = httpx.AsyncClient(transport=httpx.MockTransport(unauthorized))
    res = asyncio.run(fetch_vps_metrics(client=client))
    assert res["ok"] is False
    assert res["err"] == "auth_failed"


def test_empty_account_is_no_vm(monkeypatch):
    monkeypatch.setenv("HOSTINGER_API_TOKEN", "fake_token")

    def empty(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/virtual-machines"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
    res = asyncio.run(fetch_docker_summary(client=client))
    assert res["ok"] is False
    assert res["err"] == "no_vm"
