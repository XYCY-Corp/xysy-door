# XYSY door — a Hermes plugin

Lets [XYSY](https://xysy.ai) on the web drive the creative applications on this computer, without
XYSY installing an application of its own and without Claude Desktop needing to be open.

## Install

```
hermes plugins install XYSY-Corp/xysy-door --enable
hermes serve --stop
hermes serve
```

The first command clones the plugin into `~/.hermes/plugins/xysy` and adds it to `plugins.enabled`.
The other two **restart** Hermes' server. Then open [xysy.ai](https://xysy.ai) → **Set up Hermes**
and press **↻ Try again**.

🔴 **The restart is the step people miss.** Hermes discovers dashboard plugins when its server
*starts*, so a server that was already running was started before this plugin existed and will
never load it. Running plain `hermes serve` on a machine that already has one answers:

```
ERROR: [Errno 48] error while attempting to bind on address ('127.0.0.1', 9119): address already in use
```

— the install looks fine, nothing changes, and the app still reports that it cannot reach the
computer. `hermes serve --stop` first is safe either way; with nothing running it just says so.

⚠️ **Ignore the installer's own advice to run `hermes gateway restart`.** That manages the
*messaging* gateway — Telegram, Discord, WhatsApp — not the backend server this plugin lives in.
It will appear to succeed and change nothing.

`hermes serve` runs in the foreground and stops when you close its window. To keep the door up
after a reboot, run this once:

```
bash ~/.hermes/plugins/xysy/keep-serving.sh
```

It writes a LaunchAgent, starts it, and then asks the door whether it answered. Safe to run twice;
removes nothing. To undo, see the end of this file.

Update later with `hermes plugins update xysy`. Remove with `hermes plugins remove xysy`.

## Why a door of its own

Hermes' own local server refuses `xysy.ai` by name — measured against a real `hermes serve`:

```
Origin: https://xysy.ai        ->  400  Disallowed CORS origin
Origin: http://localhost:5173  ->  200  allowed
```

That is a hardcoded rule with a security reason attached, not a setting. So this plugin opens a
second, much smaller listener on `127.0.0.1:4850` and answers only for XYSY.

## The four locks

1. **Loopback only.** The listener binds `127.0.0.1`; nothing off this machine can reach it.
2. **An origin allowlist**, enforced on the preflight *and* on the request.
3. **A bearer token, always** — including when there is no `Origin` header. "No origin means not a
   browser, so trust it" is the wrong way round and is deliberately not done here.
4. **Pairing.** The door belongs to exactly one XYSY account. The first key is verified upstream
   with xysy.ai, and compared with `hmac.compare_digest`, never `==`.

## What it can do

`hello` · `pair` · `system` · `inventory` · `apps` · `servers` / `server_verify` · `capture` ·
`projects` · `project_create` · `run_write` · `run_start` · `run_status` · `run_stop` ·
`read_output` · `stage_skills` · `think` · `open`

Deliberately **not** exposed: anything that runs a shell, and `verify_mcp` — the caller hands that
one a command line, and a door reachable from a web page does not get to run command lines on this
computer. `think` refuses anything needing the live web.

**Screenshots go through the application's own MCP connector**, not the screen: no Screen Recording
permission, and a dialog sitting over the window cannot corrupt the result. Connector definitions
come from Hermes' own `mcp_servers` config. A screen grab remains only as a fallback, and says so.

## Keeping the server up on macOS

```
cat > ~/Library/LaunchAgents/ai.xysy.hermes-serve.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.xysy.hermes-serve</string>
  <key>ProgramArguments</key><array>
    <string>PYTHON</string><string>HERMES</string><string>serve</string><string>--skip-build</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
</dict></plist>
PLIST
```

Replace `PYTHON` with `~/.hermes/hermes-agent/venv/bin/python` and `HERMES` with
`~/.hermes/hermes-agent/hermes` (absolute paths — launchd does not expand `~`), then
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.xysy.hermes-serve.plist`.

## When the door does not open

`hermes serve` starts the door in a daemon thread, wrapped so a plugin can never take Hermes down.
That is correct, and it means a door that fails to start is **invisible** — Hermes runs, the plugin
is enabled, nothing listens on 4850, and no log says why. Run the door by hand and it will tell you:

```
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/plugins/xysy/dashboard/api.py
```

It prints the port, the state file and the Hermes home it resolved, then either **Listening** or
**THE DOOR DID NOT OPEN** with the reason. Ctrl+C to stop.

As of 0.4.3 the same reason is also written to stderr at startup and returned by the plugin's
`/status` route. ⚠️ The stderr line was **not** observed in `hermes serve`'s own log during
testing — Hermes may capture or discard plugin stderr — so treat running the module by hand as the
reliable way to see it, not the log.

## Checking it works

```
curl -s http://127.0.0.1:4850/xysy/hello
{"ok": true, "door": "xysy", "version": "0.4.2", "host": "hermes", "paired": false, "email": ""}
```
