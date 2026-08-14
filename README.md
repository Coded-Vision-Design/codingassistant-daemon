# codingassistant-daemon

![tests](https://github.com/Coded-Vision-Design/codingassistant-daemon/actions/workflows/ci.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)

Pluggable **data sources for AI/infra desk dashboards**, by
[Coded Vision Design](https://codedvisiondesign.co.uk). Each module reads one
thing a developer wants at a glance and emits a compact JSON page (≤240
bytes) sized for constrained displays and low-bandwidth links such as BLE:

| Module | Page it produces | Credentials |
| --- | --- | --- |
| `codex_usage` | OpenAI Codex weekly usage, plan, reading age — read from Codex's own session logs on disk | none (no network at all) |
| `github_stats` | Contribution heatmap (52 weeks, one digit per day), open PRs split human/bot, review requests, Dependabot / secret-scanning / code-scanning counts, worst-offender rows, oldest-PR rows | the `gh` CLI's token, read per refresh, never stored |
| `vps_usage` | Docker container health summary + per-project rows; VPS CPU/RAM/disk/uptime + hostname/IP/plan/traffic detail (Hostinger API) | `HOSTINGER_API_TOKEN` |
| `pc_stats` | Local CPU / RAM / disk, GPU utilisation / VRAM / temperature | none (psutil + nvidia-smi) |
| `gdrive_usage` | Google Drive storage quota | OAuth refresh token via `setup_gdrive_oauth.py` (scope: metadata read-only) |
| `source_cache` | The harness: every source runs on its own cadence into a last-known-good cache with timeouts and back-off, so a hung source can never stall your event loop | — |

## Design contract

- **Never block, never raise.** Every fetcher returns a dict; failures are
  `{"ok": false, "err": "..."}` with distinct codes for *never-configured*
  (`no_token`, `no_creds`, `no_org`, `auth_failed`, `no_vm`) versus
  *transient* (`http_NNN`), so a consumer can prune dead sources and retry
  live ones.
- **Wire-budgeted.** Pages self-truncate to stay under 240 bytes; tests
  assert it.
- **Secrets never cross the wire.** The GitHub secret-scanning parser
  whitelists repo + type and drops everything else; sentinel tests prove a
  committed secret can never survive into a payload.
- **Honest staleness.** Log-derived readings carry their age; a rolled-over
  window reports "no trustworthy figure" rather than a stale one.

## Quickstart

```bash
pip install httpx psutil
python -m pytest daemon/tests -q     # 49 tests, no credentials needed

# then, in your dashboard/daemon:
from daemon.source_cache import SourceCache, SourceRegistry
from daemon.vps_usage import fetch_vps_pages

reg = SourceRegistry()
reg.add(SourceCache("vps", fetch_vps_pages, interval=60, timeout=15))
reg.start_all(stop_event)            # asyncio; sync fetchers run in threads
page = reg.value("vps", max_age=900)["metrics"]
```

Configuration lives in `%LOCALAPPDATA%\CodingAssistant\config` (or env
vars): `org=` for GitHub, `hostinger_token=` for Docker/VPS; Google Drive
runs `python daemon/setup_gdrive_oauth.py` once from a real terminal.

## Provenance

These modules were built for CodingAssistant, Coded Vision Design's ESP32
desk dashboard. Every line in this repository is Coded Vision Design's own
work, published under MIT. (The dashboard firmware builds on third-party
code and assets and is a separate, private project.)
