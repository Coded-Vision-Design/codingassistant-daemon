#!/usr/bin/env python3
"""PC system stats reader for the CodingAssistant daemon (Page 4).

Collects:
- CPU utilization % (via psutil)
- RAM usage % and used/total GB (via psutil)
- Disk usage % (via psutil)
- GPU utilization %, VRAM %, and GPU temperature (via nvidia-smi with CREATE_NO_WINDOW)

Rule from plan:
- CPU temperature is omitted (not faked): psutil on Windows does not support CPU temp
  without kernel drivers.

Payload format (under 240 bytes):
{
  "p": 4,
  "ok": true,
  "cpu": 24,         # CPU %
  "ram": 58,         # RAM %
  "ram_u": 18.5,     # RAM used GB
  "ram_t": 32.0,     # RAM total GB
  "disk": 65,        # Primary disk %
  "gpu": 35,         # GPU % (optional, if nvidia GPU present)
  "vram": 42,        # VRAM % (optional)
  "gput": 54         # GPU temp C (optional)
}
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

import psutil

PAGE_PC = 4
MAX_PC_PAYLOAD_BYTES = 240

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

def get_nvidia_stats() -> Dict[str, Any]:
    """Query nvidia-smi with CREATE_NO_WINDOW to avoid flashing console windows."""
    nvsmi = shutil.which("nvidia-smi")
    if not nvsmi:
        return {}
    
    cmd = [
        nvsmi,
        "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits"
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3.0,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout.strip():
            # e.g. "12, 4500, 12288, 48"
            line = res.stdout.strip().split("\n")[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                gpu_pct = int(parts[0])
                vram_used = float(parts[1])
                vram_total = float(parts[2])
                gpu_temp = int(parts[3])
                vram_pct = int(round((vram_used / max(1.0, vram_total)) * 100))
                return {
                    "gpu": gpu_pct,
                    "vram": vram_pct,
                    "gput": gpu_temp,
                }
    except Exception:
        pass
    return {}

def fetch_pc_stats() -> Dict[str, Any]:
    """Read PC telemetry. Never blocks, never raises."""
    try:
        cpu_pct = int(round(psutil.cpu_percent(interval=None)))
        mem = psutil.virtual_memory()
        ram_pct = int(round(mem.percent))
        ram_used_gb = round(mem.used / (1024 ** 3), 1)
        ram_total_gb = round(mem.total / (1024 ** 3), 1)
        
        # Primary disk (C: on Windows, / on Unix)
        disk_root = "C:\\" if os.name == "nt" else "/"
        disk = psutil.disk_usage(disk_root)
        disk_pct = int(round(disk.percent))

        payload: Dict[str, Any] = {
            "p": PAGE_PC,
            "ok": True,
            "cpu": cpu_pct,
            "ram": ram_pct,
            "ram_u": ram_used_gb,
            "ram_t": ram_total_gb,
            "disk": disk_pct,
        }

        # Enrich with NVIDIA GPU telemetry if available
        gpu_stats = get_nvidia_stats()
        payload.update(gpu_stats)

        # Wire size guard verification
        raw = json.dumps(payload, separators=(",", ":")).encode()
        if len(raw) > MAX_PC_PAYLOAD_BYTES:
            # Fallback if too large
            payload.pop("ram_u", None)
            payload.pop("ram_t", None)

        return payload
    except Exception as e:
        return {"p": PAGE_PC, "ok": False, "err": str(e)[:30]}
