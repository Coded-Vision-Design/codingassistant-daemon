#!/usr/bin/env python3
import json
import pytest
from daemon.pc_stats import PAGE_PC, MAX_PC_PAYLOAD_BYTES, fetch_pc_stats, get_nvidia_stats

def test_pc_stats_structure_and_bounds():
    stats = fetch_pc_stats()
    assert stats["p"] == PAGE_PC
    assert stats["ok"] is True
    assert "cpu" in stats
    assert "ram" in stats
    assert "disk" in stats
    
    # Assert CPU temp is NOT present (rule from plan: CPU temp omitted on Windows)
    assert "cput" not in stats
    
    # Verify wire size bounds
    raw = json.dumps(stats, separators=(",", ":")).encode()
    assert len(raw) <= MAX_PC_PAYLOAD_BYTES
    print(f"PC stats wire size: {len(raw)} bytes (budget: {MAX_PC_PAYLOAD_BYTES} bytes)")

def test_nvidia_stats_graceful_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    gpu = get_nvidia_stats()
    assert gpu == {}
