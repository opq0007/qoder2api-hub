# AGENTS.md — Qoder2API-Hub

## What this repo is

A zero-dependency Python gateway that wraps Qoder's native COSY-signed protocol
(CN realm `qoder.com.cn` / Intl realm `qoder.com`) into an OpenAI-compatible API
(Chat Completions + Responses API), with a multi-account pool, daily check-in,
scheduler, and a single-file web dashboard. Sibling project of WorkBuddy2API-Hub
(same architecture, different upstream protocol). **`README.md` (Chinese) is the
authoritative doc** — its §六「开发与测试」has the module map; read the relevant
section before touching an area.

## Hard constraints

- **Zero external pip dependencies.** AES-128/256+GCM, RSA, DPAPI, QMC decryption,
  and COSY signing are pure-stdlib implementations in `qoder_sign.py`. Never add
  a dependency — the Docker image is `python:3.11-alpine` (no compiler) and the
  Windows launcher may use a bundled portable runtime. Requires Python 3.9+.
- **Never commit or print `accounts/` contents** — one plaintext-token JSON per
  account, plus `accounts/settings.json` (API keys, PBKDF2 panel password).
  `accounts/` and `usage/` are runtime data dirs (gitignored, Docker volumes).
- **Dual-realm correctness.** CN (`gateway.qoder.com.cn`) and Intl
  (`api1.qoder.sh`, fallback `api2`/`api3`) have different model sets, hosts, and
  OAuth URL params. Realm-exclusive models must route to their own exit; a Key
  bound to one exit receiving the other realm's model must fail with a plain-400.
- **Retry policy is deliberate.** Client parameter errors and upstream content-policy
  rejections (`DataInspectionFailed`) must fail fast — never retry. Transient
  transport errors retry 2x same-account, plus in-stream `418` envelope retry
  (only before any upstream byte is written to the client).
- **Model catalog fidelity.** Runtime priority: dynamic `/algo/api/v2/model/list`
  (GET must carry a `{}` body matching the COSY signature, else 403) > local
  client catalog cache > `qoder_catalog_{intl,cn}.json` snapshots > embedded
  frozen copy in `qoder_catalog.py`. Entries are field-for-field official
  (`display_name` = the id clients must use); never invent fields (e.g. no
  fabricated max-output). `enable=false` entries are shown with the official
  disabled reason, not filtered.
- **Fingerprint stability.** `qoder_fingerprint.derive_id` derives the machine
  identity deterministically from the account UID — the same account must always
  map to the same virtual physical device. Don't change the derivation.
- **Protocol constants track the official desktop client** (currently 0.4.3:
  `cosy-version: 1.1.64`, realm hosts, endpoint paths). When bumping, verify
  against the client and note it in the changelog.
- **Account IP proxy is fail-closed.** When an account has a `proxy`, all of its
  gateway-side traffic must go through it (`http/https/socks5/socks5h`); a tunnel
  failure must fail the request and rotate, **never fall back to a direct
  connection**, and must skip local DNS resolution (`validate_public_http_url(..., resolve=False)`).
  Proxy accounts also skip the native `runtime-info.exe` bridge (it leaks the host
  IP). Never log/return proxy credentials — use `qoder_net.mask_proxy`. 5
  consecutive failures auto-pause the account.

## Layout

| File | Role |
|---|---|
| `qoder_proxy.py` | Main gateway (~5.6k lines): HTTP routing, COSY data plane, dual-protocol conversion, usage stats, dashboard auth. `VERSION` constant lives here. |
| `qoder_net.py` | Account IP proxy: proxy-URL parse/mask, pure-stdlib HTTP CONNECT + SOCKS5 tunnels, single-exit `urllib` opener factory |
| `qoder_sign.py` | Custom Base64, crypto primitives, COSY signing, credential decryption |
| `qoder_accounts.py` | Dual-realm account pool, OAuth device flow, PAT, token lifecycle (`dt-/jt-/drt-/jrt-/pt-`), local credential scan (DPAPI desktop app / CLI), per-account proxy fields + circuit breaker |
| `qoder_catalog.py` + `qoder_catalog_{intl,cn}.json` | Model snapshots, aliases, realm-exclusivity |
| `qoder_tasks.py` / `qoder_scheduler.py` | Check-in & Pro-benefit claims; hourly scheduler (09/21 check-in, 22:00 keep-alive) |
| `qoder_settings.py` / `qoder_fingerprint.py` | Panel password + multi API keys w/ exit binding; per-UID device fingerprint |
| `baseprompt.json` | Official inference request body template |
| `dashboard.html` | Single-file dashboard (zh-CN, inline CSS/JS, no build step), served from disk next to `qoder_proxy.py` |
| `_*.py` | Tooling scripts (see commands below), not part of the runtime |

## Commands

```bash
python _test_qoder.py                                # offline deterministic tests (342 assertions, no network)
python qoder_proxy.py --port 8790                    # run gateway (default 8790)
python _diag_gateway.py [--chat]                     # liveness/stream self-check (exit 0=ok 1=failed 2=unreachable)
python _diag_campaign.py                             # check-in/campaign diagnostics, Chinese output, read-only
python _refresh_catalog.py [--dry-run]               # re-export model snapshot JSONs after client updates
python _verify_models.py --base http://127.0.0.1:8790 # e2e catalog verification vs official live data (needs running gateway)
```

- Tests must stay offline & deterministic: they set `ACCOUNTS_DIR`/`USAGE_DIR` to
  temp dirs and `QD_NATIVE_IDENTITY=0`. New logic → add `check()` assertions there.
- Start scripts: `start-qoder-proxy.bat [port]`, `start-qoder-proxy-lan.bat [port] [key]`,
  `allow-firewall.bat` (firewall). Docker: `docker compose up -d`.

## Conventions & gotchas

- Comments, docstrings, log lines, and user-facing messages are largely **Chinese**;
  keep that style. The dashboard is zh-CN.
- **`.bat` files must stay ASCII-only** (parsed with the console code page) and
  must not put `%VAR%` inside `if( ... )` blocks — use `if/errorlevel/goto`.
- Env vars use the `QD_` prefix (`QD_SSE_HEARTBEAT`, `QD_PROXY_DEFAULT_REALM`,
  `QD_NATIVE_IDENTITY`, `QD_MAX_PAYLOAD_BYTES`, …). Config dirs: `ACCOUNTS_DIR`,
  `USAGE_DIR` / `QD_PROXY_USAGE_DIR`.
- SSE streaming is chunked HTTP/1.1 with `: ping` heartbeats every 5s (upstream
  first token can take 40–71s) and ends with a proper `0\r\n\r\n`; never send
  `Connection: close` on streamed responses (breaks keep-alive clients).
- Upstream wraps provider faults as `418/5xx + provider_error`; access logs may
  show HTTP 200 while the SSE envelope carries the real `statusCodeValue`.
- Version bumps: update `VERSION` in `qoder_proxy.py` **and** the README badge
  (and add a changelog section); releases are git-tagged `vX.Y.Z`.
- Probe endpoints `/ping`, `/healthz`, `/livez`, `/readyz` require **no** auth —
  keep them auth-free; full state is `GET /health`.
