# Plan: AI Prompt Router (round-robin front for multiple bridges)

Status: AGREED — written 2026-09-07; decisions in §11 confirmed by Will the same day.

## 1. What we have today

An app never talks to the browser extension directly. It talks to the **HTTP bridge** that
sits in front of the extension (`ai_prompt_api`: either the native host or `start-bridge.sh`).
The "connection" an app is configured with is therefore a triple:

    ip/host  +  HTTP port  +  API key (Bearer)

and the contract is four routes: `POST /prompt`, `GET/DELETE /sessions[/{id}]`, `GET /status`,
`GET /health`. Each browser profile has its own bridge, e.g. on OVH:

| Browser    | HTTP port | WS port |
|------------|-----------|---------|
| Firefox #1 | 8201      | 8760    |
| Chrome     | 8202      | 8761    |
| Firefox #2 | 8203      | 8762    |
| Firefox #3 | 8204      | 8763    |

Facts that shape the design (verified in code):

- `session_id` is minted by the extension (`sess_<base36 time><random>`) and the tab lives in
  **that** browser. A follow-up with a `session_id` only works on the bridge that created it.
- A bridge accepts concurrent prompts (each opens its own tab); there is no queue to respect.
- A pinned `provider` never fails over inside a bridge ("true pin"). The bridge does not report
  which providers are actually logged in; `/status.available_providers` is just the enum.
- Bridge error surface: HTTP 503 when the extension is not connected; otherwise HTTP 200 with
  `success:false` and an `error` string (including `"Timeout after Ns"`).
- A browser extension cannot listen on a TCP port. In the current design the *native host*
  process is what binds the HTTP port; the extension only pushes `configure {httpPort, apiKey}`.
- The CLI bypasses HTTP. `ai-prompt.py` starts a WebSocket **server** on `localhost:8760` and
  waits for the extension to dial in (no handshake message; one client at a time; `prompt` →
  `response` matched by `request_id`). `ai-prompt-native.py` connects to the native host's unix
  socket `/tmp/ai-prompt-native.sock` with 4-byte-length-prefixed JSON (`prompt` with a
  `timeout` field, and `{type:"status"}` → `{type:"status", connected}`).

## 2. What we are building

**`ai-prompt-router`**: a small service that exposes the *identical* HTTP contract and forwards
each call to one of N configured **upstreams** (host, port, key). An upstream is either an
existing bridge or another router — the router cannot tell the difference and does not need to.

    app ──► router (:8300, key R) ──round-robin──► bridge ff1   (:8201, key A)
                                              ├──► bridge chrome (:8202, key B)
                                              └──► router-2     (:8300 on another box, key C)
                                                        └──► bridges …

Callers change only host/port/key to the router's. Nothing else in the app changes.

Form factor (decided): a standalone Python process next to `ai_prompt_api`, configured by a JSON
file, run via `start-router.sh` or systemd on the OVH box. No browser required. (The
"router browser extension + native host" variant in §9 is kept as a possible later add-on.)

The router accepts callers the same three ways the current extension/bridge does ("fronts"):

| Front        | Direction                                  | Who uses it                     | Auth            |
|--------------|--------------------------------------------|---------------------------------|-----------------|
| HTTP REST    | router listens on `listen.port`            | apps (securities, …), routers   | per-app keys    |
| WebSocket    | router **dials out** to `ws://host:port`   | `ai-prompt.py`, standalone bridge `main.py` | none (localhost, same as today) |
| Unix socket  | router listens on `unix_socket.path`       | `ai-prompt-native.py`           | file mode 0600  |

All three feed the same core (`router.forward_prompt()`); the upstream side is always HTTP.
On the WebSocket front the router *impersonates an extension*: it keeps dialling each configured
target with the extension's reconnect cadence (500 ms fast / backoff), handles `prompt`, and
replies `response`. So a standalone bridge started with `start-bridge.sh` can point its WS port
at the router instead of at a browser, and the CLI needs no change at all.

## 3. Package layout

    ai_prompt_router/
      __init__.py
      __main__.py         # python -m ai_prompt_router
      config.py           # load + validate router.json, env overrides (AI_PROMPT_ROUTER_*)
      upstreams.py        # UpstreamClient: httpx client, health state, in-flight counter
      strategy.py         # RoundRobin (interface so least_busy / weighted can be added later)
      sessions.py         # session_id -> upstream owner map, JSON persistence
      router.py           # choose_upstream(), forward_prompt() with failover + hop guard
      auth.py             # Bearer check against the router's per-app keys
      app.py              # FastAPI app: /prompt /sessions /status /health /upstreams
      fronts/
        ws_client.py      # dial-out WebSocket front (extension protocol, auto-reconnect)
        unix_socket.py    # length-prefixed unix-socket front (native-host protocol)
      requirements.txt    # fastapi, uvicorn, httpx, pydantic-settings
      router.example.json
    scripts/start-router.sh          # mirrors start-bridge.sh (--status / --stop)
    ai-prompt-cli/ai-prompt-native.py  # gains --socket / AI_PROMPT_NATIVE_SOCKET (path is hardcoded today)
    scripts/ai-prompt-router.service # systemd unit example for OVH
    tests/test_router_*.py

The API request/response models are **imported from `ai_prompt_api.models`** so the router
cannot drift from the bridge contract.

## 4. Configuration

`router.json` (path from `AI_PROMPT_ROUTER_CONFIG`, default `~/.ai-prompt/router.json`):

```json
{
  "listen": { "host": "0.0.0.0", "port": 8300 },
  "api_keys": [
    { "name": "securities", "key": "…" },
    { "name": "dev-laptop", "key": "…" }
  ],
  "ws_targets": [ { "url": "ws://127.0.0.1:8760" } ],
  "unix_socket": { "path": "/tmp/ai-prompt-router.sock" },
  "strategy": "round_robin",
  "failover": true,
  "max_attempts": 3,
  "health_interval_seconds": 10,
  "max_hops": 4,
  "upstreams": [
    { "name": "ff1",     "url": "http://127.0.0.1:8201", "api_key": "…", "providers": ["chatgpt", "grok"] },
    { "name": "chrome",  "url": "http://127.0.0.1:8202", "api_key": "…" },
    { "name": "ff2",     "url": "http://127.0.0.1:8203", "api_key": "…", "enabled": false },
    { "name": "router-b","url": "http://10.0.0.5:8300",  "api_key": "…" }
  ]
}
```

- `providers` is optional; omitted means "assume every provider". It is only consulted when a
  request pins `provider` (see §5.2). Static because bridges do not report live availability.
- `enabled:false` keeps an upstream in the file but out of rotation (maintenance).
- `api_keys` is a list of **per-app keys**. Any listed key authenticates; the matching `name`
  is logged with every request and echoed as `X-AI-Prompt-Client` so `/upstreams` and logs can
  be read per app. Revoking an app = removing its entry. An empty list = no auth on the HTTP
  front (same rule as the bridge). Upstream keys are per-upstream and never exposed to callers.
- `ws_targets` — WebSocket servers the router dials into as if it were the extension (the CLI's
  port, or a standalone bridge's `WS_PORT`). Omit or leave empty to disable that front.
- `unix_socket.path` — where the unix-socket front listens; defaults to
  `/tmp/ai-prompt-router.sock` (the native host already owns `ai-prompt-native.sock`). Set to
  `null` to disable.
- Env overrides: `AI_PROMPT_ROUTER_CONFIG`, `AI_PROMPT_ROUTER_PORT`.
- Config reload: restart the process (matches the bridge, which has no hot-reload). SIGHUP
  reload is a possible later addition (§10).

## 5. Routing rules

### 5.1 Order of decisions for `POST /prompt`

1. **Hop guard.** Read `X-AI-Prompt-Hops` (default 0). If `>= max_hops` respond 508 Loop
   Detected. Forward with the header incremented, and append this router's instance id to
   `X-AI-Prompt-Via`; if our own id is already in `Via`, respond 508 immediately.
2. **Session affinity.** If `session_id` is present:
   - owner known in the session map → forward to that upstream, no round-robin, no failover
     (the tab lives there; nothing else can continue it).
   - owner unknown (router restarted without its map, or a session created before the router
     existed) → fan out `GET /sessions/{id}` to enabled upstreams; first 200 wins and is recorded.
   - still unknown → treat as a new conversation (this mirrors what the extension itself does
     with an unknown session id: it silently starts a new session).
3. **Eligibility filter** for a new conversation: `enabled` AND currently healthy AND (if
   `provider` is pinned) `provider ∈ upstream.providers`.
4. **Round-robin** over the eligible list. One atomic counter, advanced per selection, indexed
   into the eligible list sorted by config order. (Pure rotation; no weights in v1.)
5. **Forward** with the upstream's key, the caller's body verbatim, `timeout` passed through.
   httpx timeouts: connect 5 s, read = `request.timeout or 120` + 15 s grace so the upstream's own
   `"Timeout after Ns"` message is what the caller sees, not a router timeout.
6. **Record** `session_id → upstream` from a successful response that carries a session id.
7. **Return** the upstream body unchanged. Add response header `X-AI-Prompt-Upstream: <name>`
   so callers/tests can see where it went without changing the JSON contract.

The WebSocket and unix-socket fronts run the same steps 2–7 on the `prompt` message and answer
with the extension's `response` shape (`type`, `request_id`, `success`, `text`, `images`,
`provider`, `model`, `input_tokens`, `output_tokens`, `session_id`, `error`). `timeout` comes
from the message when present (unix socket) or the router default (WebSocket). Hop headers are
HTTP-only; those fronts are entry points, never chained.

### 5.2 Failover (decided: on by default)

Retry on the *next* eligible upstream only when we are certain the prompt was never executed:

| Upstream outcome                                        | Retry? | Why                                  |
|---------------------------------------------------------|--------|--------------------------------------|
| connection refused / reset / DNS failure                | yes    | never reached the bridge             |
| HTTP 503 (extension not connected)                      | yes    | bridge refused before sending        |
| HTTP 401/403                                            | yes*   | our key for that upstream is wrong; mark upstream `misconfigured`, retry elsewhere |
| 200 `success:false`, error contains `Extension not connected` | yes | bridge lost the extension before sending |
| 200 `success:false`, error contains `not available`     | yes    | pinned provider missing on that browser; nothing sent |
| 200 `success:false`, error `Timeout after …`            | **no** | prompt may still be running there; a retry would double-send |
| 200 `success:false`, any other error                    | **no** | provider ran and failed; return as-is |
| 200 `success:true`                                      | —      | done                                 |

Bounded by `max_attempts`. Requests with a known session owner never fail over. A pinned
`provider` request may fail over to another *upstream* that lists that provider; this preserves
the bridge's "true pin" semantics (the provider never changes, only the browser).

### 5.3 Health

- Background task polls `GET /health` on every enabled upstream every `health_interval_seconds`.
  Healthy = HTTP 200 and `extension_connected: true`. Chained routers compose naturally because
  a router's `/health` reports `extension_connected = any upstream healthy`.
- Passive marking: a transport failure during a real request marks the upstream unhealthy
  immediately and schedules a recheck in 2 s.
- An upstream that is unhealthy is skipped by the rotation; when it recovers it rejoins at its
  config position. If *no* upstream is eligible, `/prompt` returns 503 with the same
  `"Browser extension not connected"` detail the bridge uses, so existing callers handle it.

### 5.4 Sessions endpoints

- `GET /sessions` — fan out to healthy upstreams, merge lists, dedupe by id. (Upstream session
  lists are in-memory per bridge process, so this is only as complete as the bridges' own lists.)
- `GET /sessions/{id}` — owner from map, else fan-out; 404 if nobody has it.
- `DELETE /sessions/{id}` — same resolution; on success drop from the map.

### 5.5 Status / health / visibility

- `GET /health` → `{status:"ok", extension_connected: <any healthy>}`.
- `GET /status` → `extension_connected`, `pending_requests` = sum of in-flight, `active_sessions`
  = size of the session map, `available_providers` = union of configured provider lists.
- `GET /upstreams` (router-only, requires key) → per upstream: name, url, enabled, healthy,
  last_check, last_error, in_flight, sessions_owned; plus `fronts`: each WS target's connection
  state and the unix-socket path. This is the operator's dashboard.
- Unix-socket `{type:"status"}` → `connected` = any upstream healthy (what `ai-prompt-native.py
  --status` prints).

### 5.6 Persistence

`~/.ai-prompt/router-sessions.json` — `{session_id: {upstream, provider, created_at}}`, written
on change (atomic rename). Needed because the extension persists sessions in browser storage,
so they outlive a router restart. Entries are pruned on DELETE, on a 404 from the owner, and by
age (default 24 h, configurable).

## 6. Security / network

- The HTTP front uses **per-app keys** (§4); upstream keys live only in `router.json` (mode 600).
  The WebSocket and unix-socket fronts carry no key, exactly like today: the WS front only ever
  connects to targets *we* configured, and the socket file is 0600.
- Deployment (decided): the router runs on the OVH box beside the bridges. Open only the router
  port through HestiaCP (`v-add-firewall-rule ACCEPT 0.0.0.0/0 8300 TCP "ai-prompt router"`), point
  the apps at `cp.cholds.com:8300`, then restrict 8201–8204 to `127.0.0.1` (bind change in
  `start-bridge.sh`, or a source-IP rule). Today those ports are public and, on the standalone
  bridge, unauthenticated; the router is where that gets fixed.
- Attachments are base64 inside JSON and are passed through untouched; set the router's request
  body limit to match what the bridges accept (no streaming needed).

## 7. Testing

- **Unit**: eligibility + rotation order, pin filtering, session affinity precedence, failover
  table (each row), hop guard, session-map persistence/pruning.
- **Integration** (pytest + httpx ASGI transport, no browser): start two fake upstreams built from
  the real `ai_prompt_api` routers with a stub `extension_manager`, plus one router; assert
  alternation via `X-AI-Prompt-Upstream`, affinity on follow-up, failover when one stub reports
  disconnected, and a router→router chain (three-level) including loop detection.
- **Contract**: extend `tests/test_prompt_request_contract.py` so the router accepts exactly the
  same `PromptRequest` fields and returns `PromptResponse`.
- **Fronts**: a test WebSocket server (same shape as `ai-prompt.py`'s) that the router dials into,
  asserting `prompt` → `response` round-trip and reconnect after the server restarts; a unix-socket
  test using the real `ai-prompt-native.py` framing for `prompt` and `status`.
- **Manual**: `curl` the router twice; watch `X-AI-Prompt-Upstream` alternate; `GET /upstreams`.

## 8. Delivery phases

1. **Core** — package skeleton, config loading, `/prompt` round-robin with hop guard, `/health`,
   `/status`, router auth, `X-AI-Prompt-Upstream`. Unit tests.
2. **Sessions** — affinity, persistence, fan-out resolution, `/sessions` routes.
3. **Resilience** — health poller, passive marking, failover table, `/upstreams`.
4. **CLI fronts** — WebSocket dial-out front, unix-socket front, `ai-prompt-native.py --socket`.
5. **Ops** — `start-router.sh`, systemd unit, README section + `router.example.json`, note in the
   popup Port Map table, OVH firewall step and 8201–8204 lock-down. Integration tests.
6. **Optional follow-ons** — see §10.

## 9. Later alternative: "router" browser extension + native host

Not in v1 (decided). If the upstream list should one day be managed from a popup like today's bridges:

- A second extension (`ai-prompt-router-extension/`) whose popup stores `upstreams[]`,
  `httpPort`, `apiKey` and pushes them to a native host via `configure`, exactly as the current
  extension does with its port/key.
- The native host would be the same `ai_prompt_router` package from §3, started by the browser
  through a second native-messaging manifest (`ai_prompt_router` instead of `ai_prompt_api`).
- Trade-offs: a browser must be open for the router to exist; the extension needs no host
  permissions and no content scripts (it is purely a config surface); two things to install
  instead of one. The routing logic, tests, and API are identical, so this can be added later
  without redoing §5.

## 10. Later options (not in v1)

- `strategy: least_busy` (lowest in-flight) or `weighted` round-robin.
- Live provider availability: have the extension push its enabled/logged-in providers to the
  bridge in `configure`, expose them in `/status`, and let the router use that instead of the
  static `providers` list.
- SIGHUP / file-watch config reload; `POST /upstreams/{name}/enable|disable` admin routes.
- Per-key policy (restrict an app key to certain upstreams or providers).

## 11. Decisions (confirmed 2026-09-07)

| # | Question | Decision |
|---|----------|----------|
| 1 | Form factor | Standalone service with a JSON config (§2). Extension variant deferred (§9). |
| 2 | Callers | HTTP API **and** the CLI paths: WebSocket dial-out + unix socket, mirroring how the extension takes connections today (§2 fronts). |
| 3 | Failover | Try the next upstream on the "never executed" cases in §5.2. |
| 4 | Pinned providers | Static per-upstream `providers` list in the config for v1. |
| 5 | Session affinity | Follow-ups with a `session_id` always go to the creating upstream; rotation only for new conversations. |
| 6 | Location | On the OVH box beside the bridges; open the router port, then close 8201–8204 to the public (§6). |
| 7 | Keys | Per-app keys (`api_keys` list, §4). |
