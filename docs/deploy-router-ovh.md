# Deploying the ai-prompt router on the OVH box

Runbook for plan §6 of [plans/ai-prompt-router.md](plans/ai-prompt-router.md): put the router in
front of the four bridges, make **8300 the only public ai-prompt port**, and close 8201–8204.

Box: `ssh ovh` → `cp.cholds.com` (192.99.45.69), user `debian`, passwordless sudo. Shared
**HestiaCP** production box (also mailcow, fail2ban, Docker) — the firewall and any web reload are
shared blast radius. Repo lives at `/home/debian/projects/ai-prompt`.

Everything below is reversible; §9 is the rollback. Do it in one sitting — the dangerous state is
"bridges closed, router not yet proven".

---

## 1. State of the box (verified read-only, 2026-09-07)

| Bridge     | HTTP (public today) | WS (127.0.0.1 only) | Process |
|------------|---------------------|---------------------|---------|
| Firefox #1 | 8201                | 8760                | uvicorn from `venv/` (older venv) |
| Chrome     | 8202                | 8761                | uvicorn from `venv/` |
| Firefox #2 | 8203                | 8762                | uvicorn from `ai_prompt_api/.venv` |
| Firefox #3 | 8204                | 8763                | uvicorn from `ai_prompt_api/.venv` |

All four are `ai_prompt_api.main:app` started by `scripts/start-bridge.sh` as `debian`, bound
`0.0.0.0` (IPv4 only). They **do** require a Bearer key: `GET /sessions` on 8201 and 8202 returns
401 from off-box, so the "unauthenticated to the internet" note in older handover notes is stale.

Three facts that change how you sequence this. Re-verify each before you start:

1. **Hestia's stored rules and the live chain disagree.** `v-list-firewall` lists rules 13–16 =
   8201, 8202, 8203, 8204 open to `0.0.0.0/0`, but the running chain only has ACCEPT rules for
   8201 and 8202. From off-box, 8201/8202 answer 200 and 8203/8204 time out — Firefox #2 and #3
   have been publicly unreachable while looking healthy on the box.
   **Hazard:** `v-add-firewall-rule` rebuilds the whole chain from `rules.conf`, so *adding the
   8300 rule re-opens 8203 and 8204 to the internet* as a side effect. Delete those two rules in
   the same maintenance window (step 8), or knowingly accept the exposure until you do.
2. **Loopback is accepted by source** (`-A INPUT -s 127.0.0.1/32 -j ACCEPT`, policy `INPUT DROP`).
   Removing the per-port ACCEPT rules therefore does **not** break router → bridge over
   `127.0.0.1`. Confirm the rule is still there before you delete anything:
   ```bash
   ssh ovh 'sudo iptables -S INPUT | grep -E "^-P INPUT|127\.0\.0\.1/32"'
   ```
3. **IPv6 is unfiltered.** The box has a global v6 address and `ip6tables -P INPUT ACCEPT` with no
   port rules; Hestia's rule only covers IPv4. The bridges bind IPv4 only, so nothing is exposed
   over v6 today. Keep the router's `listen.host` as `"0.0.0.0"`. If you ever set it to `"::"`,
   port 8300 becomes world-reachable over IPv6 **regardless of the Hestia rule**, and closing
   8201–8204 buys you nothing. (`cp.cholds.com` has no AAAA record, so clients use IPv4 anyway.)

Also note `-A INPUT -s 192.99.45.69/32 -j ACCEPT` — the box's own public IP. Traffic that leaves
and re-enters via the public IP bypasses every port rule. Local **Docker containers** do not: they
arrive from a `172.x` address and reach the bridges today only through the `0.0.0.0/0` port rule,
so a container POSTing to `host:8201` will break at step 8 and must be repointed to the router.

---

## 2. Preflight (read-only — record this before changing anything)

```bash
ssh ovh 'sudo ss -lntp | grep -E ":(820[1-4]|8300|876[0-3])\b"'
ssh ovh 'sudo /usr/local/hestia/bin/v-list-firewall'          # keep this output for rollback
ssh ovh 'sudo iptables -S INPUT'                              # what is ACTUALLY applied
for p in 8201 8202 8203 8204; do
  printf '%s -> ' "$p"
  curl -s -4 --max-time 6 -o /dev/null -w '%{http_code}\n' "http://cp.cholds.com:$p/health"
done
```

Write down which bridges answer `200` and which report `"extension_connected": true` — those are
the only ones worth putting in the rotation, and they are your before/after comparison.

## 3. Install the router on the box

```bash
ssh ovh
cd /home/debian/projects/ai-prompt
git pull
python3 -m venv ai_prompt_router/.venv
ai_prompt_router/.venv/bin/pip install -r ai_prompt_router/requirements.txt
```

The bridges keep running throughout; nothing here touches them.

## 4. Write `router.json`

```bash
mkdir -p ~/.ai-prompt
cp ai_prompt_router/router.example.json ~/.ai-prompt/router.json
chmod 600 ~/.ai-prompt/router.json          # it holds every bridge's key
$EDITOR ~/.ai-prompt/router.json
```

- `listen`: `{"host": "0.0.0.0", "port": 8300}` — IPv4, see §1.3.
- `api_keys`: at least one per-app entry. Generate with `openssl rand -hex 32`. **Do not paste
  keys into a chat, a commit, or a ticket** — put them straight into the file and the app's own
  secret store.
- `upstreams`: one per bridge, `http://127.0.0.1:820x` (loopback, not the public name), each with
  that bridge's existing key. The bridges read theirs from
  `/home/debian/projects/ai-prompt/ai_prompt_api/.env` (`AI_PROMPT_API_KEY`); copy the values
  without echoing them:
  ```bash
  grep -oE '^[A-Z_]+=' /home/debian/projects/ai-prompt/ai_prompt_api/.env   # names only
  ```
  If a bridge instance was started with a different `.env`, take its key from wherever that
  instance was launched. Mark bridges you do not want in rotation `"enabled": false` rather than
  deleting them.
- `ws_targets` / `unix_socket`: only if you want the CLI fronts on this box. The unix socket
  defaults to `/tmp/ai-prompt-router.sock` — do not point it at `ai-prompt-native.sock`, the
  native host owns that name.

**No hot-reload:** every later edit to this file needs a router restart.

## 5. Start it and prove it locally (still not public)

`INPUT` policy is DROP and 8300 has no rule yet, so the router is only reachable from the box —
which is exactly where you want to test it first.

```bash
cd /home/debian/projects/ai-prompt
./scripts/start-router.sh                       # (re)start on :8300, waits until it answers
curl -s localhost:8300/health
curl -s -H "Authorization: Bearer $ROUTER_KEY" localhost:8300/upstreams   # every bridge healthy?
```

Expect `extension_connected: true` on `/health` (it means "at least one upstream is healthy") and
one entry per bridge in `/upstreams`. A bridge listed `healthy: false` here is a *bridge* problem —
fix it before going further, or set `"enabled": false` so it stays out of the rotation.

Now send two real prompts through the loopback and watch them alternate:

```bash
for i in 1 2; do
  curl -sD- -o /dev/null -X POST localhost:8300/prompt \
    -H "Authorization: Bearer $ROUTER_KEY" -H 'Content-Type: application/json' \
    -d '{"text":"say ok","ephemeral":true}' | grep -i '^x-ai-prompt-upstream'
done
```

Two different upstream names = round-robin is live. (This opens a tab in each browser; that is the
point — it proves the whole path, not just the front door.)

## 6. Make it survive a reboot

`start-router.sh` dies with the box. Install the unit before you move traffic onto the router:

```bash
sudo cp /home/debian/projects/ai-prompt/scripts/ai-prompt-router.service \
        /etc/systemd/system/ai-prompt-router.service
sudo systemctl daemon-reload
/home/debian/projects/ai-prompt/scripts/start-router.sh --stop   # free the port first
sudo systemctl enable --now ai-prompt-router
systemctl status ai-prompt-router
journalctl -u ai-prompt-router -n 50
curl -s localhost:8300/health
```

Check the unit's `User=`, `WorkingDirectory=`, `PYTHONPATH=` and `ExecStart=` match the paths above
before installing. Verify persistence with `sudo systemctl restart ai-prompt-router` (a reboot of
a shared production box is not a test you run for this).

Note the bridges themselves are still hand-started per browser and do **not** survive a reboot —
unchanged by this work, and worth a separate follow-up.

## 7. Open 8300

```bash
ssh ovh 'sudo /usr/local/hestia/bin/v-add-firewall-rule ACCEPT 0.0.0.0/0 8300 TCP "ai-prompt router"'
ssh ovh 'sudo /usr/local/hestia/bin/v-list-firewall | tail -5'
ssh ovh 'sudo iptables -S INPUT | grep 8300'
```

This writes `/usr/local/hestia/data/firewall/rules.conf` **and** reloads iptables — which is what
re-applies rules 15/16 (8203/8204). Never use a raw `iptables -I`: Hestia wipes it on the next
reload.

If the calling app has a fixed egress IP, prefer it to `0.0.0.0/0`:
`v-add-firewall-rule ACCEPT <app-ip>/32 8300 TCP "ai-prompt router"`.

Verify from off-box, including the negative cases:

```bash
curl -s -4 --max-time 6 -o /dev/null -w 'health %{http_code}\n' http://cp.cholds.com:8300/health
curl -s -4 -o /dev/null -w 'no key  %{http_code}\n'  http://cp.cholds.com:8300/upstreams          # expect 401
curl -s -4 -o /dev/null -w 'bad key %{http_code}\n' -H 'Authorization: Bearer nope' \
     http://cp.cholds.com:8300/upstreams                                                          # expect 401
curl -s -4 -H "Authorization: Bearer $ROUTER_KEY" http://cp.cholds.com:8300/upstreams             # expect 200 + the table
```

A 200 on the no-key call means `api_keys` is empty and you have just published an open prompt API —
stop, fix the config, restart, re-test.

## 8. Repoint the apps, then close 8201–8204

**Order matters. Do not close the bridges until an app has actually run through the router.**
Closing first is the one move here that causes an outage.

1. Change each app's ai-prompt connection from `http://cp.cholds.com:820x` + that bridge's key to
   `http://cp.cholds.com:8300` + the router key. Nothing else in the app changes.
2. Exercise a real user path in each app (not just `/health`) and confirm a prompt round-trips.
   `X-AI-Prompt-Upstream` on the response tells you which browser served it.
3. Only now, close the bridge ports. Re-read the ids first — they shift as rules are added and
   removed — and delete highest first:

```bash
ssh ovh 'sudo /usr/local/hestia/bin/v-list-firewall'          # find the 8201/8202/8203/8204 rows
ssh ovh 'sudo /usr/local/hestia/bin/v-delete-firewall-rule 16'   # 8204
ssh ovh 'sudo /usr/local/hestia/bin/v-delete-firewall-rule 15'   # 8203
ssh ovh 'sudo /usr/local/hestia/bin/v-delete-firewall-rule 14'   # 8202
ssh ovh 'sudo /usr/local/hestia/bin/v-delete-firewall-rule 13'   # 8201
```

Verify the lock-down from both sides:

```bash
for p in 8201 8202 8203 8204; do                      # off-box: expect 000 (timeout), not 200
  printf '%s -> ' "$p"
  curl -s -4 --max-time 6 -o /dev/null -w '%{http_code}\n' "http://cp.cholds.com:$p/health"
done
ssh ovh 'for p in 8201 8202 8203 8204; do curl -s -o /dev/null -w "$p %{http_code}\n" \
         http://127.0.0.1:$p/health; done'            # on-box: expect 200 each
ssh ovh 'curl -s -H "Authorization: Bearer '"$ROUTER_KEY"'" localhost:8300/upstreams'  # all still healthy
```

Then send one more prompt through the public router endpoint. If it works, the sweep is complete:
public 8300 only, private 8201–8204, all four browsers still reachable through the router.

### Two stronger lock-downs, and why the firewall is the one to use here

- **Bind the bridges to loopback (defence in depth, not yet applied).** `scripts/start-bridge.sh`
  hardcodes `--host 0.0.0.0`; there would be no public socket at all if that line became
  `--host "${BIND_HOST:-127.0.0.1}"` and each bridge were restarted. That is a code change to a
  file outside this task's scope — do it as its own commit, and remember restarting a bridge drops
  its extension WebSocket (the extension reconnects on its own) and kills any in-flight prompt.
- **Native-host-managed bridges cannot be bound from the shell.** Not used on this box today, but
  if a browser ever manages its own host: the extension's `configure` message carries only
  `{httpPort, apiKey}`, and `native_host.py` defaults the bind host to `0.0.0.0` on every
  `configure` — overriding `AI_PROMPT_HOST_HTTP_HOST`. For those, the firewall rule is the *only*
  lock-down that holds.
- A source-IP rule (`v-add-firewall-rule ACCEPT <app-ip>/32 8201 TCP …`) is the middle option if
  some app genuinely must keep talking to one bridge directly.

## 9. Rollback

Each step undoes independently; work upwards from wherever you are.

```bash
# a) re-open the bridge ports (restores the pre-change posture exactly)
ssh ovh 'sudo /usr/local/hestia/bin/v-add-firewall-rule ACCEPT 0.0.0.0/0 8201 TCP "ai-prompt ff1"'
ssh ovh 'sudo /usr/local/hestia/bin/v-add-firewall-rule ACCEPT 0.0.0.0/0 8202 TCP "ai-prompt chrome"'
# (8203/8204 only if you want them public — they were NOT reachable before this work)

# b) point the apps back at cp.cholds.com:820x with their old per-bridge keys

# c) stop the router
ssh ovh 'sudo systemctl disable --now ai-prompt-router'
#   or, if you started it by hand:  ./scripts/start-router.sh --stop

# d) close 8300 again
ssh ovh 'sudo /usr/local/hestia/bin/v-list-firewall'                    # find the 8300 rule id
ssh ovh 'sudo /usr/local/hestia/bin/v-delete-firewall-rule <ID>'
```

The bridges are untouched by all of this, so rollback is a firewall + app-config change only.

## 10. Troubleshooting

| Symptom | Where to look |
|---|---|
| Router won't start | `journalctl -u ai-prompt-router -n 100`, or `/tmp/ai-prompt-router-8300.log` if started by the script. Missing/invalid `router.json` is the usual cause. |
| `Nothing listening on :8300` after `--stop` | The systemd unit restarted it (`Restart=on-failure`) — use `systemctl stop` instead. |
| `/health` says `extension_connected: false` | No upstream is healthy. `GET /upstreams` names each one's `last_error`; then check the bridge itself with `curl 127.0.0.1:820x/health` on the box. |
| One bridge never gets traffic | It is `enabled: false`, unhealthy, or missing the pinned provider in its `providers` list. |
| Upstream returns 401 in `/upstreams` | Wrong key for that bridge in `router.json`; the router marks it misconfigured and routes around it. |
| Stale `/tmp/ai-prompt-router.sock` after a hard kill | Remove the file and restart. |
| Ports open again after unrelated Hestia work | Any Hestia firewall action reloads the whole chain from `rules.conf`. `rules.conf` is the source of truth — fix it there, never with raw `iptables`. |

## 11. Monitoring gap

Nothing watches these ports today, which is why 8203/8204 could be publicly unreachable without
anyone noticing. The cheapest closing check, using an endpoint that already exists:

- poll `http://cp.cholds.com:8300/health` and alert when it is not 200 **or**
  `extension_connected` is false (that covers "every browser died", not just "the process is up");
- and, less often, `GET /upstreams` with the router key, alerting when the count of
  `healthy: true` upstreams drops below the number you expect — that is the check that would have
  caught two dead browsers behind a perfectly healthy front door.
