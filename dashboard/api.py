"""XYCY's door — the one thing a web page at xycy.ai can knock on.

WHY THIS FILE EXISTS
--------------------
Hermes' own local server refuses xycy.ai by name. Measured 2026-08-10 on a real
`hermes serve`: an identical request labelled `Origin: https://xycy.ai` comes back
400 "Disallowed CORS origin", while `Origin: http://localhost:5173` sails through.
That is a hardcoded regex in hermes_cli/web_server.py with a written reason —
otherwise any website you visited could read and change Hermes' config and secrets.
It is not a setting, and patching theirs would strand every user on a fork.

So XYCY brings its own door. Hermes imports this file when the plugin is enabled,
which gives us a process that is already running and already has Hermes in it; we
open a second, much smaller listener beside Hermes' own and answer only for us.

    ~/.hermes/plugins/xycy/dashboard/{manifest.json, api.py}
    enabled via  plugins.enabled  in config.yaml  (or one click in Hermes' UI)

THE THREAT, STATED PLAINLY
--------------------------
Every website you visit can make your browser send requests to 127.0.0.1. This door
can drive your applications. So the interesting question is never "can XYCY reach
it" — it is "why can nothing else". Four answers, and all four are required:

  1. IT ONLY LISTENS TO THE LOOPBACK. Nothing off this machine can reach the port.

  2. ORIGIN ALLOWLIST, ON THE PREFLIGHT *AND* ON THE REQUEST. A browser asks
     permission before sending anything interesting, and we say no to everyone
     except XYCY. This is what stops evil.example driving your Rhino from a tab you
     forgot you had open.

  3. A BEARER TOKEN, ALWAYS. Origin checking alone trusts a header a browser sets
     honestly and a script does not. The XYCY Bridge's rule — "a caller with
     no Origin is not a browser, so trust it" — is the wrong way round, and it is
     deliberately NOT repeated here: no Origin means no browser means it still
     needs the token.

  4. PAIRING. A token is not enough on its own, or any XYCY user anywhere could
     drive THIS computer. The door is paired to exactly one account: the first
     token is verified with xycy.ai, the account it names is remembered, and from
     then on only that account gets in.

Nothing here is typed by a person. The browser is already signed in to xycy.ai, it
fetches its own short-lived key, and hands it to the door over loopback — the same
handover the Bridge already uses to connect a computer to an account.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- XY-SAYPY ---------------------------------------------------------------
# Say something when a handler catches.
#
# A LOCAL copy on purpose. This file ships inside the agent bundle, and some of
# these files are published again as part of the Hermes door or copied into a
# throwaway per-run home, so a shared module would have to be added to packaging
# lists that nothing checks - and an import that is missing on somebody else's
# machine is a worse failure than twelve lines written out more than once.
#
# It never raises, and it repeats itself at most three times per place, so a
# handler inside a loop cannot bury everything else in the log.
import sys as _xy_sys


def _pid_is_alive(pid):
    """Ask whether a process is running, without touching it. XY-KILLNOTASK."""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            got = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(got) and code.value == 259           # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False



_XY_SAID = {}


def say_something(err, where):
    try:
        n = _XY_SAID.get(where, 0) + 1
        _XY_SAID[where] = n
        if n > 3:
            return
        tail = " (further reports from this line are dropped)" if n == 3 else ""
        _xy_sys.stderr.write("[xycy] " + str(where) + ": " + type(err).__name__
                             + ": " + str(err) + tail + "\n")
        _xy_sys.stderr.flush()
    except Exception:
        return  # the reporter cannot report itself; this is the one place silence is right
# --- end XY-SAYPY -----------------------------------------------------------


try:  # Hermes imports this file for `router`; the import must never break serve.
    from fastapi import APIRouter
except Exception:  # pragma: no cover - only when FastAPI is absent
    APIRouter = None

# A blank page at :4850/xycy is a STALE door, not a broken one - this is the fingerprint.
DOOR_VERSION = "0.7.5"
DOOR_PORT = int(os.environ.get("XYCY_DOOR_PORT", "4850"))

# Who may knock. A browser sends its page's origin; anything not on this list is
# refused before it can send a body. localhost is here for development only.
ALLOWED_ORIGINS = {
    "https://xycy.ai",
    "https://www.xycy.ai",
    "https://xysy.ai",             # the old site name, kept while it redirects to the new one  # KEEP-OLD-NAME
    "https://www.xysy.ai",         # KEEP-OLD-NAME
    "https://xysy-2ct.pages.dev",  # the Pages project's own preview address  # KEEP-OLD-NAME
}
for _extra in (os.environ.get("XYCY_DOOR_ORIGINS") or "").split(","):
    if _extra.strip():
        ALLOWED_ORIGINS.add(_extra.strip())

# XY-DOORUI - the door now SERVES the screen too (v0.4.0). Until now the only
# thing on this machine that could put /xycy in a browser was the Claude Desktop
# extension, so the app existed exactly as long as Claude was open. Hermes is
# supposed to be the local agent; the door serves the same downloaded screen
# from the same cache, so http://127.0.0.1:4850/xycy works with Claude closed.
# A page we serve is our own origin, and its POSTs carry that origin — so the
# door's own address must be on its own allowlist.
SELF_ORIGINS = {"http://127.0.0.1:%d" % DOOR_PORT, "http://localhost:%d" % DOOR_PORT}
ALLOWED_ORIGINS |= SELF_ORIGINS

# Where the first token is checked. Reusing the relink route means the door never
# has to understand XYCY's tokens: it asks xycy.ai "is this real, and whose is it",
# and gets a fresh key back in the same breath.
VERIFY_URL = os.environ.get("XYCY_DOOR_VERIFY_URL", "https://xycy.ai/api/m/relink")

def hermes_home() -> Path:
    """Where Hermes keeps its own home folder.

    Hermes' own installer sets HERMES_HOME, so that wins whenever it is set. When it
    is not set the default is not the same on every operating system, and assuming
    ~/.hermes everywhere is how XYCY came to look in an empty folder: measured on
    8 September 2026, a Hermes Desktop install on Windows put its home in
    %LOCALAPPDATA%\\hermes and left ~/.hermes holding nothing at all.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


STATE = Path(os.environ.get("XYCY_DOOR_STATE") or (hermes_home() / "xycy-door.json"))
RECHECK_SECONDS = 30 * 60


# --------------------------------------------------------------------------- state
def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STATE)


def _verify_upstream(token: str) -> dict:
    """Ask xycy.ai whether this key is real and whose it is.

    Returns {} for anything other than a clean yes. A door that guessed here would
    be a door that opens for a forged key.
    """
    req = urllib.request.Request(VERIFY_URL, headers={
        "authorization": "Bearer " + token,
        "accept": "application/json",
        "user-agent": "XYCY-Door/" + DOOR_VERSION,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:
        return {}
    if not body.get("ok") or not body.get("email"):
        return {}
    return body


# ------------------------------------------------------------------- the screen
# The screen is DOWNLOADED, not built in — same contract as the Claude extension
# (see local-agent/mcpb/server/index.js): one source (xycy.ai /api/m/ui), one
# cache (~/.xycy/ui/xycy.html), shared between both doors. Offline serves the
# last download; a machine never linked says so in words.
# The home folder moved with the product's renames: ~/.openstudio, ~/.xysy, now ~/.xycy.  # KEEP-OLD-NAME
# Move the newest old home once, so a machine that runs only Hermes (no Claude extension to
# do the move) keeps its pairing, its registry and its cached screen. Lines that keep an old
# name carry KEEP-OLD-NAME so a rename pass skips them.
try:
    _new_home = Path.home() / ".xycy"
    if not _new_home.exists():
        for _old_name in (".xysy", ".openstudio"):  # KEEP-OLD-NAME
            _old_home = Path.home() / _old_name
            if _old_home.exists():
                _old_home.rename(_new_home)
                break
    _old_ui = _new_home / "ui" / "xysy.html"  # KEEP-OLD-NAME
    if _old_ui.exists() and not (_new_home / "ui" / "xycy.html").exists():
        _old_ui.rename(_new_home / "ui" / "xycy.html")
    _old_etag = _new_home / "ui" / "xysy.etag"  # KEEP-OLD-NAME
    if _old_etag.exists() and not (_new_home / "ui" / "xycy.etag").exists():
        _old_etag.rename(_new_home / "ui" / "xycy.etag")
except Exception as _xy_e:
    say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:245')
UI_DIR = Path(os.environ.get("XYCY_UI_DIR") or (Path.home() / ".xycy" / "ui"))
UI_FILE = UI_DIR / "xycy.html"
UI_ETAG = UI_DIR / "xycy.etag"
UI_MIN_BYTES = 200_000        # a sign-in page / error JSON is a few hundred bytes;
                              # writing one over the cache would blank the app offline
UI_GAP_S = float(os.environ.get("XYCY_UI_GAP_MS", "60000")) / 1000.0
UI_FETCH_S = float(os.environ.get("XYCY_UI_FETCH_MS", "6000")) / 1000.0
CLOUD_BASE = (os.environ.get("XYCY_CLOUD_BASE") or "https://xycy.ai").rstrip("/")

_ui_lock = threading.Lock()
_ui_checked_at = 0.0


def _ui_token() -> str:
    """A key good enough to ask the site for the screen: the door's own pairing
    first, else the Claude extension's cloud link — both live on this machine."""
    state = _load()
    if state.get("token") and float(state.get("expiresAt") or 0) / 1000.0 > time.time():
        return state["token"]
    try:
        link = json.loads((Path.home() / ".xycy" / "cloud_link.json").read_text(encoding="utf-8"))
        if link.get("token"):
            return link["token"]
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:270')
    return ""


def _refresh_ui() -> None:
    """Best effort, never raises, never blocks longer than UI_FETCH_S."""
    global _ui_checked_at
    with _ui_lock:
        if time.time() - _ui_checked_at < UI_GAP_S:
            return
        token = _ui_token()
        if not token:
            return                    # not linked: the cache is all there is
        headers = {"authorization": "Bearer " + token, "accept": "text/html",
                   "user-agent": "XYCY-Door/" + DOOR_VERSION}
        etag = ""
        try:
            etag = UI_ETAG.read_text(encoding="utf-8").strip()
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:289')
        # An etag with no file behind it would answer 304 forever.
        if not UI_FILE.exists():
            etag = ""
        if etag:
            headers["if-none-match"] = etag
        req = urllib.request.Request(CLOUD_BASE + "/api/m/ui", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=UI_FETCH_S) as resp:
                body = resp.read()
                new_etag = resp.headers.get("etag") or ""
                status = resp.status
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                _ui_checked_at = time.time()
            return
        except Exception:
            return
        if status != 200 or len(body) < UI_MIN_BYTES:
            return
        if b"<!doctype html" not in body[:400].lower():
            return
        try:
            UI_DIR.mkdir(parents=True, exist_ok=True)
            tmp = UI_FILE.with_suffix(".html.part")
            tmp.write_bytes(body)
            tmp.replace(UI_FILE)      # atomic: never a half-written screen
            if new_etag:
                UI_ETAG.write_text(new_etag, encoding="utf-8")
            _ui_checked_at = time.time()
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:320')


_CTYPES = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg",
           "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
           "woff2": "font/woff2", "woff": "font/woff", "css": "text/css",
           "js": "text/javascript", "json": "application/json",
           "html": "text/html", "ico": "image/x-icon", "pdf": "application/pdf"}


def _no_screen_page() -> bytes:
    linked = bool(_ui_token())
    body = ("<p>This computer is connected to a XYCY account, but could not reach the site "
            "just now. Reload this page once the network is back.</p>" if linked else
            "<p>Open <a href='https://xycy.ai'>xycy.ai</a> in this browser once, sign in, and "
            "connect this computer. The screen arrives with it; after that it works offline "
            "from the last download.</p>")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>XYCY</title>"
            "<style>body{font-family:-apple-system,system-ui,sans-serif;margin:0;"
            "padding:32px;max-width:620px;line-height:1.5;color:#1a1a1a}</style></head>"
            "<body><h2>There is no XYCY screen on this computer yet</h2>"
            "<p>XYCY keeps the last screen the site gave it and serves that copy — "
            "this door (Hermes) does exactly what the Claude extension does.</p>"
            + body + "</body></html>").encode("utf-8")


# ----------------------------------------------------------------------- the jobs
def _job_ping(_args: dict) -> dict:
    return {"ok": True, "pong": True, "at": int(time.time())}


# ------------------------------------------------------- the app's own connector
# THE RIGHT WAY TO PHOTOGRAPH AN APPLICATION, and the reason the OS screen grab below
# is only a fallback:
#
#   * it needs no permission at all — no Screen Recording, no Accessibility;
#   * it cannot be occluded. A screen grab of Rhino with a dialog on top of it is a
#     picture of the dialog, and the person only finds out later;
#   * it is the app's OWN render, at whatever size we ask for, not whatever the window
#     happened to be sized to.
#
# The connectors themselves are Hermes' business, not ours: we read the set the person
# already has in ~/.hermes/config.yaml. That is the whole premise of being a plugin —
# Hermes owns the connectors, XYCY asks them questions.

CAPTURE_SPEC = {
    "rhino": ("capture_viewport", {"viewport": "active", "width": 1000, "height": 667}),
    "blender": ("get_viewport_screenshot", {"max_size": 1000}),
    "freecad": ("get_view", {"view_name": "Isometric"}),
}

# What a person calls an application vs. what its connector is called.
APP_TO_SERVER = {
    "rhino": "rhino", "rhinoceros": "rhino",
    "blender": "blender",
    "freecad": "freecad",
    "sketchup": "sketchup",
    "comfyui": "comfyui",
}


def _hermes_config() -> dict:
    home = hermes_home()
    try:
        import yaml
        return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        try:
            return json.loads((home / "config.yaml").read_text(encoding="utf-8")) or {}
        except Exception:
            return {}


def _hermes_model() -> dict:
    """Which local model Hermes is set to, read from its config rather than inferred.

    🔴 THE BUG THIS EXISTS TO STOP. Left to work it out for itself, the runner found no
    model, Hermes fell back to its OWN default provider — openrouter — and the run died
    in 1.5 seconds with "401 Missing Authentication header". Nothing was wrong with the
    run; it had simply been pointed at a service the person has no account with. A run
    must be told which brain to use, out loud, every time.
    """
    m = (_hermes_config().get("model") or {})
    out = {}
    for key, flag in (("default", "--model"), ("provider", "--provider"),
                      ("base_url", "--base-url")):
        if m.get(key):
            out[flag] = str(m[key])
    ctx = m.get("ollama_num_ctx") or m.get("context_length")
    if ctx:
        out["--context-length"] = str(int(ctx))
    return out


def _hermes_servers() -> dict:
    """The connectors Hermes already has. Read from its config, never from ours."""
    home = hermes_home()
    cfg = home / "config.yaml"
    try:
        import yaml  # Hermes' own dependency; we run inside its process.
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    servers = data.get("mcp_servers") or {}
    return servers if isinstance(servers, dict) else {}


def _end(proc) -> None:
    """Stop a connector AND everything it started.

    🔴 THE LEAK THIS CLOSES. `uvx rhinomcp` is a LAUNCHER: it downloads, then execs the real
    server as a child. Killing the process we spawned killed the launcher and left the server
    running, holding Rhino's port. Four of them were found alive after a handful of captures,
    and the next capture hung waiting for a port the previous one still owned — which is how a
    demo that worked five times in a row stops working on the sixth.

    So each connector is started in its own process group and the GROUP is signalled. Same
    lesson the run path already learned when a stopped run left its app servers behind.
    """
    try:
        if os.name != "nt":
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:452')


def _mcp_call(entry: dict, tool: str, args: dict, timeout: float = 60.0) -> dict:
    """One tool call against a stdio connector: start, initialize, call, stop.

    Deliberately its own short-lived process rather than anything shared. A capture is
    a question, not a session, and a connector left running holds the app's port.
    """
    command = entry.get("command")
    if not command:
        return {"ok": False, "error": "that connector has no way to start"}
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (entry.get("env") or {}).items()})
    # A GUI-launched parent has a stripped PATH; the connectors live in the places a
    # person's own shell would find them.
    extra = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = os.pathsep.join([env.get("PATH", "")] + extra)

    try:
        proc = subprocess.Popen(
            [command] + [str(a) for a in (entry.get("args") or [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
            # Its OWN process group — see _end() for the leak this closes.
            start_new_session=(os.name != "nt"),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    def send(obj):
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:487')

    result = {"ok": False, "error": "the connector never answered"}
    deadline = time.time() + timeout
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "xycy-door", "version": DOOR_VERSION}}})
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line or not line.startswith("{"):
                continue          # connectors print startup chatter on stdout too
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == 1 and msg.get("result") is not None:
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": tool, "arguments": args}})
            elif msg.get("id") == 2:
                if msg.get("error"):
                    result = {"ok": False, "error": str(msg["error"].get("message"))[:300]}
                    break
                content = (msg.get("result") or {}).get("content") or []
                image = next((c for c in content if c.get("type") == "image" and c.get("data")), None)
                if image:
                    result = {"ok": True, "mime": image.get("mimeType") or "image/png",
                              "data": image["data"]}
                else:
                    said = " ".join(c.get("text", "") for c in content).strip()
                    result = {"ok": False, "error": said[:300] or "the app returned no picture"}
                break
    finally:
        _end(proc)
    return result


def _says_error(text: str) -> bool:
    """An application's own words outrank its envelope.

    blender-mcp answers "Error executing code: Could not connect to Blender" with
    isError FALSE. Trusting the flag alone reports that as a success - the exact bug
    call_tool exists to prevent. Narrow on purpose: the reply has to LEAD with the
    complaint, or say it could not connect.
    """
    t = str(text or "").strip()
    if not t:
        return False
    return bool(re.match(r"(?i)^(error\b|exception\b|traceback \(most recent call last\))", t)
                or re.search(r"(?i)could not connect to", t))


def _mcp_call_text(entry: dict, tool: str, args: dict, timeout: float = 180.0) -> dict:
    """One tool call against a stdio connector, for a call whose answer is TEXT.

    _mcp_call is the capture path: it wants an image back and reports text as a failure.
    Driving an application is the opposite - "Code executed successfully" IS the answer.
    Same short-lived process, same handshake, different idea of success.

    An MCP error, or an isError envelope carrying the application's own complaint, is
    returned ok:False. This job exists because a stand-in once answered ok:true and three
    steps went green over an untouched scene (XY-PLAYTRUTH); it must not repeat that.
    """
    command = entry.get("command")
    if not command:
        return {"ok": False, "error": "that connector has no way to start"}
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (entry.get("env") or {}).items()})
    extra = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = os.pathsep.join([env.get("PATH", "")] + extra)
    try:
        proc = subprocess.Popen(
            [command] + [str(a) for a in (entry.get("args") or [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
            start_new_session=(os.name != "nt"),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    def send(obj):
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:576')

    result = {"ok": False, "error": "the connector never answered"}
    deadline = time.time() + timeout
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "xycy-door", "version": DOOR_VERSION}}})
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == 1 and msg.get("result") is not None:
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": tool, "arguments": args or {}}})
            elif msg.get("id") == 2:
                if msg.get("error"):
                    result = {"ok": False, "error": str((msg["error"] or {}).get("message"))[:300]}
                    break
                res = msg.get("result") or {}
                said = "\n".join(c.get("text", "") for c in (res.get("content") or [])).strip()
                if res.get("isError") or _says_error(said):
                    result = {"ok": False, "error": said[:300] or "the application refused the call"}
                else:
                    result = {"ok": True, "text": said, "result": res}
                break
    finally:
        _end(proc)
    return result


def _connector_entry(name: str) -> dict:
    """How to start ONE connector: Hermes' config first, then XYCY's own registry.

    _hermes_servers() is deliberately Hermes-only, because _job_servers must describe
    Hermes rather than us. But a RUN never uses that file either - it writes a per-run
    Hermes profile from ~/.xycy/registry.json - so on a normally set up machine the
    global config is empty and asking only it means the door can drive nothing.

    The boundary this door exists to hold is that the PAGE never hands this computer a
    command line. It does not: it names an app. Which local file records the command for
    that app is our business, and registry.json is the one XYCY actually keeps.
    """
    name = str(name or "").strip()
    if not name:
        return {}
    entry = _hermes_servers().get(name)
    if isinstance(entry, dict) and entry.get("command"):
        return entry
    try:
        reg = json.loads((Path.home() / ".xycy" / "registry.json").read_text(encoding="utf-8"))
        ours = (reg.get("servers") or {}).get(name)
        if isinstance(ours, dict) and ours.get("command"):
            return ours
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:639')
    return entry if isinstance(entry, dict) else {}


def _job_call_tool(args: dict) -> dict:
    """Run ONE tool on a connected application and return what it said.

    The page names the application and the tool. It never names a command - the command
    comes from Hermes' own connector config here, which is what keeps a web page from
    handing this computer a command line (see scripts/verify_door_boundary.mjs).
    """
    name = str(args.get("server") or "").strip()
    tool = str(args.get("tool") or "").strip()
    if not name:
        return {"ok": False, "error": "no application was named"}
    if not tool:
        return {"ok": False, "error": "no tool was named"}
    entry = _connector_entry(name)
    if not entry or not entry.get("command"):
        return {"ok": False, "error": "no connector called %s on this computer" % name}
    out = _mcp_call_text(entry, tool, args.get("args") or {},
                         timeout=float(args.get("timeout") or 180))
    return dict(out, server=name, tool=tool)


def _job_servers(_args: dict) -> dict:
    """Which connectors this computer has, and which of them can take a picture."""
    out = []
    for name, entry in sorted(_hermes_servers().items()):
        if not isinstance(entry, dict):
            continue
        out.append({"id": name, "command": entry.get("command"),
                    "canCapture": name.lower() in CAPTURE_SPEC})
    return {"ok": True, "servers": out}


def _job_apps(_args: dict) -> dict:
    """Which creative applications are open right now.

    Deliberately "open", not "installed": the door's job is to tell XYCY what it can
    drive this second. A full inventory is a separate, slower question.
    """
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, timeout=30).stdout
        names = sorted({l.split('","')[0].strip('"') for l in out.splitlines() if l.strip()})
    else:
        script = 'tell application "System Events" to get name of every process whose background only is false'
        out = subprocess.run(["osascript", "-e", script], capture_output=True,
                             text=True, timeout=30).stdout
        names = sorted(n.strip() for n in out.split(",") if n.strip())
    return {"ok": True, "apps": names}


def _screen_grab(app: str) -> dict:
    """Last resort. macOS only, and it needs permissions the connector route does not."""
    if os.name == "nt":
        return {"ok": False, "error": "screen capture is macOS-only in this build"}
    ids = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to tell process "%s" to get id of every window' % app],
        capture_output=True, text=True, timeout=30)
    wid = (ids.stdout or "").split(",")[0].strip()
    denied = "assistive access" in (ids.stderr or "")

    path = Path(tempfile.gettempdir()) / ("xycy-door-%d.png" % int(time.time() * 1000))
    if wid.isdigit():
        via, note = "window", ""
        try:
            subprocess.run(["osascript", "-e", 'tell application "%s" to activate' % app],
                           capture_output=True, timeout=20)
            time.sleep(0.4)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:712')
        argv = ["screencapture", "-x", "-o", "-l", wid, str(path)]
    else:
        # Say so in the answer. A picture that quietly stopped being the window you asked
        # for is the kind of wrong that surfaces in a customer's screenshot weeks later.
        via = "screen"
        note = ("could not single out %s\u2019s window%s \u2014 this is the whole screen instead"
                % (app, " (Accessibility permission not granted)" if denied else ""))
        argv = ["screencapture", "-x", str(path)]

    shot = subprocess.run(argv, capture_output=True, text=True, timeout=40)
    if shot.returncode != 0 or not path.exists() or path.stat().st_size == 0:
        return {"ok": False, "error": "the window server refused the capture",
                "detail": (shot.stderr or "").strip()[:200]}
    import base64
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    try:
        path.unlink()
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:731')
    out = {"ok": True, "via": via, "bytes": len(data),
           "dataUrl": "data:image/png;base64," + data}
    if note:
        out["note"] = note
        out["hint"] = "System Settings > Privacy & Security > Accessibility \u2014 allow Hermes"
    return out


def _job_capture(args: dict) -> dict:
    """A picture of one application, by the best means available.

    Order matters and is not negotiable: the app's own connector first, because it needs
    no permission and cannot be occluded; the OS only if there is no connector for it.
    """
    app = str(args.get("app") or "").strip()
    server = str(args.get("server") or "").strip().lower()
    if not app and not server:
        return {"ok": False, "error": "which application?"}
    if not server:
        server = APP_TO_SERVER.get(app.lower(), app.lower())

    entry = _hermes_servers().get(server)
    spec = CAPTURE_SPEC.get(server)
    if entry and spec:
        tool, targs = spec
        if isinstance(args.get("toolArgs"), dict):
            targs = dict(targs, **args["toolArgs"])
        shot = _mcp_call(entry, tool, targs, timeout=float(args.get("timeout") or 60))
        if shot.get("ok"):
            return {"ok": True, "app": app or server, "server": server, "via": "connector",
                    "tool": tool, "bytes": len(shot["data"]),
                    "dataUrl": "data:%s;base64,%s" % (shot["mime"], shot["data"])}
        connector_error = shot.get("error")
    else:
        connector_error = ("no connector on this computer can photograph %s"
                           % (app or server))

    if not app:
        return {"ok": False, "error": connector_error}
    fallback = _screen_grab(app)
    if fallback.get("ok"):
        fallback["app"] = app
        fallback["connectorError"] = connector_error
    else:
        fallback["error"] = "%s, and the screen grab failed too: %s" % (
            connector_error, fallback.get("error"))
    return fallback


# ---------------------------------------------------------------- running a workflow
# THE RUN ITSELF IS NOT NEW. `xycy_hermes_run.py` was written for the XYCY agent and is
# already Hermes-shaped Python: it builds a per-run HERMES_HOME so a run only starts the
# connectors it declares, raises Hermes' 50-tool-call ceiling, and streams tool-by-tool
# events through an observer plugin instead of scraping stdout. All the door does is
# reach it, because the browser cannot.
#
# It is invoked as a PROCESS, not imported. A run is minutes of somebody's real work
# driving real applications; if it falls over it must not take the door — or Hermes —
# with it. Same reason the agent shells out to it today.
#
# ⚠️ The runner lives in two places (the agent bundle and here). They are kept identical
# by scripts/sync_hermes_runner.py, which fails loudly rather than letting them drift —
# this repo has lost hours to one file living in four places before.

PROJECTS_ROOT = Path(
    os.environ.get("XYCY_PROJECTS") or (Path.home() / "Documents" / "Claude" / "Projects")
)


def _runner() -> str:
    """This door's OWN runner, and nothing else.

    XY-ENGINEALONE. There used to be a second candidate here: the copy inside the Claude
    Desktop extension. It was meant kindly and it broke the one rule this door exists to
    keep - the Hermes engine must work on a computer that has Hermes and nothing else.

    What the fallback actually did was hide an incomplete install. On a machine that also
    had Claude Desktop, a door missing its own runner worked perfectly and nobody found
    out; on a machine with only Hermes, the same install was simply broken. Worse, the two
    copies drift: on 19 Sep 2026 the extension's runner had two sibling files the door's
    did not, so a door borrowing that runner pointed its write-guard hook at a path under
    the Claude extension - present on one machine, absent on the next, and silent either
    way (Hermes logs a missing hook command and carries on).

    It was also macOS-only, which made it a fallback that could not even be relied on to
    be wrong consistently.

    So: this door uses its own runner or says it has none. sync_hermes_runner.py derives
    the list of files that runner needs beside itself and refuses a push when the door is
    missing any of them, which is the check that makes this safe to be strict about.
    """
    here = Path(__file__).resolve().parent
    mine = here.parent / "runner" / "xycy_hermes_run.py"
    return str(mine) if mine.exists() else ""


def _inside_projects(p: Path) -> bool:
    """Every path the door touches must be under the projects folder.

    Without this, "make me a project called ../../.ssh" is a way to write anywhere the
    person can write. The door is reachable from a web page; it does not get to choose
    files by name.
    """
    try:
        p.resolve().relative_to(PROJECTS_ROOT.resolve())
        return True
    except Exception:
        return False


def _run_helper(argv: list, timeout: float = 180.0) -> dict:
    runner = _runner()
    if not runner:
        return {"ok": False, "error": "the XYCY runner is missing from this plugin"}
    import sys as _sys
    proc = subprocess.run([_sys.executable, runner] + argv, capture_output=True,
                          text=True, timeout=timeout)
    # The helper prints exactly one JSON object as its LAST line; a chatty dependency can
    # print before it, so match the final object rather than trusting all of stdout.
    match = re.search(r"\{[\s\S]*\}\s*$", proc.stdout or "")
    if not match:
        return {"ok": False, "error": "the runner produced no result",
                "detail": (proc.stderr or "")[-400:]}
    try:
        return json.loads(match.group(0))
    except Exception:
        return {"ok": False, "error": "could not read the runner's result"}


def _job_project_create(args: dict) -> dict:
    name = str(args.get("name") or "Workflow").strip()
    folder = re.sub(r"[\\/:*?\"<>|]+", " ", name)
    folder = re.sub(r"\s+", " ", folder).strip()[:60] or "Workflow"
    # Stripping the slashes out of "../../.ssh" already makes it harmless, but it leaves a
    # folder called ".. .. .ssh" sitting in the person's Projects. Refuse the name instead:
    # a caller reaching for a parent directory is not making a typo.
    if folder.startswith(".") or ".." in folder:
        return {"ok": False, "error": "that name is not allowed"}
    target = PROJECTS_ROOT / folder
    if not _inside_projects(target):
        return {"ok": False, "error": "that name is not allowed"}
    for sub in ("inputs", "outputs", "runs"):
        (target / sub).mkdir(parents=True, exist_ok=True)
    marker = target / ".xycy.json"
    if not marker.exists():
        marker.write_text(json.dumps({"workflow": name, "created": int(time.time() * 1000)}),
                          encoding="utf-8")
    return {"ok": True, "dir": str(target), "name": folder}


def _job_run_write(args: dict) -> dict:
    d = Path(str(args.get("dir") or ""))
    run_id = str(args.get("runId") or ("run_%d" % int(time.time() * 1000)))
    if not d.name or not _inside_projects(d):
        return {"ok": False, "error": "that is not a XYCY project folder"}
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", run_id):
        return {"ok": False, "error": "that run name is not allowed"}
    rd = d / "runs" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (d / "outputs").mkdir(parents=True, exist_ok=True)
    (rd / "plan.json").write_text(json.dumps(args.get("plan") or {}, indent=2), encoding="utf-8")
    if not (rd / "progress.json").exists():
        (rd / "progress.json").write_text(
            json.dumps({"runId": run_id, "status": "queued", "steps": []}, indent=2),
            encoding="utf-8")
    if args.get("prompt"):
        (rd / "RUN.md").write_text(str(args["prompt"]), encoding="utf-8")
    return {"ok": True, "runId": run_id, "dir": str(rd)}


def _job_run_start(args: dict) -> dict:
    d = Path(str(args.get("dir") or ""))
    run_id = str(args.get("runId") or "")
    if not _inside_projects(d) or not run_id:
        return {"ok": False, "error": "which run, in which project?"}
    argv = ["start", "--dir", str(d), "--run-id", run_id]
    if args.get("prompt"):
        argv += ["--prompt", str(args["prompt"])]
    # Say which brain, always — see _hermes_model() for the 401 this prevents.
    chosen = _hermes_model()
    if args.get("model"):
        chosen["--model"] = str(args["model"])
    for flag, value in chosen.items():
        argv += [flag, value]
    # An explicit EMPTY list means "this run needs no application" and must survive as
    # empty. Collapsing it with "unspecified" is how a text-only run ends up launching
    # every connector the person has, including one that opens an OAuth window.
    if isinstance(args.get("servers"), list):
        argv += ["--servers", ",".join(str(x) for x in args["servers"])]
    if isinstance(args.get("mcpEnv"), dict):
        argv += ["--mcp-env", json.dumps(args["mcpEnv"])]
    # Scoping a step to the tools it actually needs is Hermes' real advantage over the
    # Claude path, where the same thing is only a polite instruction in the prompt. It is
    # also what makes a small model usable: given `terminal`, llama3.1:8b answered "write
    # this file" by inventing a `hermes workflow:run` command and failing. Given `file`,
    # it writes the file.
    if args.get("toolsets"):
        argv += ["--toolsets", str(args["toolsets"])]
    if args.get("skills"):
        argv += ["--skills", str(args["skills"])]
    return _run_helper(argv, timeout=180)


def _job_run_status(args: dict) -> dict:
    d = Path(str(args.get("dir") or ""))
    run_id = str(args.get("runId") or "")
    if not _inside_projects(d) or not run_id:
        return {"ok": False, "error": "which run, in which project?"}
    out = _run_helper(["status", "--dir", str(d), "--run-id", run_id,
                       "--tail", str(int(args.get("tail") or 40))], timeout=90)
    # The model's own honest ledger, read straight off disk. Never synthesised here: a
    # door that "helpfully" closed out a step would manufacture the exact outcome the
    # run-honesty rules exist to prevent.
    try:
        out["progress"] = json.loads((d / "runs" / run_id / "progress.json").read_text(encoding="utf-8"))
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:948')
    return out


def _job_run_stop(args: dict) -> dict:
    d = Path(str(args.get("dir") or ""))
    run_id = str(args.get("runId") or "")
    if not _inside_projects(d) or not run_id:
        return {"ok": False, "error": "which run, in which project?"}
    return _run_helper(["stop", "--dir", str(d), "--run-id", run_id], timeout=90)


def _job_read_output(args: dict) -> dict:
    """Read one file a run produced, so the page can show what actually came out."""
    d = Path(str(args.get("dir") or ""))
    rel = str(args.get("path") or "")
    target = (d / rel)
    if not _inside_projects(d) or not _inside_projects(target) or ".." in rel:
        return {"ok": False, "error": "that file is not inside a XYCY project"}
    if not target.is_file():
        return {"ok": False, "error": "no such file"}
    if target.stat().st_size > 2_000_000:
        return {"ok": False, "error": "that file is too big to hand back"}
    try:
        return {"ok": True, "path": rel, "text": target.read_text(encoding="utf-8")}
    except UnicodeDecodeError:
        import base64
        return {"ok": True, "path": rel, "base64": base64.b64encode(target.read_bytes()).decode()}

# ------------------------------------------------------------------ the rest of the port
# What XYCY asks a computer for, beyond runs and pictures. Everything here either forwards
# to something Hermes already owns (connectors, skills) or is one of the three jobs Hermes
# has no reason to have (what is installed, launch this, package this).

def _job_system(_args: dict) -> dict:
    import platform
    cfg = _hermes_config()
    # XY-DOORWIDE - the page's three system_info readers want the AGENT's dialect: `os` as
    # "darwin 25.5.0" (platform.system + release, lowercased) and `xycy_root`. Answer in that
    # dialect, keep the door's own fields beside it - the UI must not be able to tell which
    # door a fact came through, or every screen grows a second branch.
    return {"ok": True, "host": platform.node(),
            "os": platform.system().lower() + " " + platform.release(),
            "python": platform.python_version(), "door": DOOR_VERSION,
            "agent": "xycy-door", "version": DOOR_VERSION,
            "xycy_root": str(Path.home() / ".xycy"),
            "hermesHome": str(hermes_home()),
            "projects": str(PROJECTS_ROOT),
            "model": (cfg.get("model") or {}).get("default")}


def _job_think(args: dict) -> dict:
    """Short reasoning, on the local model.

    Anything whose answer has to come from OUTSIDE the prompt — a live web search, finding
    a connector nobody has heard of — is NOT answered here. Measured: asked to search the
    web for MCP servers for Revit, this model invented four, with URLs. Those questions
    belong to XYCY's own key, which the PAGE can reach and the door cannot.
    """
    prompt = str(args.get("prompt") or "")
    if not prompt.strip():
        return {"ok": False, "error": "nothing to think about"}
    if args.get("web") or args.get("cloud"):
        return {"ok": False, "needs": "account",
                "error": "this one needs XYCY's own reasoning, not the local model"}
    argv = ["think", "--prompt", prompt, "--timeout", str(int(args.get("timeout") or 180))]
    if args.get("system"):
        argv += ["--system", str(args["system"])]
    chosen = _hermes_model()
    for flag in ("--model", "--provider", "--base-url", "--context-length"):
        if flag in chosen:
            argv += [flag, chosen[flag]]
    return _run_helper(argv, timeout=float(args.get("timeout") or 180) + 60)


def _job_server_verify(args: dict) -> dict:
    """Really verify a connector: start it, complete the handshake, list its tools.

    "It is in the config" is not verification — that is how a connector that cannot reach
    its application still shows a tick. Optionally CALLS a named read-only tool, so success
    means the application actually answered.
    """
    name = str(args.get("server") or "").strip()
    entry = _hermes_servers().get(name)
    if not entry:
        return {"ok": False, "error": "no connector called %s on this computer" % (name or "?")}
    probe = args.get("probe")
    tool = str(probe) if probe else "tools/list"
    if probe:
        out = _mcp_call(entry, str(probe), args.get("probeArgs") or {},
                        timeout=float(args.get("timeout") or 45))
        return {"ok": bool(out.get("ok")), "server": name, "probed": str(probe),
                "error": None if out.get("ok") else out.get("error")}
    listed = _mcp_list_tools(entry, timeout=float(args.get("timeout") or 45))
    return dict(listed, server=name)


def _mcp_list_tools(entry: dict, timeout: float = 45.0) -> dict:
    """initialize + tools/list, and nothing else. The cheap half of a real verify."""
    command = entry.get("command")
    if not command:
        return {"ok": False, "error": "that connector has no way to start"}
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (entry.get("env") or {}).items()})
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), str(Path.home() / ".local" / "bin"),
                                   "/opt/homebrew/bin", "/usr/local/bin"])
    try:
        proc = subprocess.Popen([command] + [str(a) for a in (entry.get("args") or [])],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
                                start_new_session=(os.name != "nt"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    def send(obj):
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:1067')

    out = {"ok": False, "error": "the connector never answered"}
    deadline = time.time() + timeout
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "xycy-door", "version": DOOR_VERSION}}})
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == 1 and msg.get("result") is not None:
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            elif msg.get("id") == 2:
                tools = [t.get("name") for t in ((msg.get("result") or {}).get("tools") or [])]
                out = {"ok": True, "tools": tools, "toolCount": len(tools)}
                break
    finally:
        _end(proc)
    return out


def _job_projects(_args: dict) -> dict:
    """The workflow folders XYCY has made, and nothing else in there."""
    out = []
    try:
        for child in sorted(PROJECTS_ROOT.iterdir()):
            marker = child / ".xycy.json"
            if child.is_dir() and not marker.exists() and (child / ".xysy.json").exists():  # a project made before the rename  # KEEP-OLD-NAME
                marker = child / ".xysy.json"  # KEEP-OLD-NAME
            if child.is_dir() and marker.exists():
                try:
                    meta = json.loads(marker.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
                runs = child / "runs"
                out.append({"name": child.name, "dir": str(child),
                            "workflow": meta.get("workflow") or child.name,
                            "runs": len(list(runs.iterdir())) if runs.is_dir() else 0})
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "projects": out}


def _job_open(args: dict) -> dict:
    """Show a folder to the person, or start an application. Both are one-way and harmless.

    The path is still jailed: a door reachable from a web page does not get to open
    arbitrary files on somebody's disk just because opening is not writing.
    """
    app = str(args.get("app") or "").strip()
    target = str(args.get("path") or "").strip()
    if app:
        try:
            # XY-DOORWIDE - focusOnly means "front it ONLY if it is already running, never
            # resurrect one the person quit". The page uses it to follow the active canvas
            # node; a door that answered it by LAUNCHING the app would pop applications
            # open under the person's cursor as they click around a workflow.
            if args.get("focusOnly"):
                if os.name == "nt":
                    return {"ok": True, "focused": False, "note": "focusOnly is a no-op on Windows"}
                script = 'if application "%s" is running then tell application "%s" to activate' % (
                    app.replace('"', ''), app.replace('"', ''))
                subprocess.run(["osascript", "-e", script], capture_output=True, timeout=20)
                return {"ok": True, "focused": True, "app": app}
            if os.name == "nt":
                subprocess.Popen(["cmd", "/c", "start", "", app], close_fds=True)
            else:
                subprocess.run(["open", "-a", app], capture_output=True, timeout=20)
            return {"ok": True, "launched": app}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
    if not target:
        return {"ok": False, "error": "open what?"}
    p = Path(target)
    if not _inside_projects(p):
        return {"ok": False, "error": "that is not inside a XYCY project"}
    try:
        subprocess.run(["explorer" if os.name == "nt" else "open", str(p)],
                       capture_output=True, timeout=20)
        return {"ok": True, "opened": str(p)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


MAC_INVENTORY = """
for p in /Applications/*.app /Applications/*/*.app /Applications/*/*/*.app "$HOME"/Applications/*.app; do
  [ -d "$p" ] || continue
  v=$(/usr/bin/defaults read "$p/Contents/Info" CFBundleShortVersionString 2>/dev/null)
  [ -n "$v" ] || v=$(/usr/bin/defaults read "$p/Contents/Info" CFBundleVersion 2>/dev/null)
  n=$(basename "$p"); n=${n%.app}
  printf "%s\t%s\n" "$n" "$v"
done
"""


def _job_inventory(_args: dict) -> dict:
    """What creative software this computer actually owns, with versions.

    The difference between "that application is not connected" and "you do not own that
    application" — two sentences XYCY must never mix up. Read-only: nothing is launched.
    """
    try:
        if os.name == "nt":
            ps = ('Get-ItemProperty HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, '
                  'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* '
                  '| Where-Object {$_.DisplayName} '
                  '| ForEach-Object { "{0}`t{1}" -f $_.DisplayName,$_.DisplayVersion }')
            raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=120).stdout
        else:
            raw = subprocess.run(["sh", "-c", MAC_INVENTORY],
                                 capture_output=True, text=True, timeout=120).stdout
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    apps = []
    for line in (raw or "").splitlines():
        if "\t" in line:
            name, _, version = line.partition("\t")
            if name.strip():
                apps.append({"name": name.strip(), "version": version.strip() or None})
    return {"ok": True, "count": len(apps), "apps": apps}


def _job_stage_skills(args: dict) -> dict:
    """Write a run's skills into the project so the harness loads them.

    Two homes on purpose: `.claude/skills/` is what XYCY has always written and what the
    Claude path reads; the per-run Hermes profile symlinks that same folder in at start
    time. One source of truth, two readers.
    """
    d = Path(str(args.get("dir") or ""))
    if not _inside_projects(d):
        return {"ok": False, "error": "that is not a XYCY project folder"}
    staged = []
    for skill in (args.get("skills") or []):
        sid = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(skill.get("id") or "")).strip("-")
        body = str(skill.get("body") or "")
        if not sid or not body:
            continue
        folder = d / ".claude" / "skills" / sid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(body, encoding="utf-8")
        staged.append(sid)
    return {"ok": True, "staged": staged}

# ------------------------------------------------------- XY-DOORSETUP - the setup screen
# Hermes' six-step setup screen used to be answerable only by the XYCY Bridge, which
# is a Claude Desktop extension: with Claude closed the screen said "XYCY can't see this
# Mac" and stopped there. Steps 3-6 ask about HERMES, and this file runs inside Hermes, so
# the door answers them and Claude is not needed on the machine at all.
#
# Steps 1 and 2 - install Hermes, install the model server - are NOT here, and it is not an
# oversight that can be corrected later: a door hosted by Hermes cannot report on whether
# Hermes is installed. Those two belong to the person, and the screen now says so plainly
# instead of offering a button that only works when Claude happens to be open.
#
# The shape of every answer below matches the Bridge's tool of the same name, because
# the SAME runner produces it. The UI must not be able to tell which door it came through -
# if it could, every screen would grow a second dialect. `via` is for humans reading logs.

MIN_CONTEXT = 64000   # keep in step with HERMES_MIN_CONTEXT in the Bridge's index.js

# What a model id is allowed to look like. The door is reachable from a web page, so the id
# is treated as hostile input even though nothing here goes near a shell: fixed argv,
# shell=False. This bounds it to the shape Ollama and friends actually use.
_MODEL_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")


def _shaped(out: dict) -> dict:
    """Every answer is a dict carrying minContext. A runner that returned something odd is a
    failure, not a silent empty success - the UI reads absence as 'not installed'."""
    if not isinstance(out, dict):
        return {"ok": False, "error": "the runner gave an answer XYCY could not read",
                "minContext": MIN_CONTEXT, "via": "door"}
    out["minContext"] = MIN_CONTEXT
    out["via"] = "door"
    return out


def _job_local_models(_args: dict) -> dict:
    """Every model installed on this computer, with the ones too small for Hermes marked.

    Marked rather than hidden, on purpose: a model missing from the list becomes a support
    question, while one shown greyed out with its own reason answers itself.
    """
    return _shaped(_run_helper(["models"], timeout=90.0))


def _job_hermes_status(args: dict) -> dict:
    """Is Hermes ready? Answered by MAKING IT REASON - never by a file being present.

    A run must be TOLD its model or Hermes falls back to its own default provider and dies
    in about a second on a service the person has no account with. So the configured model
    is passed explicitly, exactly as _job_think does.
    """
    timeout = float(args.get("timeout") or 180)
    flags = dict(_hermes_model())
    # A caller may override, but it may not send the same flag twice - argparse would take
    # the last one and which that is would depend on dict order.
    for key, flag in (("model", "--model"), ("provider", "--provider"),
                      ("baseUrl", "--base-url"), ("contextLength", "--context-length")):
        if args.get(key):
            flags[flag] = str(args[key])
    argv = ["probe", "--timeout", str(int(timeout))]
    for flag, val in flags.items():
        argv += [flag, str(val)]
    return _shaped(_run_helper(argv, timeout=timeout + 60.0))


def _job_set_model(args: dict) -> dict:
    """Point Hermes at one of the models already on this computer.

    THREE keys, not one. `model.default` is the model. `model.ollama_num_ctx` is what Ollama
    actually allocates, and Hermes checks that one - setting only the model is how a model
    with a 40,960-token window passed a 64k readiness check. `model.context_length` is what
    Hermes BELIEVES the window is, and it was not being written at all: XY-FULLCONTEXT2 found
    it left at 65,536 on Sean's PC while ollama_num_ctx had just been raised to 262,144. Two
    numbers for one fact, disagreeing, is the exact trap the paragraph above is about, and
    this had fallen into it one key along.

    Written through `hermes config set` when the CLI is on PATH, so Hermes' own writer owns
    the file and our comments and formatting survive. The YAML fallback exists for a Hermes
    installed somewhere PATH does not reach.
    """
    model = str(args.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "no model was named"}
    if not _MODEL_ID_OK.match(model):
        return {"ok": False, "error": "that does not look like a model name"}
    try:
        ctx = int(args.get("contextLength") or 65536)
    except (TypeError, ValueError):
        ctx = 65536
    ctx = max(MIN_CONTEXT, min(ctx, 1048576))

    exe = shutil.which("hermes")
    if exe:
        pairs = (("model.default", model), ("model.ollama_num_ctx", str(ctx)),
                 ("model.context_length", str(ctx)))
        for key, val in pairs:
            proc = subprocess.run([exe, "config", "set", key, val],
                                  capture_output=True, text=True, timeout=45)
            if proc.returncode != 0:
                return {"ok": False, "error": "Hermes would not accept that setting",
                        "detail": ((proc.stderr or proc.stdout or "")[-300:])}
        return {"ok": True, "model": model, "contextLength": ctx, "via": "door"}

    # Fallback: edit the config ourselves. Round-tripping YAML loses comments, so this is
    # second choice, not first.
    home = hermes_home()
    cfg = home / "config.yaml"
    try:
        import yaml
    except Exception:
        return {"ok": False, "error": "the hermes command is not on PATH and PyYAML is missing"}
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"ok": False, "error": "could not read Hermes' config"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "Hermes' config is not in the expected shape"}
    model_block = data.get("model")
    if not isinstance(model_block, dict):
        model_block = {}
    model_block["default"] = model
    model_block["ollama_num_ctx"] = ctx
    model_block["context_length"] = ctx
    data["model"] = model_block
    try:
        tmp = cfg.with_suffix(".yaml.xycy-tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        tmp.replace(cfg)
    except Exception as exc:
        return {"ok": False, "error": "could not save Hermes' config: " + str(exc)[:200]}
    return {"ok": True, "model": model, "contextLength": ctx, "via": "door", "wrote": "yaml"}


def _job_cloud_link_clear(_args: dict) -> dict:
    """Forget the account this computer is paired with.

    This exists because of the answer above. Once the setup screen may report "signed in" on the
    strength of THIS pairing, it has to be able to undo it here too — otherwise Sign out clears the
    extension's copy, the door quietly keeps its own, and the screen flips back to signed-in on the
    next check. A control that appears to work and silently does not is worse than no control.

    Every later call 401s, which is correct: the pairing is what authorises them.
    """
    state = _load()
    email = state.get("email") or ""
    _save({})
    return {"ok": True, "cleared": bool(email), "email": email, "via": "door"}


def _job_cloud_link_status(_args: dict) -> dict:
    """Whether this computer is connected to a XYCY account.

    Only a paired caller can reach any job at all, so in practice this always answers yes -
    it exists so the setup screen's account step reads the same field whichever door
    answered, rather than growing a second branch for the Hermes case.
    """
    state = _load()
    token = state.get("token") or ""
    expires = float(state.get("expiresAt") or 0)
    linked = bool(token) and expires / 1000.0 > time.time()
    return {"ok": True, "linked": linked, "signedOut": bool(token) and not linked,
            "email": state.get("email") or "", "expiresAt": int(expires), "via": "door"}


# --------------------------------------------------------------- XY-DOORPARITY jobs
def _job_run_progress(args: dict) -> dict:
    """The run's own progress ledger, byte-identical in shape to the Bridge's.

    Read straight off disk and never synthesised. The shape matters more than it looks:
    the page treats a FAILED call as {status:'error', steps:[]}, so a door that could not
    answer this made every finished run look like a crash that produced nothing. A run
    folder with no progress.json yet is 'queued', not an error - that distinction is the
    entire fix.
    """
    d = Path(str(args.get("dir") or ""))
    run_id = str(args.get("runId") or "")
    if not run_id or not _inside_projects(d):
        return {"ok": True, "run": None}
    # XY-RUNSEE - optionally the run's PLAN too. A browser that did not start this run holds no
    # copy of the workflow, so without the plan the only name it can give a step is its id. The
    # file sits in the same run folder as progress.json; this reaches no further than we already do.
    plan = None
    if args.get("plan"):
        pf = d / "runs" / run_id / "plan.json"
        if pf.is_file():
            try:
                p = json.loads(pf.read_text(encoding="utf-8"))
                if isinstance(p, dict) and p.get("nodes"):
                    plan = p
            except Exception:
                plan = None
    f = d / "runs" / run_id / "progress.json"
    if not f.is_file():
        return {"ok": True, "run": {"runId": run_id, "status": "queued", "steps": []}, "plan": plan}
    try:
        return {"ok": True, "run": json.loads(f.read_text(encoding="utf-8")), "plan": plan}
    except Exception as exc:
        # Half-written by a run that is mid-save. 'running' with no steps is honest;
        # 'error' would be a verdict the door is not entitled to reach.
        return {"ok": True, "run": {"runId": run_id, "status": "running", "steps": []},
                "plan": plan, "softError": str(exc)[:200]}


_IMAGE_TYPES = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif",
                "webp": "webp", "bmp": "bmp"}


def _job_read_image(args: dict) -> dict:
    """One picture a run saved, as a data URL, so a step thumbnail can show the real thing.

    Confined to the projects folder like everything else here - this is the door's only
    job that hands back file CONTENT a person did not name in a run, so the boundary is
    the whole security story.
    """
    raw = str(args.get("path") or "").strip()
    if not raw:
        return {"ok": False, "error": "no path"}
    base = Path(str(args.get("dir") or ""))
    target = Path(raw) if os.path.isabs(raw) else (base / raw.lstrip("./\\"))
    if ".." in raw or not _inside_projects(target):
        return {"ok": False, "error": "that picture is not inside a XYCY project"}
    if not target.is_file():
        return {"ok": False, "error": "not found: %s" % raw}
    size = target.stat().st_size
    if size > 12 * 1024 * 1024:
        return {"ok": False, "error": "too large"}
    ext = target.suffix.lower().lstrip(".")
    mime = _IMAGE_TYPES.get(ext)
    if not mime:
        return {"ok": False, "error": "not a picture"}
    import base64
    return {"ok": True, "bytes": size, "path": str(target),
            "dataUrl": "data:image/%s;base64,%s"
                       % (mime, base64.b64encode(target.read_bytes()).decode())}


def _job_runs(_args: dict) -> dict:
    """Every run still alive on this computer, so one that outlived the page can be re-adopted.

    The Bridge keeps a registry; the door has none and must not invent one, so this
    reads the ground truth each run already writes: hermes.pid beside progress.json. A pid
    that is gone means the run is gone - it is not reported as finished, because the door
    does not know that, and a run that died is not a run that succeeded.
    """
    runs = []
    try:
        for proj in sorted(PROJECTS_ROOT.iterdir()):
            rdir = proj / "runs"
            if not proj.is_dir() or not rdir.is_dir():
                continue
            for run in sorted(rdir.iterdir()):
                pidfile = run / "hermes.pid"
                if not run.is_dir() or not pidfile.is_file():
                    continue
                try:
                    pid = int(pidfile.read_text(encoding="utf-8").strip())
                    # XY-KILLNOTASK - this was os.kill(pid, 0), the POSIX "are you there"
                    # idiom, which on Windows is TerminateProcess on some Python versions.
                    # Listing the runs must never be able to end one. See the note beside
                    # pid_is_alive in xycy_hermes_run.py, measurement included.
                    if not _pid_is_alive(pid):
                        continue
                except Exception:
                    continue
                prog, status = None, "running"
                try:
                    prog = json.loads((run / "progress.json").read_text(encoding="utf-8"))
                    status = prog.get("status") or status
                except Exception as _xy_e:
                    say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:1490')
                steps = (prog or {}).get("steps") or []
                runs.append({"runId": run.name, "pid": pid, "dir": str(proj),
                             "name": (prog or {}).get("name") or "",
                             "started": None, "status": status,
                             "stepsDone": len([s for s in steps if (s or {}).get("status") == "done"]),
                             "stepsTotal": len(steps),
                             "kind": "workflow" if prog else "system"})
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "runs": runs}


def _job_set_runtime(args: dict) -> dict:
    """Remember which harness drives workflows, where the runner also looks."""
    name = str(args.get("runtime") or "").strip()
    if not name:
        return {"ok": False, "error": "which runtime?"}
    try:
        root = Path(os.path.expanduser("~/.xycy"))
        root.mkdir(parents=True, exist_ok=True)
        (root / "runtime.json").write_text(
            json.dumps({"runtime": name, "at": int(time.time() * 1000)}, indent=2),
            encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "runtime": name}

JOBS = {"ping": _job_ping, "apps": _job_apps, "servers": _job_servers,
        "capture": _job_capture,
        "project_create": _job_project_create, "run_write": _job_run_write,
        "run_start": _job_run_start, "run_status": _job_run_status,
        "run_stop": _job_run_stop, "read_output": _job_read_output,
        "system": _job_system, "think": _job_think,
        "server_verify": _job_server_verify, "projects": _job_projects,
        # XY-DOORCALL - the Hermes half of XY-CALLTOOL: hand a script to an application
        # with Claude Desktop closed. App name and tool name only, never a command line.
        "call_tool": _job_call_tool,
        "open": _job_open, "inventory": _job_inventory,
        "stage_skills": _job_stage_skills,
        # XY-DOORSETUP - the setup screen, answerable with Claude closed
        "local_models": _job_local_models, "hermes_status": _job_hermes_status,
        "set_model": _job_set_model, "cloud_link_status": _job_cloud_link_status,
        "cloud_link_clear": _job_cloud_link_clear,
        # XY-DOORPARITY - reporting a run honestly, with Claude nowhere on the machine
        "run_progress": _job_run_progress, "read_image": _job_read_image,
        "runs": _job_runs, "set_runtime": _job_set_runtime}


# --------------------------------------------------------------------------- door
class _Door(BaseHTTPRequestHandler):
    server_version = "XYCYDoor/" + DOOR_VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):  # keep Hermes' log readable
        pass

    # -- helpers ----------------------------------------------------------------
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        # No Origin is NOT a free pass — see rule 3 in this file's header. It only
        # means there is no origin to echo; the token check still runs.
        return origin is None or origin in ALLOWED_ORIGINS

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "false")
        self.send_header("Vary", "Origin")

    def _say(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _paired_caller(self) -> tuple[bool, str]:
        """Is this the account this computer was paired with?"""
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return False, "no key"
        token = auth[7:].strip()
        state = _load()
        held = state.get("token") or ""
        if not held:
            return False, "this computer is not connected to a XYCY account"
        # Constant time: a plain == leaks how many leading characters were right.
        if not hmac.compare_digest(token, held):
            return False, "that key is not the one this computer is paired with"
        if float(state.get("expiresAt") or 0) / 1000.0 <= time.time():
            return False, "that key has expired — connect again"
        return True, state.get("email") or ""

    # -- verbs ------------------------------------------------------------------
    def do_OPTIONS(self):
        if not self._origin_ok():
            self._say(403, {"ok": False, "error": "this door does not open for " + str(self.headers.get("Origin"))})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type,authorization")
        self.send_header("Access-Control-Max-Age", "600")
        # A PUBLIC page (xycy.ai) reaching a LOCAL address (127.0.0.1) is a private-network
        # request: the browser asks permission on the preflight and treats a missing answer as
        # a refusal. Answering 204 with the CORS headers alone is therefore a NO, and the page
        # sees "Failed to fetch" - indistinguishable from no door being here at all.
        # Only ever answered for an origin already on the allowlist: _origin_ok() ran above,
        # so this widens nothing that was not already permitted to knock.
        if (self.headers.get("Access-Control-Request-Private-Network") or "").lower() == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def _send_bytes(self, code: int, raw: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._origin_ok():
            self._say(403, {"ok": False, "error": "this door does not open for that site"})
            return
        path = self.path.split("?")[0]

        if path == "/xycy/hello":
            # Says only that a door exists and whether it is spoken for. Deliberately
            # nothing about the person, the computer, or what is installed.
            state = _load()
            self._say(200, {"ok": True, "door": "xycy", "version": DOOR_VERSION,
                            "host": "hermes", "paired": bool(state.get("token")),
                            "email": state.get("email") or ""})
            return

        # XY-DOORUI - the screen itself, so /xycy works with Claude closed.
        if path in ("/", "/xycy", "/index.html") or path == "/xysy":  # the last is the old name's path  # KEEP-OLD-NAME
            try:
                _refresh_ui()
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:1638')
            if UI_FILE.exists():
                try:
                    self._send_bytes(200, UI_FILE.read_bytes(), "text/html")
                    return
                except Exception as _xy_e:
                    say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:1644')
            self._send_bytes(503, _no_screen_page(), "text/html")
            return

        # XY-DOORUI - the key handover, for the page WE serve and nobody else.
        # The page's _doorConnect() fetches '/api/runkey' from its own origin; on
        # xycy.ai a Pages function answers with the session's key. Here the door
        # answers with the key this computer is already paired with — a fact any
        # local process could read from disk anyway, so this opens nothing new.
        # A cross-origin caller (even an allowed one) is refused: xycy.ai pages
        # have their own /api/runkey, and this one is not for them.
        if path == "/api/runkey":
            origin = self.headers.get("Origin")
            if origin is not None and origin not in SELF_ORIGINS:
                self._say(403, {"ok": False, "error": "this key is only for the door's own page"})
                return
            state = _load()
            token = state.get("token") or ""
            fresh = float(state.get("expiresAt") or 0) / 1000.0 > time.time()
            if token and not fresh:
                # Expired pairing: ask the site to roll it, exactly as /xycy/pair does.
                who = _verify_upstream(token)
                if who:
                    _save({"email": who["email"], "token": who.get("token") or token,
                           "expiresAt": who.get("expiresAt") or 0,
                           "pairedAt": state.get("pairedAt") or int(time.time() * 1000),
                           "checkedAt": int(time.time())})
                    state = _load()
                    token, fresh = state.get("token") or "", True
            # Deliberately NO fallback to the Claude extension's cloud_link.json
            # here: pairing with that key would roll it upstream and silently
            # strand the extension on a dead credential. The door hands out only
            # what is the door's to hand out.
            if token and fresh:
                self._say(200, {"ok": True, "token": token,
                                "expiresAt": int(float(state.get("expiresAt") or 0))})
            else:
                self._say(200, {"ok": False, "error": "this computer is not connected to a XYCY account"})
            return

        # XY-DOORUI - static files the screen asks for, from the same cache dir.
        # Jailed: resolve under UI_DIR or refuse; dotfiles and the door's own
        # state never live here.
        if UI_DIR.exists():
            rel = path.lstrip("/")
            if rel and ".." not in rel and not rel.startswith("."):
                cand = (UI_DIR / rel)
                try:
                    inside = str(cand.resolve()).startswith(str(UI_DIR.resolve()) + os.sep)
                except Exception:
                    inside = False
                if inside and cand.is_file():
                    ext = cand.suffix.lstrip(".").lower()
                    self._send_bytes(200, cand.read_bytes(),
                                     _CTYPES.get(ext, "application/octet-stream"))
                    return

        self._say(404, {"ok": False, "error": "no such thing here"})

    def do_POST(self):
        if not self._origin_ok():
            self._say(403, {"ok": False, "error": "this door does not open for that site"})
            return
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 2_000_000:
            self._say(413, {"ok": False, "error": "too much"})
            return
        try:
            body = json.loads((self.rfile.read(length) or b"{}").decode("utf-8"))
        except Exception:
            body = {}

        if path == "/xycy/pair":
            token = str(body.get("token") or "")
            if not token:
                self._say(400, {"ok": False, "error": "no key was handed over"})
                return
            who = _verify_upstream(token)
            if not who:
                # One message for forged, expired and unreachable on purpose: a
                # caller who can tell them apart is a caller probing the lock.
                self._say(401, {"ok": False, "error": "XYCY did not recognise that key"})
                return
            _save({"email": who["email"], "token": who.get("token") or token,
                   "expiresAt": who.get("expiresAt") or 0,
                   "pairedAt": int(time.time() * 1000), "checkedAt": int(time.time())})
            # XY-DOORSETUP - hand back the key we actually STORED. _verify_upstream may
            # return a fresh one, and until now the caller kept using the key it sent: the
            # pairing looked like it worked and every call after it answered "that key is
            # not the one this computer is paired with". Costs nothing to return - it is
            # the caller's own account key, and it just proved it holds one.
            saved = _load()
            self._say(200, {"ok": True, "email": who["email"],
                            "key": saved.get("token") or token,
                            "expiresAt": int(saved.get("expiresAt") or 0)})
            return

        if path == "/xycy/call":
            ok, who = self._paired_caller()
            if not ok:
                self._say(401, {"ok": False, "error": who})
                return
            job = str(body.get("job") or "")
            fn = JOBS.get(job)
            if not fn:
                self._say(404, {"ok": False, "error": "XYCY's door cannot do '%s'" % job})
                return
            try:
                self._say(200, fn(body.get("args") or {}))
            except Exception as exc:
                self._say(500, {"ok": False, "error": str(exc)[:300]})
            return

        self._say(404, {"ok": False, "error": "no such thing here"})


_server = None
_thread = None
# Why the door is not open, when it is not. Empty means "no reason recorded" — which, after
# start_door() has run, means it opened.
_door_error = ""


def _say(msg: str) -> None:
    """One line to stderr, so it reaches `hermes serve`'s log. Never raises."""
    try:
        sys.stderr.write("[xycy-door] " + msg + "\n")
        sys.stderr.flush()
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:1777')


def start_door() -> None:
    """Open the door once, in the background, and never take Hermes down with it."""
    global _server, _thread, _door_error
    if _server is not None:
        return
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", DOOR_PORT), _Door)
    except OSError as exc:
        # Usually "already listening" — a second Hermes profile, or a reload — and that is
        # harmless, because the door that IS up is serving. But it can equally be a refused
        # bind, and returning silently there is how a computer ends up with a running Hermes,
        # an enabled plugin, no door, and nothing anywhere saying why. Say it either way.
        _server = None
        _door_error = "could not bind 127.0.0.1:%d - %s" % (DOOR_PORT, exc)
        _say(_door_error)
        return
    _server.daemon_threads = True
    _thread = threading.Thread(target=_server.serve_forever, name="xycy-door", daemon=True)
    _thread.start()
    _door_error = ""
    _say("listening on 127.0.0.1:%d (door %s)" % (DOOR_PORT, DOOR_VERSION))



# ---------------------------------------------------------------------------------------------
# XY-DOORROOT - the door makes itself PERMANENT (v0.5.0).
#
# Sean, 2026-08-11, from the second Mac: the setup text ended with `hermes serve` running in a
# Terminal window, so XYCY on that machine lived exactly as long as the window stayed open.
# Closing it looked like "xycy.ai forgot this computer". A person should paste the setup ONCE
# and never think about it again - no window to keep open, back by itself after a restart.
#
# So the first time the door runs on a Mac that has no launchd job, it writes one:
# ~/Library/LaunchAgents/ai.xycy.hermes-serve.plist - the same job already proven on the first
# Mac. The job's command SLEEPS briefly, then WAITS for Hermes' own port (9119) to be free
# before starting `hermes serve`, so the copy launchd manages never fights a serve the person
# started by hand; it simply takes over whenever that one goes away (window closed, crash,
# reboot).
#
# Deliberately NOT done:
#   * never overwrite an existing plist - the first Mac's hand-written job stays untouched;
#   * nothing on Windows/Linux yet - Hermes on Windows is itself unrun; this returns a note
#     instead of guessing at Scheduled Tasks;
#   * no kickstart of the new job now - the serve this door lives in is already the server.
#
# XYCY_PERSIST_NO_LOAD=1 skips the launchctl registration - a test hook, so a test with a
# scratch HOME can prove the plist without planting a job in the REAL launchd session.

PERSIST_LABEL = "ai.xycy.hermes-serve"
_persist_state = ""


def _hermes_serve_argv():
    """How THIS machine starts `hermes serve` - the ~/.hermes venv install first (what the
    proven first-Mac plist runs), then whatever `hermes` is on PATH."""
    agent = hermes_home() / "hermes-agent"
    py, hm = agent / "venv" / "bin" / "python", agent / "hermes"
    if py.exists() and hm.exists():
        return [str(py), str(hm), "serve", "--skip-build"]
    w = shutil.which("hermes")
    if w:
        return [w, "serve", "--skip-build"]
    return None


def _persist_plist_xml(argv):
    import shlex
    home = str(Path.home())
    log = str(hermes_home() / "logs" / "serve-launchd.log")

    def x(t):
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Sleep first so a serve that is starting RIGHT NOW (the one loading this plugin) gets to
    # bind 9119 before we look; then wait until the port is free, then BECOME the server.
    cmd = ("/bin/sleep 5; while /usr/bin/nc -z 127.0.0.1 9119 2>/dev/null; do /bin/sleep 30; "
           "done; exec " + " ".join(shlex.quote(a) for a in argv))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>%s</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        "    <string>/bin/sh</string>\n    <string>-c</string>\n    <string>%s</string>\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key><dict>\n"
        "    <key>PATH</key><string>%s/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>\n"
        "    <key>HOME</key><string>%s</string>\n"
        "  </dict>\n"
        "  <key>WorkingDirectory</key><string>%s</string>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>\n"
        "  <key>StandardOutPath</key><string>%s</string>\n"
        "  <key>StandardErrorPath</key><string>%s</string>\n"
        "</dict></plist>\n"
    ) % (PERSIST_LABEL, x(cmd), x(home), x(home), x(home), x(log), x(log))


def ensure_persistence():
    """Make `hermes serve` - and with it this door - outlive the window that started it.
    Returns a short note of what happened; never raises past its caller's wrap."""
    if sys.platform != "darwin":
        return "not-macos"
    plist = Path.home() / "Library" / "LaunchAgents" / (PERSIST_LABEL + ".plist")
    if plist.exists():
        return "already-persistent"
    argv = _hermes_serve_argv()
    if not argv:
        return "hermes-not-found"
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        tmp = plist.with_suffix(".plist.part")
        tmp.write_text(_persist_plist_xml(argv), encoding="utf-8")
        tmp.replace(plist)
    except Exception as exc:
        return "could-not-write - %s" % exc
    if not os.environ.get("XYCY_PERSIST_NO_LOAD"):
        try:
            subprocess.run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(plist)],
                           capture_output=True, timeout=10)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:1902')
    return "installed"


# Hermes imports this module at startup when the plugin is enabled, so this is our
# only hook. Wrapped because a plugin must never be the reason `hermes serve` fails.
try:
    start_door()
except Exception as _exc:                      # noqa: BLE001 - a plugin must never break serve
    _door_error = "the door did not start - %s: %s" % (type(_exc).__name__, _exc)
    _say(_door_error)
    try:
        import traceback as _tb
        _tb.print_exc()
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/hermes-plugin/xycy/dashboard/api.py:1917')

# The door is up (or said why not); now make sure it OUTLIVES the process it lives in.
try:
    _persist_state = ensure_persistence()
    if _persist_state != "already-persistent":
        _say("persistence: " + _persist_state)
except Exception as _pexc:  # noqa: BLE001 - a plugin must never break serve
    _persist_state = "failed - %s: %s" % (type(_pexc).__name__, _pexc)
    _say("persistence: " + _persist_state)

# Hermes also wants a router. Ours adds nothing to Hermes' own screens; it exists so
# the plugin loads at all, and so `hermes serve`'s own UI can show the door's state.
if APIRouter is not None:
    router = APIRouter()

    @router.get("/status")
    async def status():
        state = _load()
        return {"ok": True, "door": "xycy", "version": DOOR_VERSION, "port": DOOR_PORT,
                "listening": _server is not None,
                # The whole point: a door that is not listening can be ASKED why, from Hermes'
                # own screens, without anybody reading a log.
                "error": _door_error,
                "persistent": _persist_state in ("already-persistent", "installed"),
                "paired": bool(state.get("token")), "email": state.get("email") or ""}
else:  # pragma: no cover
    router = None


# ---------------------------------------------------------------------------------------------
# Run the door BY HAND and watch it:
#
#     ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/plugins/xycy/dashboard/api.py
#
# `hermes serve` starts the door in a daemon thread wrapped so a plugin can never break serve.
# That is correct, and it means a door that fails to open is invisible. This is the way to see it.
if __name__ == "__main__":
    print("")
    print("  XYCY door %s" % DOOR_VERSION)
    print("  port        : %d" % DOOR_PORT)
    print("  state file  : %s" % STATE)
    print("  hermes home : %s" % hermes_home())
    print("")
    # start_door() already ran at import. This either returns immediately (already open) or
    # retries and records why not.
    start_door()
    if _server is None:
        print("  THE DOOR DID NOT OPEN")
        print("  %s" % (_door_error or "no reason recorded"))
        print("")
        print("  If it says the address is in use, something already holds the port - which may")
        print("  well be a door that is working. Check with:")
        print("      curl -s http://127.0.0.1:%d/xycy/hello" % DOOR_PORT)
        raise SystemExit(1)
    print("  Listening. Ask it from another window with:")
    print("      curl -s http://127.0.0.1:%d/xycy/hello" % DOOR_PORT)
    print("")
    print("  Ctrl+C to stop. (Hermes runs this for you normally - this is only for looking.)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  stopped.")
