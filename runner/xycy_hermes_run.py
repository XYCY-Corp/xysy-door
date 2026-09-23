#!/usr/bin/env python3
"""xycy_hermes_run — launch a XYCY workflow run on the Hermes Agent harness.

This is the Hermes counterpart of `tRunCli` in local-agent/mcpb/server/index.js.
Same job, same return shape ({ok, pid, runId, ...} as one line of JSON on stdout),
so wiring it up is a new branch in the agent's run tool rather than a new runtime.

    xycy_hermes_run.py start  --dir <project> --run-id <id> [--servers a,b] [...]
    xycy_hermes_run.py status --dir <project> --run-id <id>
    xycy_hermes_run.py stop   --dir <project> --run-id <id>

WHAT MAPS TO WHAT (verified against Hermes v0.20.0)

    claude -p <prompt>                      hermes -z <prompt>
    --add-dir <dir>                         --in <dir>
    --model <m>                             -m <m>
    --permission-mode bypassPermissions     --yolo   (+ --accept-hooks, headless)
    --mcp-config runs/<id>/mcp.json         HERMES_HOME=<per-run home>   <-- see below
    --output-format stream-json --verbose    the xycy_progress plugin's NDJSON feed
    (no equivalent)                         -t <toolsets>, --worktree, --usage-file

WHY A PER-RUN HERMES_HOME
    Hermes has no per-invocation MCP config flag; servers come from
    $HERMES_HOME/config.yaml. Pointing HERMES_HOME at a directory this script
    generates buys three things at once:
      1. the per-variant env isolation XYCY needs (OS_MCP_APP, RHINO_MCP_PORT …),
      2. an allowlist — only the servers THIS run needs get started, so a run
         never fires up `autodesk` (npx mcp-remote → an OAuth browser window) or
         six uvx app servers it has no use for,
      3. a private plugins/ dir, so the instrumentation is scoped to XYCY runs
         and cannot alter the user's own `hermes` sessions.

Python 3.9+, standard library only. Runs on the user's machine next to the
Bridge, so it must not assume anything is installed but hermes itself.
"""
from __future__ import annotations

import argparse
import json
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse


# ------------------------------------------------------------------ XY-KILLNOTASK
# Ask whether a process is running, without any chance of touching it.
#
# This was `os.kill(pid, 0)` everywhere - the POSIX idiom for "are you there", where signal 0
# is delivered to nobody and only the existence and permission checks run. Windows has no
# signals, and CPython implements os.kill there as OpenProcess plus TerminateProcess for every
# signal but the two console ones. Zero has not always been excluded from that, so on some
# Python versions this idiom KILLS what it asks about, with exit code 0, and then returns as
# though the answer were yes.
#
# MEASURED, so the claim is not bigger than the evidence: on Sean's PC, Python 3.12.10, a child
# process SURVIVED os.kill(pid, 0) - that Python does not terminate on signal zero. So this was
# not the cause of the Hermes runs dying, and nothing here should be read as saying it was. It
# is still the wrong call to make: whether this line is a question or a killing depends on which
# Python happens to be on the machine, and this product runs on whichever one it finds. Five
# places asked it - the run status poll, Stop, the app-claim check, the setup probe and the
# dashboard's run list - and the page asks the first of them every 2.2 seconds while a run is on
# screen. A liveness check must not be a coin flip about a process somebody's work is inside.
#
# OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION asks and cannot touch. The one imprecision,
# stated rather than hidden: a process that exited with code 259 reads as alive, because 259 is
# also STILL_ACTIVE and Windows gives no way to tell those apart. Reporting a dead run as alive
# for a moment is the safe direction, and the heartbeat check beside this one settles it.
def pid_is_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            got = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(got) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


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


HOME = os.path.expanduser("~")
XYCY_ROOT = os.environ.get("XYCY_ROOT") or os.path.join(HOME, ".xycy")
REGISTRY = os.path.join(XYCY_ROOT, "registry.json")
RUN_HOMES = os.path.join(XYCY_ROOT, "hermes-runs")
PLUGIN_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xycy_progress")

# The Bridge's own MCP server is never handed to a run: the run is what the
# agent launched, and re-entering it invites a loop.
SELF_SERVERS = {"xycy-agent", "openstudio-agent", "xycy", "openstudio"}

# Hermes refuses to start on a model reporting under 64k context, and a XYCY run
# carries plan.json + a brief + skills before it does any work, so the ceiling
# matters more here than in chat. Overridable per run.
MIN_CONTEXT = 65536

# A 13-phase arch-viz run makes hundreds of tool calls. Hermes ships
# code_execution.max_tool_calls: 50, which would strand a real run mid-way.
DEFAULT_MAX_TOOL_CALLS = 2000


def die(message: str, **extra):
    print(json.dumps({"ok": False, "error": message, **extra}))
    sys.exit(1)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {} if default is None else default


def hermes_home():
    """Where Hermes keeps its own home folder.

    Hermes' own installer sets HERMES_HOME, so that wins whenever it is set. When it
    is not set the default is not the same on every operating system, and assuming
    ~/.hermes everywhere is how XYCY came to look in an empty folder: measured on
    8 September 2026, a Hermes Desktop install on Windows put its home in
    %LOCALAPPDATA%\\hermes and left ~/.hermes holding nothing at all.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return env
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
        return os.path.join(local, "hermes")
    return os.path.join(HOME, ".hermes")


def find_hermes():
    """GUI-launched parents inherit a stripped PATH — look where the installer puts it.

    Windows needs its own list AND the .exe/.cmd suffixes: a bare 'hermes' matches nothing there,
    so XYCY would report Hermes missing on a machine that has it — and, because the agent IS
    reachable on Windows, it would say so with confidence rather than admitting it cannot tell.
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
        local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
        names = ["hermes.exe", "hermes.cmd", "hermes.bat", "hermes"]
        candidates = [os.environ.get("HERMES_CLI")]
        candidates += [shutil.which(n) for n in names]
        for base in (os.path.join(hermes_home(), "bin"),
                     os.path.join(hermes_home(), "hermes-agent", "venv", "Scripts"),
                     os.path.join(HOME, ".local", "bin"),
                     os.path.join(HOME, ".hermes", "hermes-agent", "venv", "Scripts"),
                     os.path.join(local, "Programs", "Python", "Scripts"),
                     os.path.join(appdata, "Python", "Scripts"),
                     os.path.join(local, "Microsoft", "WindowsApps")):
            candidates += [os.path.join(base, n) for n in names]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return ""
    candidates = [
        os.environ.get("HERMES_CLI"),
        shutil.which("hermes"),
        os.path.join(hermes_home(), "bin", "hermes"),
        os.path.join(hermes_home(), "hermes-agent", "venv", "bin", "hermes"),
        os.path.join(HOME, ".local", "bin", "hermes"),
        os.path.join(HOME, ".hermes", "hermes-agent", "venv", "bin", "hermes"),
        "/usr/local/bin/hermes",
        "/opt/homebrew/bin/hermes",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def enriched_env():
    env = dict(os.environ)
    extra = [os.path.join(HOME, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    path = env.get("PATH", "")
    for entry in extra:
        if entry not in path.split(os.pathsep):
            path = path + os.pathsep + entry
    env["PATH"] = path
    return env


def select_servers(names, mcp_env):
    """Pick this run's servers out of the XYCY registry and apply env overrides.

    `names is None` means "not specified" → every app server in the registry.
    An empty LIST means "this run needs no app servers" and must stay empty:
    collapsing the two is how `--servers ''` ends up launching `autodesk` and
    opening an OAuth browser window on a run that wanted a text tool.
    """
    registry = load_json(REGISTRY).get("servers", {}) or {}
    if names is None:
        wanted = [n for n in registry.keys() if n not in SELF_SERVERS]
    else:
        wanted = [n for n in names if n]
    selected, missing = {}, []
    for name in wanted:
        entry = registry.get(name)
        if not entry:
            missing.append(name)
            continue
        # transport:'builtin' servers have no launch command — the same class of
        # bug that leaked ~/.xycy paths into exported packages. Skip, don't guess.
        if not entry.get("command"):
            continue
        server = {"command": entry["command"], "args": list(entry.get("args") or [])}
        env = dict(entry.get("env") or {})
        override = (mcp_env or {}).get(name) or (mcp_env or {}).get("*")
        if isinstance(override, dict):
            env.update(override)
        if env:
            server["env"] = env
        selected[name] = server
    return selected, missing


# XY-ONEDRIVER. 26 Aug, run_full0826: a second n1 run was started while the first was still
# alive, and for twenty minutes TWO Hermes sessions drove the same SketchUp. They wrote
# outputs/DiyaraTower_CityRender.png over each other and the LOSER's write is the one that
# survived - a blocked eye-level view, 1280x720, written at the moment its own verdict said
# `done`. Every check we own passed it: right name, right folder, non-zero, plausible
# dimensions. Nothing in XYCY prevented it, noticed it, or wrote it down.
#
# THE RULE: a deliverable is only attributable if exactly ONE run could have written it.
# Last-write-wins across concurrent runs defeats XY-DONEEMPTY, XY-OUTWROTE, XY-BLANKPAGE and
# XY-OUTPUTREAL at once, because each of them asks about the FILE and none of them asks who
# wrote it.
#
# A claim is a file per application. It is only honoured while the claiming process is ALIVE:
# a holder whose pid is gone is stale and is taken over, because a crashed run must never lock
# an application out for ever. That is the [[a-finished-run-that-says-it-is-running]] lesson -
# a status nobody recomputes goes stale and every reader repeats it as fact.
APP_HOLDERS = os.path.join(RUN_HOMES, "_holders")


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "server"))[:80] or "server"


def _holder_path(server):
    return os.path.join(APP_HOLDERS, "%s.json" % _safe_name(server))


def _live_holder(server, run_id=None):
    """The run that is driving this application right now, or None. Stale claims are cleared."""
    path = _holder_path(server)
    data = load_json(path)
    if not isinstance(data, dict) or not data.get("runId"):
        return None
    if run_id and data.get("runId") == run_id:
        return None                      # our own claim is not a conflict
    pid = data.get("pid")
    alive = pid_is_alive(pid) if isinstance(pid, int) else False   # XY-KILLNOTASK
    if not alive:
        try:
            os.remove(path)              # a dead run holds nothing
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:311')
        return None
    return data


def claim_apps(servers, run_id, pid=None):
    """Claim every declared application for this run. Returns the conflicts, if any."""
    conflicts = []
    for name in sorted((servers or {}).keys()):
        held = _live_holder(name, run_id)
        if held:
            conflicts.append((name, held))
    if conflicts:
        return conflicts
    os.makedirs(APP_HOLDERS, exist_ok=True)
    for name in sorted((servers or {}).keys()):
        try:
            with open(_holder_path(name), "w", encoding="utf-8") as handle:
                json.dump({"runId": run_id, "pid": int(pid or os.getpid()),
                           "startedAt": round(time.time(), 3)}, handle, indent=2)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:332')
    return []


def release_apps(servers, run_id):
    """Give the applications back. Only ever removes THIS run's own claim."""
    for name in list(servers or []):
        path = _holder_path(name)
        data = load_json(path)
        if isinstance(data, dict) and data.get("runId") == run_id:
            try:
                os.remove(path)
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:345')


# XY-APPDOC. "Is the application there?" is not the question. 26 Aug, run_full0826__n1:
# sketchup_get_status answered {connected: true, status: "running", bridge_version: "0.5.0"}
# while SketchUp had NO OPEN DOCUMENT, and the step spent 2:30 discovering that every call died
# on `undefined method 'start_operation' for nil:NilClass`. The bridge is alive because the
# PLUGIN is alive. Third sighting of the same shape in two days - the app not launched at all
# (25 Aug), the app with its main window gone (26 Aug midday), the app with no document (now).
#
# So the probe asks what the application HOLDS, and the rule is deliberately narrow: only an
# EXPLICIT "there is no document" refuses the step. A probe that times out, errors, or answers
# something we do not recognise is "I could not ask" and the run proceeds - the opposite of
# that is how XY-SERVEDONLY, XY-OUTWROTE and XY-BLANKPAGE all started.
#
# tool, args, and a predicate over the answer text that is TRUE only for a definite no.
DOC_PROBES = {
    "sketchup": ("sketchup_get_model_info", {},
                 lambda t: "no active model" in t.lower()),
    "rhino": ("get_document_summary", {},
              lambda t: "no document" in t.lower()),
    "freecad": ("list_documents", {},
                lambda t: False),   # an empty FreeCAD is legitimate: the payload makes the doc
    "blender": ("get_scene_info", {"user_prompt": "xycy document check"},
                lambda t: False),   # Blender always has a scene
}
DOC_PROBE_TIMEOUT = 20.0


def _mcp_ask(server, tool, args, timeout=DOC_PROBE_TIMEOUT):
    """Speak MCP over stdio to ONE server, call ONE read-only tool, return its text.

    Returns (text, error). Either may be None. Never raises: every failure is an "I could
    not ask", which the caller must not read as an answer.
    """
    argv = [server.get("command")] + list(server.get("args") or [])
    if not argv[0]:
        return None, "no launch command"
    env = dict(os.environ)
    env.update(server.get("env") or {})
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
    except Exception as exc:
        return None, str(exc)

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    deadline = time.time() + timeout

    def read_for(msg_id):
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == msg_id:
                return msg
        return None

    text, error = None, None
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "xycy-appdoc", "version": "1"}}})
        if read_for(1) is None:
            error = "no initialize response"
        else:
            send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": tool, "arguments": args or {}}})
            reply = read_for(2)
            if reply is None:
                error = "no answer from %s" % tool
            elif reply.get("error"):
                error = str(reply["error"])[:200]
            else:
                content = ((reply.get("result") or {}).get("content") or [])
                text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    except Exception as exc:
        error = str(exc)[:200]
    finally:
        try:
            proc.kill()
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:438')
    return text, error


# XY-INPUTLOADED. XY-APPDOC asks whether an application holds A document. It does not ask
# whether it holds THE RIGHT ONE, and on 27 Aug that difference cost a run: SketchUp was holding
# an empty untitled model where DiyaraTower_CityBlock.skp - the step's own declared City Context
# input, 15,487 entities - should have been. The step would have built a tower into an empty
# void, photographed the void, and reported success. Nothing anywhere in XYCY would have
# noticed; a person looking at the render would have.
#
# A declared input that is an application's OWN document type is a precondition, not a hint. So:
# read the node's inputFiles out of the plan, keep the ones this server can open, ask the
# application what document it is holding, and compare basenames.
#
# One-sided, like XY-APPDOC: only a definite mismatch refuses. A probe that could not run, an
# answer this cannot parse, an app whose document type is not in the table - all of those let
# the run proceed and are written into the record. "I could not ask" is never a yes and never a
# no.
APP_DOC_TYPES = {
    "sketchup": (".skp",),
    "rhino": (".3dm",),
    "blender": (".blend",),
    "freecad": (".fcstd",),
}


def _open_document_names(name, spec):
    """Every document path/name the application admits to holding. None means "could not ask"."""
    probe = DOC_PROBES.get(name)
    if not probe:
        return None
    text, error = _mcp_ask(spec, probe[0], probe[1])
    if not text or error:
        return None
    # VALUES only, never keys. The first cut regexed every quoted token and came back holding
    # {"bounds", "entitycount", "maxx", "miny", ...} - the JSON field names of the answer. The
    # verdict was still correct, because the wanted file genuinely was not there, but the
    # message it printed named the shape of the reply instead of the document, which is the kind
    # of diagnostic that sends the next person the wrong way for an hour.
    found = set()

    def _take(v):
        if isinstance(v, str) and v.strip():
            found.add(os.path.basename(v).lower())
            found.add(os.path.splitext(os.path.basename(v))[0].lower())
        elif isinstance(v, list):
            for x in v:
                _take(x)

    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
        for key in ("path", "title", "name", "FileName", "Label"):
            _take(data.get(key))
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:493')
    # FreeCAD's list_documents is a bare JSON array of document names
    try:
        arr = json.loads(text[text.index("["):text.rindex("]") + 1])
        _take(arr)
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:499')
    # and anything that simply looks like a document filename, wherever it appears
    for m in re.findall(r'[\w\-. ()]+\.(?:skp|3dm|blend|FCStd|fcstd)', text):
        _take(m)
    found.discard("")
    found.discard("untitled")      # an untitled document is not an identity
    return found


def check_declared_inputs_are_open(servers, project_dir, run_id):
    """DEFINITE mismatches between a node's declared input document and what the app holds."""
    node = plan_node_for(project_dir, run_id) or {}
    declared = (node.get("inputFiles") or {})
    server = node.get("server")
    mismatches, asked = [], []
    if not declared or not server or server not in (servers or {}):
        return mismatches, asked
    exts = APP_DOC_TYPES.get(server) or ()
    wanted = [os.path.basename(v) for v in declared.values()
              if isinstance(v, str) and v.lower().endswith(exts)]
    if not wanted:
        return mismatches, asked
    holding = _open_document_names(server, servers[server])
    record = {"server": server, "declared": wanted,
              "holding": sorted(holding) if holding is not None else None}
    asked.append(record)
    if holding is None:
        record["verdict"] = "could not ask"
        return mismatches, asked
    for w in wanted:
        stem = os.path.splitext(w)[0].lower()
        if w.lower() in holding or stem in holding:
            continue
        record["verdict"] = "NOT OPEN"
        mismatches.append((server, w, sorted(holding)[:6] or ["an untitled, unsaved document"]))
    record.setdefault("verdict", "ok")
    return mismatches, asked


def plan_node_for(project_dir, run_id):
    """The plan node this run is for. `<parent>__<node>`, with a trailing retry letter allowed."""
    parent, _, node_id = (run_id or "").partition("__")
    if not node_id:
        return None
    plan = load_json(os.path.join(project_dir, "runs", parent, "plan.json")) or {}
    nodes = plan.get("nodes") or []
    for n in nodes:
        if n.get("id") == node_id:
            return n
    # run_v7__n1b is node n1's second attempt - strip trailing letters, same as the skills filter
    base = node_id.rstrip("abcdefghijklmnopqrstuvwxyz") or node_id
    for n in nodes:
        if n.get("id") == base:
            return n
    return None


def check_apps_hold_a_document(servers):
    """Ask every declared application what it holds. Returns a list of DEFINITE refusals.

    One-sided on purpose - see the note on DOC_PROBES. The findings are also returned for the
    run record, so "I could not ask" is written down rather than silently treated as a yes.
    """
    refusals, asked = [], []
    for name, spec in (servers or {}).items():
        probe = DOC_PROBES.get(name)
        if not probe:
            continue
        tool, args, is_empty = probe
        text, error = _mcp_ask(spec, tool, args)
        record = {"server": name, "tool": tool,
                  "answered": bool(text), "error": error,
                  "excerpt": (text or "")[:200]}
        asked.append(record)
        if text and is_empty(text):
            record["holds"] = "nothing"
            refusals.append((name, tool, (text or "").strip()[:200]))
    return refusals, asked


# Built-in Hermes toolsets switched off while a run is driving an application. Each one is a
# way for the model to accomplish the task without touching the app, which both defeats the
# point and leaves the viewport empty. `file`, `vision`, `skills`, `todo` and `delegation`
# stay on: a step still has to read its plan, look at what it made, and write its outputs.
APP_RUN_DISABLED_TOOLSETS = ("terminal", "code_execution", "browser", "web",
                             "computer_use",
                             # XY-EAGERTOOLS, second half: `todo` is a private
                             # notebook the model writes to instead of acting. On a
                             # payload run there is nothing to plan - the calls are
                             # printed in the prompt - and each entry cost a whole
                             # turn: 17.6s of n4 and 43s of n3, measured.
                             "todo",
                             # XY-VISIONOFF, 27 Aug. `vision_analyze` DOES NOT RETURN in a
                             # XYCY run. Three attempts on 27 Aug with the auxiliary client
                             # correctly pointed at this machine's own ollama: the log shows
                             # the vision completion coming back 200 in 42 s and the tool
                             # never handing an answer to the agent. XY-HZSTALL cannot see it
                             # either - its watchdog only watches waitingOn == "model".
                             #
                             # It cost run_v8__n7 the end of its step: the Blender massing was
                             # built, the render was written and the .blend was saved, and
                             # then the model - unprompted - reached for vision_analyze to
                             # LOOK at its own render, and the step hung there with every
                             # deliverable already on disk and no verdict.
                             #
                             # A step that wants to look at something has scripts/look_at.py,
                             # which is the same model and answers in about four seconds. The
                             # broken door is closed until the tool is fixed.
                             "vision")

# XY-DOCSTEPPY. The other half of the same question, and the half that was wrong. A step that
# drives NO application has no app to do the work with - it has to AUTHOR its artifact, and on
# this machine that means Python: the xlsx, docx and pdf skills are Python, and there is no
# other way to produce a real workbook or a real PDF.
#
# MEASURED on Sean's PC, 18 Sep 2026, run_1789793077905: one Hermes session for a whole
# workflow, 103 tool calls, two of them to Rhino and neither of them drawing anything, no .3dm
# written, and a 675-byte PDF the model had typed out by hand. Step 3 said why itself:
# code_execution and terminal were off, because ONE step in that run declared a server and the
# exclusion below is per RUN. The document step was made to pay for the CAD step's guard rail.
#
# So the exclusion is per run and a run is per step: a step with an application loses the tools
# that let it avoid the application, and a step with no application keeps terminal and
# code_execution and loses the ones that are either broken here (vision - see XY-VISIONOFF) or
# a way to spend turns without acting (todo, browser, web, computer_use).
DOC_RUN_DISABLED_TOOLSETS = ("browser", "web", "computer_use", "todo", "vision")


# XY-VISIONLOCAL - seconds a single vision_analyze call may take against the local model.
# 85 s measured for a 1024 px sketch on this Mac; the ceiling is for a bigger image on a
# slower machine, not for a hang. A hang is what this whole block exists to stop.
VISION_TIMEOUT = int(os.environ.get("XYCY_VISION_TIMEOUT", "300") or 300)

# Above this many tools on one server, keep Hermes' deferral bridge - see XY-EAGERTOOLS.
EAGER_TOOLS_MAX_PER_SERVER = 8


# XY-NOCODEGEN. Some app connectors expose BOTH typed verbs ("make a box here, this big") and
# an escape hatch that runs arbitrary code inside the application. A small local model reaches
# for the escape hatch and then cannot write code that compiles.
#
# Measured on run_1786843101699 (qwen3.6:35b, Rhino, 2026-08-15). Every typed call succeeded:
# get_document_summary, gh_create_document, capture_viewport, the display-mode command. Every
# code attempt failed: rhinoscript came back "invalid syntax", then C# came back with five
# compiler errors. In between it ran a script whose only successful act was "Cleared all
# geometry" - so the escape hatch's single working contribution was destroying the model.
#
# So for connectors that have real typed verbs, take the escape hatch away and make the model
# build through the typed API, where the argument is JSON and there is no syntax to get wrong.
#
# Keyed by server id, values are exact tool names or fnmatch globs, matched against the bare
# name the server reports. Hermes reads this as mcp_servers.<id>.tools.exclude.
#
# ONLY list a connector here when it can still do its job without code. Rhino can: it has
# create_object/create_objects and a full query API. FreeCAD and SketchUp are deliberately
# ABSENT - their script tool is essentially their whole API, and excluding it would leave the
# model with nothing at all.
APP_CODE_ESCAPE_HATCHES = {
    "rhino": ["execute_rhinoscript_python_code", "execute_rhinocommon_csharp_code"],
}

# XY-NOSERVERPROMPTS, 27 Aug. An MCP server can advertise PROMPTS and RESOURCES as well as
# tools, and Hermes surfaces those as `list_prompts` / `get_prompt` / `list_resources` /
# `read_resource`. They are NOT in the server's tool list, so XY-ONLYTHESETOOLS' include list
# does not touch them - measured twice on rhinomcp, which advertises a "MANDATORY STEPS"
# rhinoscript workflow prompt:
#
#   run_v6__n4  the model's first three calls were list_prompts, list_resources, get_prompt -
#               about 60 seconds spent reading a workflow the payload had already done;
#   run_v8__n4  the same three calls, and then the session ENDED with nothing built.
#
# A payload step is the workflow. Whatever the server wants to tell it about how to work is,
# by construction, a different plan from the one in the prompt. Excluded on every app server,
# always - and by exclusion, so a payload that genuinely names one still wins.
SERVER_PROMPT_TOOLS = ["list_prompts", "get_prompt", "list_resources", "read_resource",
                       "list_resource_templates"]

# XY-CODEBYPAYLOAD. XY-NOCODEGEN exists because a local model handed a code tool writes its own
# geometry and invents the building. That is still true and the default stays deny.
#
# But it collided with the payload method on 27 Aug and cost a run. run_v6__n4's prompt printed a
# complete, XYCY-generated RhinoScript and told the model to paste it into
# `execute_rhinoscript_python_code` - a tool XY-NOCODEGEN had already removed from the array. The
# model could not find it, and spent its first three turns and about 60 seconds pulling
# `list_prompts`, `list_resources` and `get_prompt` off the server instead, hunting for a way in.
#
# The distinction the tool name cannot carry: the hatch is about WHO AUTHORED THE CODE. A script
# the model composes is the thing to refuse; a script XYCY computed and printed in the prompt is
# the payload method working exactly as intended. So the caller says so, once, per run, in the
# open - `--allow-code rhino` - and it is written into the run record and the log. A rule with no
# way to say "I meant it" gets deleted by whoever hits it next at 2am; a rule with a loud,
# recorded exception survives.
CODE_ALLOWED_ENV = "XYCY_ALLOW_CODE"


def tools_named_in(prompt, servers):
    """The tools this prompt actually names, per server. XY-ONLYTHESETOOLS.

    XY-ONLYNEED's rule - keep exactly what the run names - applied one layer in, to TOOLS.
    A payload step prints the calls it is going to make (`mcp__rhino__create_objects`, and so
    on) and makes no others. Everything else on that server is schema the model pays for on
    every single turn and never uses.

    Measured 26 Aug with scripts/turn_profile.py: with the whole Rhino surface in the array a
    step carried ~60k tokens a turn against a 65,536 window; the application itself did its
    work in 1.8 seconds of a 383-second step. The context is the cost, and most of it was for
    tools nobody called.

    Returns {} when the prompt names nothing - a hand-written prompt is not a payload and must
    keep the full surface.
    """
    found, hits = {}, {}
    for server, tool in re.findall(r"mcp__([A-Za-z0-9_]+?)__([A-Za-z0-9_]+)", prompt or ""):
        if server in (servers or {}):
            found.setdefault(server, set()).add(tool)
            hits[server] = hits.get(server, 0) + 1
    # A server mentioned in passing is not a recipe. TWO distinct tools is one - and so is ONE
    # tool named three times, which is what a single-verb payload looks like: the construction
    # document step calls mcp__freecad__execute_code four times and nothing else, and the first
    # cut of this rule left it on the bridge for want of a second name.
    return {k: sorted(v) for k, v in found.items() if len(v) >= 2 or hits.get(k, 0) >= 3}


def _skill_declared_name(skill_dir):
    """The `name:` a SKILL.md declares, or "". Read, never guessed.

    XY-SKILLONLY - a staged folder and the name inside it disagree more often than not:
    `skreg_https___github.com_alirezarezvani_..._design-system` declares `name: design-system`,
    and `blender-product-turntable` declares `name: blender`. Resolving a wanted skill has to
    be able to try both, so both are read off the disk rather than derived from the other.
    """
    try:
        with io.open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8", errors="ignore") as fh:
            for _ in range(12):
                line = fh.readline()
                if not line:
                    break
                if line.lower().startswith("name:"):
                    return line.split(":", 1)[1].strip()
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:739')
    return ""


def _safe_skill_id(value):
    """The same sanitisation tStageSkills uses to name a folder: [^A-Za-z0-9_.-] -> "_"."""
    out = []
    for ch in str(value):
        keep = ch in "_.-" or (ch.isalnum() and ord(ch) < 128)
        out.append(ch if keep else "_")
    return "".join(out)


def _plan_skill_ids(project_dir, run_id):
    """Every skill id the workflow's own plan names, or None when the plan cannot be read.

    None and [] are DIFFERENT ANSWERS and the caller must keep them apart: [] is "the plan
    names no skills", None is "I could not ask". Returning [] for an unreadable plan would
    silently strip every skill from a run - the exact substitution this corpus keeps logging.

    A per-step sub-run is "<parentRunId>__n<node>", and the plan lives in the PARENT's folder.
    """
    base = str(run_id).split("__")[0]
    path = os.path.join(project_dir, "runs", base, "plan.json")
    try:
        with io.open(path, encoding="utf-8", errors="ignore") as fh:
            plan = json.load(fh)
    except Exception:
        return None
    ids = []
    nodes = plan.get("nodes") or plan.get("steps") or []
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for skill in (node.get("skills") or []):
            sid = skill.get("id") if isinstance(skill, dict) else skill
            if sid and sid not in ids:
                ids.append(sid)
    return ids


HOSTNAME_LOWER = (os.environ.get("HOSTNAME") or "").lower()

# XY-CLOUDAUTH - WHERE the call goes decides the credential, never the provider NAME.
# Hermes has no `openai` provider (31 Aug: `--provider openai` dies with "Unknown provider
# 'openai'"), so an OpenAI-shaped cloud endpoint must be reached as provider `custom` - the
# exact name XY-VISIONKEY read as "this one is local". Every cloud run therefore handed itself
# the dummy credential on BOTH routes, and the two failures look nothing alike:
#   * main route - build_home writes $HERMES_HOME/.env, the OpenAI client reads it, and it
#     BEATS the process environment. Measured on gpt-5: HTTP 401 "Incorrect API key provided:
#     local", on the first call, every time.
#   * vision route - api_key "ollama", and per XY-VISIONLOCAL an unauthenticated vision call
#     does not fail. It HANGS ~133 s with the model idle, invisible to the stall watchdog.
# So classify by the ENDPOINT. A base_url on this machine (or none, which means the default
# local Ollama) is local; anything else is somebody else's server and needs the real key.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def endpoint_is_local(base_url):
    """True when base_url points at this machine, or is absent (the default local Ollama)."""
    if not base_url:
        return True
    try:
        host = (urlparse(str(base_url)).hostname or "").strip("[]").lower()
    except Exception:
        return False
    if not host:
        return False
    return host in LOCAL_HOSTS or host.endswith(".local") or (
        bool(HOSTNAME_LOWER) and host == HOSTNAME_LOWER)


def _ensure_doc_libs_quietly():
    """XY-DOCLIBS2. Make sure the document libraries are importable, and never raise.

    The work itself lives in xycy_setup.ensure_doc_libs, beside which_hermes, because that file
    already knows where Hermes is on each platform and one question must have one answer. It is
    imported HERE rather than at module scope so a runner sitting next to an older copy of
    xycy_setup still starts runs - it just does not get this.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import xycy_setup            # noqa: E402  - deliberately late, see above
        return xycy_setup.ensure_doc_libs()
    except Exception as exc:
        return {"ok": False, "why": "the document-library check could not run: %s" % exc}


def _plan_declares_script(run_dir, project_dir, run_id):
    """True when this run's plan carries a Script Sub-Agent step.

    XY-SCRIPTAGENT's contract: a script in a workflow is a step the person placed on the
    canvas, with {file, tool} on it. Anything else calling itself a script is the model
    inventing one mid-run. A per-step run's plan lives one level up, under the parent id,
    which is the same shape _step_verdicts already has to handle.
    """
    seen = []
    for folder in (run_dir, os.path.join(project_dir, "runs",
                                         re.sub(r"__n.*$", "", str(run_id or "")))):
        plan = load_json(os.path.join(folder, "plan.json"))
        if isinstance(plan, dict):
            seen.extend(plan.get("nodes") or [])
    for node in seen:
        if isinstance(node, dict) and isinstance(node.get("script"), dict) \
                and str((node.get("script") or {}).get("file") or "").strip():
            return True
    return False


def build_home(run_id, servers, model, provider, base_url, context_length,
               max_tool_calls, project_dir, want=None, named_tools=None, allow_code=()):
    """Create $HERMES_HOME for this run: config, plugin, skills bridge."""
    home = os.path.join(RUN_HOMES, run_id)
    os.makedirs(os.path.join(home, "plugins"), exist_ok=True)
    os.makedirs(os.path.join(home, "skills"), exist_ok=True)

    model_cfg = {"default": model} if model else {}
    if provider:
        model_cfg["provider"] = provider
    if base_url:
        model_cfg["base_url"] = base_url
    if context_length:
        model_cfg["context_length"] = int(context_length)
        # Two DIFFERENT numbers, and getting them confused is the whole trap.
        # `context_length` is what Hermes believes the model's window is.
        # `ollama_num_ctx` is what Ollama actually allocates at load time — its
        # default is far smaller, and Hermes refuses the run when the RUNTIME
        # window is under 64k even if the model's advertised window is fine.
        # Only meaningful for Ollama-backed providers; harmless elsewhere.
        # XY-CLOUDAUTH - and only when the endpoint really is Ollama on this machine,
        # because `custom` is also what a cloud OpenAI endpoint has to call itself.
        if (provider or "").lower() in ("ollama", "custom", "") and endpoint_is_local(base_url):
            model_cfg["ollama_num_ctx"] = int(context_length)

    # XY-VISIONLOCAL. Hermes' `vision_analyze` does NOT use the run's model. It routes to an
    # "auxiliary client", whose auto order is (openrouter, nous, deepinfra) - three cloud
    # services. On a machine with no cloud credentials the call does not fail and does not
    # return: measured 27 Aug on run_v6__n0, `vision_analyze` sat for 133 s with ollama at 0%
    # CPU and the model idle, and it would have sat there until the run was killed. It is
    # invisible to XY-HZSTALL, whose watchdog only watches `waitingOn == "model"`.
    #
    # Two reasons this is a defect and not a configuration gap:
    #   * Sean's rule is that Hermes runs ONLY local models. A step that silently reaches for
    #     OpenRouter to look at a picture breaks that rule whether or not it succeeds.
    #   * Looking at the input drawing is the whole point of a design step. A concept sketch
    #     that cannot be looked at is the reason every Diyara tower until today was a box.
    #
    # So: point the vision task at the same local endpoint the run itself uses. qwen3.6:35b
    # reports `vision` in its ollama capabilities and answers a 1024 px sketch in about 85 s.
    # XY-VISIONKEY. "ollama" is a placeholder Ollama ignores, not a credential. Pinning it
    # here meant that the moment a run was pointed at a cloud endpoint the main route
    # authenticated and the vision route sent the literal string - a 401. Which does not fail
    # the step: XY-VISIONLOCAL measured an unauthenticated vision call sitting for 133 s with
    # the model idle, invisible to the stall watchdog. Follow the same rule the main route
    # uses below - dummy key for local providers, the real one for anything else - and do NOT
    # fall back to the dummy on a cloud provider with no key, because a wrong credential hangs
    # and a missing one fails fast.
    _vis_local = endpoint_is_local(base_url)   # XY-CLOUDAUTH, was: the provider NAME
    _vis_key = "ollama" if _vis_local else (os.environ.get("OPENAI_API_KEY") or "")
    aux = {"vision": {"provider": provider or "custom",
                      "model": model,
                      "base_url": base_url or "http://localhost:11434/v1",
                      "api_key": _vis_key,
                      "timeout": VISION_TIMEOUT}}

    config = {
        "model": model_cfg,
        "auxiliary": aux,
        # A run drives real applications; it must not stop at the chat default.
        "code_execution": {"max_tool_calls": int(max_tool_calls), "timeout": 900},
        "delegation": {"max_iterations": 200},
        "plugins": {"enabled": ["xycy_progress"]},
        "hooks_auto_accept": True,
        "mcp_servers": servers,
    }
    # XY-MCPWAIT. A run is a ONE-SHOT Hermes session (`hermes -z`). Hermes snapshots the
    # model's tool list once, at session start, and in one-shot mode there is no second turn,
    # so its between-turns "late-binding refresh" never fires: any MCP server that finishes
    # connecting after that snapshot is invisible to the model for the ENTIRE run.
    #
    # Hermes bounds how long it waits for discovery. Its one-shot default is 15s, and a cold
    # `uvx` app server lands right on top of that. Measured on run_1786838624508: the run began
    # at 18:03:46, the session (and the snapshot) opened at 18:04:03, and the Rhino server
    # answered its tool list at 18:04:07 — four seconds late. The model then spent twelve
    # minutes with only the MCP plumbing tools (list_prompts/read_resource) and not one Rhino
    # verb, which read from the outside exactly like "the model refuses to use the connector".
    # Every surface said connected, because every surface WAS connected.
    #
    # The wait returns the instant discovery finishes, so a warm server still pays ~0s. This
    # bound only caps a genuinely slow cold start, and paying it is always cheaper than a run
    # that cannot touch the application it exists to drive.
    if servers:
        config["mcp_single_query_discovery_timeout"] = 90.0
        # XY-ONLYTHESETOOLS - keep exactly the tools this run names. `include` wins over
        # `exclude` in Hermes, so the code escape hatches are subtracted from the include list
        # rather than left to XY-NOCODEGEN below: a filter that quietly re-admits the one tool
        # another rule exists to remove would be worse than no filter at all.
        for _sid, _keep in (named_tools or {}).items():
            if _sid not in servers:
                continue
            _hatches = set() if _sid in allow_code else set(APP_CODE_ESCAPE_HATCHES.get(_sid) or [])
            _keep = [t for t in _keep if t not in _hatches]
            if not _keep:
                continue
            _entry = dict(servers[_sid])
            _tools = dict(_entry.get("tools") or {})
            _tools["include"] = _keep
            _entry["tools"] = _tools
            servers[_sid] = _entry
        config["mcp_servers"] = servers
        # XY-NOSERVERPROMPTS - see the note above. Exclusion only, on every app server.
        for _sid in list(servers):
            _entry = dict(servers[_sid])
            _tools = dict(_entry.get("tools") or {})
            _named = set((named_tools or {}).get(_sid) or [])
            _excl = list(_tools.get("exclude") or [])
            for _name in SERVER_PROMPT_TOOLS:
                if _name not in _excl and _name not in _named:
                    _excl.append(_name)
            _tools["exclude"] = _excl
            _entry["tools"] = _tools
            servers[_sid] = _entry
        config["mcp_servers"] = servers
        # XY-NOCODEGEN, applied per server. See the note on APP_CODE_ESCAPE_HATCHES.
        for _sid, _hatches in APP_CODE_ESCAPE_HATCHES.items():
            if _sid in allow_code:          # XY-CODEBYPAYLOAD - said out loud, per run
                continue
            if _sid in servers:
                _entry = dict(servers[_sid])
                _tools = dict(_entry.get("tools") or {})
                # Never widen an include list someone else set; exclusion only.
                _excl = list(_tools.get("exclude") or [])
                for _name in _hatches:
                    if _name not in _excl:
                        _excl.append(_name)
                _tools["exclude"] = _excl
                _entry["tools"] = _tools
                servers[_sid] = _entry
        config["mcp_servers"] = servers
    # XY-APPSCOPE. When this run drives an application, take away the tools that let the model
    # do the job ITSELF instead of through the app - a local model will happily shell out or
    # run its own Python rather than call the connector.
    #
    # This MUST be done by exclusion here, never by passing `-t` a list of allowed toolsets.
    # `-t` scopes by inclusion and drops every MCP tool with it, which is exactly how Site Prep
    # came to make 19 file calls and zero Rhino calls with Rhino open and connected.
    # XY-WRITEGUARD - a pre_tool_call hook that refuses a write leaving the run's folders, and
    # refuses a program file on a run that declares no script step. On EVERY run, not only an
    # application run: the seven Python files that started this were written by a run driving
    # Rhino, and a document run with Python is exactly where it will matter next.
    # hooks_auto_accept is already true in this profile, and --accept-hooks is on the command
    # line, so the hook registers with no prompt on a headless run.
    # `command` is a single STRING, not a list. MEASURED 19 Sep: the first version passed a
    # list, _parse_single_entry logged "is missing a non-empty 'command' field" and skipped the
    # hook, and the guard was simply absent - a live run wrote all three test files and the
    # model reported "all four were allowed through". A hook that fails to register is a guard
    # that is off with nothing on screen saying so, which is why this is proved by a run that
    # tries to write, never by reading the config back.
    # Quoted because both paths hold spaces on Windows; Hermes splits with its own
    # split_command_line, which keeps backslashes (shlex would eat them).
    _guard = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xycy_writeguard.py")
    if os.path.isfile(_guard):
        config["hooks"] = {"pre_tool_call": [
            {"command": '"%s" "%s"' % (sys.executable, _guard),
             "timeout": 10, "fail_closed": False}]}
    if not servers:
        # XY-DOCSTEPPY - no application on this step, so it keeps terminal and code_execution.
        config["agent"] = {"disabled_toolsets": list(DOC_RUN_DISABLED_TOOLSETS)}
    if servers:
        config["agent"] = {"disabled_toolsets": list(APP_RUN_DISABLED_TOOLSETS)}
        # XY-EAGERTOOLS. Hermes DEFERS MCP tools by default: they are replaced in the
        # model-facing tools array by three bridge tools - tool_search / tool_describe /
        # tool_call - and surfaced on demand. That is the right default for a 3,300-tool
        # catalogue and the wrong one for a step that drives ONE application.
        #
        # Measured on this Mac, 26 Aug, with scripts/turn_profile.py: an application does its
        # work in about a second, and the step costs three to five minutes. n4 spent 33.6s on
        # a tool_describe before its first real call; n3 spent roughly 80s across four
        # tool_call / tool_describe detours out of 322s. XY-DIRECTCALL and XY-ROUTERNO are both
        # written to survive a bridge the run does not need in the first place.
        #
        # It also explains glm-4.7-flash writing "Excel tools not in tool list": behind the
        # bridge that was very nearly TRUE, and only a model that already knows the exact tool
        # name (because our payload prints it) can call one without searching first.
        #
        # MEASURED, and the first version of this was WRONG. Turning the bridge off for any
        # small run put every Rhino schema in the array: n4 went from ~18k tokens a turn to
        # ~60k against a 65,536 window, and the step got SLOWER (383s against 180s) even though
        # its turns dropped from 8 to 7. The bridge exists for a reason and the reason is real.
        #
        # What is actually true is narrower: a PAYLOAD step names the tools it will call, in
        # the prompt, and calls nothing else. So the run keeps only those tools
        # (XY-ONLYTHESETOOLS below) - and once the array is a handful of small schemas, the
        # bridge is pure overhead and goes off. Eager is a CONSEQUENCE of the filter, never a
        # setting on its own.
        if named_tools and all(len(v) <= EAGER_TOOLS_MAX_PER_SERVER
                               for v in named_tools.values()):
            config["tools"] = {"tool_search": {"enabled": "off"}}
    # Written as JSON on purpose: YAML is a superset of JSON, Hermes parses this
    # file with yaml.safe_load, and hand-rolling YAML quoting for Windows paths
    # and args like `--with mcp==1.28.1` is a bug farm.
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)

    # Ollama and friends need no key, but the OpenAI-compatible client wants one.
    # XY-CLOUDAUTH - this file BEATS the process environment, so on a cloud endpoint the
    # placeholder below is not a harmless default: it is the credential that gets sent.
    env_path = os.path.join(home, ".env")
    if not os.path.exists(env_path):
        if endpoint_is_local(base_url):
            _api_key = os.environ.get("XYCY_LOCAL_API_KEY") or "local"
        else:
            _api_key = os.environ.get("OPENAI_API_KEY") or ""
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write("OPENAI_API_KEY=%s\n" % _api_key)
        os.chmod(env_path, 0o600)

    # Instrumentation. Copied, not symlinked: a symlink into the XYCY install
    # would break the moment the app is moved or updated mid-run.
    plugin_dst = os.path.join(home, "plugins", "xycy_progress")
    if os.path.isdir(PLUGIN_SRC):
        shutil.rmtree(plugin_dst, ignore_errors=True)
        shutil.copytree(PLUGIN_SRC, plugin_dst)

    # Skills bridge. stage_skills writes <project>/.claude/skills/<id>/SKILL.md,
    # which Hermes does not read; it loads $HERMES_HOME/skills plus AGENTS.md.
    #
    # XY-SKILLONLY - carry exactly what the run NAMES, never everything on disk. This used to
    # symlink every folder under .claude/skills/: a two-skill step was handed twelve, ~33k
    # tokens of a 65,536 window spent before the model acted. XY-ONLYNEED's rule, applied to
    # skills. The comment twelve lines above already said the ceiling matters more here than
    # in chat; the code then loaded every skill on disk into it.
    staged = os.path.join(project_dir, ".claude", "skills")
    wanted, source = (list(want) if want else None), "args"
    if wanted is None:
        wanted = _plan_skill_ids(project_dir, run_id)
        source = "plan"
    if wanted is None:
        # Could not ask. Leave the bridge exactly as it was and SAY SO - a filter that fails
        # open is recoverable, one that silently strips a step's method is not.
        source = "unfiltered"

    available, by_safe, by_name = [], {}, {}
    if os.path.isdir(staged):
        for name in sorted(os.listdir(staged)):
            folder = os.path.join(staged, name)
            if not os.path.isdir(folder) or not os.path.exists(os.path.join(folder, "SKILL.md")):
                continue
            available.append(name)
            by_safe.setdefault(_safe_skill_id(name), name)
            declared = _skill_declared_name(folder)
            if declared:
                by_name.setdefault(declared, name)

    unresolved = []
    if source == "unfiltered":
        chosen = list(available)
    else:
        chosen = []
        for wid in wanted:
            # Folder name first, then the sanitised id tStageSkills would have written, then
            # the name declared inside SKILL.md. Three ways a plan id reaches one folder.
            hit = None
            if wid in available:
                hit = wid
            elif _safe_skill_id(wid) in by_safe:
                hit = by_safe[_safe_skill_id(wid)]
            elif wid in by_name:
                hit = by_name[wid]
            if hit is None:
                unresolved.append(wid)
            elif hit not in chosen:
                chosen.append(hit)

    # A run id gets built more than once - a retry resumes it, and a re-run after a fix reuses
    # it. Anything the LAST bridge left in this home that this one does not want has to go, or
    # the step silently inherits a skill its plan stopped naming. Found by the driver rather
    # than by reasoning: linking only what is wanted is not the same as carrying only it.
    skills_home = os.path.join(home, "skills")
    try:
        for stale in sorted(os.listdir(skills_home)):
            if stale in chosen:
                continue
            victim = os.path.join(skills_home, stale)
            if os.path.islink(victim):
                os.unlink(victim)
            else:
                shutil.rmtree(victim, ignore_errors=True)
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:1130')

    bridged = []
    for name in chosen:
        source_dir = os.path.join(staged, name)
        dst = os.path.join(home, "skills", name)
        if os.path.islink(dst) or os.path.exists(dst):
            if os.path.islink(dst):
                os.unlink(dst)
            else:
                shutil.rmtree(dst, ignore_errors=True)
        try:
            os.symlink(source_dir, dst)
        except (OSError, NotImplementedError, AttributeError):
            # [[cross-platform-rule]] - Windows refuses a symlink without Developer Mode or an
            # elevated process, and a skills bridge that only works on one person's Mac is not
            # a bridge. Copying costs a few KB and always works.
            try:
                shutil.copytree(source_dir, dst)
            except Exception:
                continue
        bridged.append(name)

    return home, bridged, {"wanted": (wanted or []), "source": source,
                           "unresolved": unresolved, "available": available}


MODEL_KEYS = ("default", "model", "provider", "base_url", "context_length", "ollama_num_ctx")


def read_user_model_config():
    """What model is Hermes ACTUALLY configured to use? Ask Hermes, don't guess.

    `hermes config get <key>` prints the RESOLVED value, which beats reading the file for two
    reasons: it accounts for layering we would otherwise have to reimplement, and it picks up an
    endpoint we would never have guessed — someone running vLLM on a custom port, or LM Studio
    moved off 1234. Falls back to scanning the file when the CLI is unavailable.
    """
    hermes = find_hermes()
    if hermes:
        found, env = {}, enriched_env()
        for key, alias in (("model.default", "default"), ("model.provider", "provider"),
                           ("model.base_url", "base_url"),
                           ("model.context_length", "context_length"),
                           ("model.ollama_num_ctx", "ollama_num_ctx")):
            try:
                proc = subprocess.run([hermes, "config", "get", key], capture_output=True,
                                      text=True, timeout=45, env=env)
                value = (proc.stdout or "").strip().splitlines()
                value = value[-1].strip() if value else ""
            except Exception:
                continue
            # An unset key prints a sentence, not a value. Anything with a space in it is prose.
            if value and "not set" not in value.lower() and " " not in value:
                found[alias] = value
        if found.get("default"):
            return found
    return _scan_model_config()


def _scan_model_config():
    """Fallback: pull the `model:` block out of $HERMES_HOME/config.yaml by hand.

    Deliberately a scan, not a YAML parse: the shipped config.yaml is a 92 KB commented
    example and the file must be readable without adding a YAML dependency to a helper that
    has to run on any machine with bare python3.
    """
    home = hermes_home()
    path_ = os.path.join(home, "config.yaml")
    found = {}
    try:
        with open(path_, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return found
    inside = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():                  # a new top-level key ends the block
            inside = stripped.startswith("model:")
            continue
        if not inside or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in MODEL_KEYS and value:
            found[key] = value
    return found


# XY-OLLAMAHERE (18 Sep 2026) - "nothing is serving models" and "there is no model server on
# this computer" are two different answers, and folding them together is what sent a PC that
# already had Ollama installed back to the download page for another 173 MB and an interactive
# setup wizard. MEASURED on Sean's PC 18 Sep: Ollama installed at
# %LOCALAPPDATA%\Programs\Ollama\ollama.exe, endpoint dead, Hermes stuck on "Needs attention",
# and the only button the wizard offered was Install. Starting the program that was already
# there took eight seconds and made Hermes ready. So the probe reports WHERE the program is,
# separately from whether it is answering, and the setup step starts it instead of fetching it.
def find_ollama():
    """The Ollama program on this computer, or "" - not whether it is serving."""
    cands = [shutil.which("ollama"), os.path.join(HOME, ".local", "bin", "ollama"),
             "/Applications/Ollama.app/Contents/Resources/ollama"]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
        progs = os.environ.get("ProgramFiles") or "C:\\Program Files"
        cands = [shutil.which("ollama.exe"), shutil.which("ollama"),
                 os.path.join(local, "Programs", "Ollama", "ollama.exe"),
                 os.path.join(progs, "Ollama", "ollama.exe")] + cands
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def ollama_native_context(base_url, model):
    """The model's TRAINED context window, straight from Ollama — not what it was loaded with.

    This exists because the probe can be made to pass by a model that cannot really do the job:
    setting `ollama_num_ctx: 65536` makes Ollama allocate a 64k window for a model whose native
    limit is 40,960, Hermes' floor check is satisfied, and the readiness card goes green on a
    model that is rope-extended past its training and will degrade instead of failing. Returns
    None when the endpoint is not Ollama or does not say.
    """
    if not base_url or not model:
        return None
    try:
        import urllib.request
        root = base_url.rstrip("/")
        for suffix in ("/v1", "/api"):
            if root.endswith(suffix):
                root = root[: -len(suffix)]
        req = urllib.request.Request(
            root + "/api/show",
            data=json.dumps({"model": model}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read().decode("utf-8")) or {}
    except Exception:
        return None
    # The key is namespaced by architecture: llama.context_length, qwen3.context_length, …
    for key, value in (info.get("model_info") or {}).items():
        if key.endswith(".context_length"):
            try:
                return int(value)
            except Exception:
                return None
    return None


# XY-FULLCONTEXT - the window a run actually gets.
#
# MIN_CONTEXT is a FLOOR: it is the smallest window Hermes will start on. It was also being
# used as the DEFAULT, which is a different thing entirely, and the difference was costing
# three quarters of the model. MEASURED on Sean's PC, 20 Sep 2026: Ollama had qwen3.6:35b
# loaded with context_length 262144, the whole 29 GB in VRAM, and every XYCY run on Hermes
# asked it for 65,536 - because MIN_CONTEXT was the last term in the `or` chain and because
# picking a model on the Hermes card wrote model.ollama_num_ctx = 65536 into the person's own
# config for ever. A XYCY run carries plan.json, a brief and its skills before it does any
# work, so the window is not a nicety.
#
# So: what the caller asked for, then what the person's config says, then THE MODEL'S OWN
# trained window, and only then the floor. Capped at 262,144 because that is the largest
# window any model here advertises and an unbounded number is an unbounded allocation.
#
# THE TRADE, written down rather than discovered later: ollama_num_ctx is an allocation, and a
# bigger one on a machine that cannot hold it makes Ollama put layers in system memory, which
# is slower. On this machine Ollama had already chosen the native window by itself, so asking
# for it changes nothing and avoids a reload. A person who wants a smaller one sets
# model.ollama_num_ctx in their own Hermes config and it wins, as it always did.
MAX_CONTEXT = 262144


def resolve_context(asked, cfg, base_url, model):
    """The context window for this run, and where the number came from."""
    for value, why in ((asked, "asked for"),
                       (cfg.get("ollama_num_ctx"), "model.ollama_num_ctx in your Hermes config"),
                       (cfg.get("context_length"), "model.context_length in your Hermes config")):
        try:
            n = int(value or 0)
        except Exception:
            n = 0
        if n > 0:
            return n, why
    native = ollama_native_context(base_url, model)
    try:
        native = int(native or 0)
    except Exception:
        native = 0
    if native > 0:
        return max(MIN_CONTEXT, min(MAX_CONTEXT, native)), "the model's own trained window"
    return MIN_CONTEXT, "the smallest window Hermes will start on, because the model's own could not be read"


# XY-LOCALONLY. Sean, 26 Aug: "Hermes needs to run only local models." A runtime that lists a
# cloud model beside the local ones is not a local runtime with a caveat - it is a local runtime
# until somebody picks the wrong row. Ollama serves cloud models through the SAME endpoint and
# the same /api/tags listing as the local ones: `kimi-k2.7-code:cloud` came back as a 320-byte
# "model" with a 262,144 window and qualifies: true, and the only thing that stopped it running
# was an HTTP 401 from someone else's server.
#
# Two signals, both cheap, both measured on this machine:
#   * the id carries a `:cloud` tag - Ollama's own convention for a hosted model;
#   * the "model" is a stub, bytes rather than gigabytes. Nothing that runs here is 320 bytes.
# Enforced in BOTH places on purpose: the picker marks it, and `start` refuses it. A rule
# enforced on one surface is not a rule - that is XY-CUBEGONE's lesson, one subsystem along.
CLOUD_STUB_MAX_BYTES = 50 * 1024 * 1024


def cloud_model_reason(name, size=None):
    """Why this model is not local, or None when it is. Never guesses from the provider name."""
    tag = str(name or "").strip().lower()
    if tag.endswith(":cloud") or "/cloud" in tag:
        return "runs in the cloud - Hermes runs local models only"
    if isinstance(size, (int, float)) and 0 <= size <= CLOUD_STUB_MAX_BYTES:
        return ("listed as %s bytes - a stub for a hosted model, not a local one"
                % "{:,}".format(int(size)))
    return None


def cmd_models(args):
    """List the local models actually installed on THIS machine, with their real limits.

    Feeds both model pickers — the Hermes setup screen (which sets the defaults for every CLI
    call) and a Sub-Agent's own model preference. It replaces a hardcoded list of invented
    names, so the rule is: report what is installed, and say plainly which ones Hermes can
    actually use and why.

    A model that does not qualify is INCLUDED and marked, never hidden. Silently omitting it
    turns "why isn't my model listed" into a support question; showing it greyed with
    "trained for 40,960 tokens, needs 65,536" answers itself.
    """
    # ASK HERMES FIRST. The assumption to make is that this person already has models — they
    # installed Hermes to use them. So the primary source is whatever endpoint Hermes is
    # actually pointed at, which may be on a port nobody would guess. The two well-known local
    # ports are probed afterwards as extras, and only to catch a second server they also run.
    endpoints = []
    if args.base_url:
        endpoints.append(("configured", args.base_url))
    else:
        cfg = read_user_model_config()
        if cfg.get("base_url"):
            endpoints.append((cfg.get("provider") or "configured", cfg["base_url"]))
        for provider, url in (("ollama", "http://127.0.0.1:11434/v1"),
                              ("lmstudio", "http://127.0.0.1:1234/v1")):
            if not any(u.rstrip("/") == url.rstrip("/") for _, u in endpoints):
                endpoints.append((provider, url))

    import urllib.request

    def get_json(url, payload=None, timeout=10):
        try:
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if data else {}
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    models, reachable = [], []
    for provider, base_url in endpoints:
        root = base_url.rstrip("/")
        for suffix in ("/v1", "/api"):
            if root.endswith(suffix):
                root = root[: -len(suffix)]

        listing = get_json(root + "/api/tags")            # Ollama
        names = []
        if listing and isinstance(listing.get("models"), list):
            for entry in listing["models"]:
                names.append((entry.get("name") or entry.get("model"), entry.get("size")))
        else:
            listing = get_json(base_url.rstrip("/") + "/models")   # OpenAI-compatible fallback
            if listing and isinstance(listing.get("data"), list):
                names = [(e.get("id"), None) for e in listing["data"]]
        if not names:
            continue
        reachable.append({"provider": provider, "base_url": base_url, "count": len(names)})

        for name, size in names:
            if not name:
                continue
            native = ollama_native_context(base_url, name)
            item = {
                "id": name, "provider": provider, "base_url": base_url,
                "sizeBytes": size,
                "sizeGB": round(size / 1e9, 1) if isinstance(size, (int, float)) else None,
                "nativeContext": native,
                "minContext": MIN_CONTEXT,
            }
            cloud = cloud_model_reason(name, size)
            if cloud:
                # Marked, never hidden - the same rule the docstring above states for a window
                # that is too small. "Why isn't my model listed" is a support question;
                # "runs in the cloud" answers itself.
                item["qualifies"] = False
                item["reason"] = cloud
                item["local"] = False
                models.append(item)
                continue
            item["local"] = True
            if native is None:
                # Unknown is not the same as too small. Let the person try it; the readiness
                # probe is the real gate and it fails honestly.
                item["qualifies"] = None
                item["reason"] = "context window unknown — Hermes will refuse it if it is under %s" % (
                    "{:,}".format(MIN_CONTEXT))
            elif native < MIN_CONTEXT:
                item["qualifies"] = False
                item["reason"] = "trained for %s tokens; Hermes needs %s" % (
                    "{:,}".format(native), "{:,}".format(MIN_CONTEXT))
            else:
                item["qualifies"] = True
                item["reason"] = "%s tokens of context" % "{:,}".format(native)
            models.append(item)

    usable = [m for m in models if m["qualifies"]]
    print(json.dumps({
        "ok": True,
        "endpoints": reachable,
        "models": models,
        "usable": [m["id"] for m in usable],
        "count": len(models),
        "usableCount": len(usable),
        "minContext": MIN_CONTEXT,
        # XY-OLLAMAHERE - the path to the model server program, whether or not it is answering.
        # "" means there is none on this computer and one has to be fetched; a path with
        # state no_endpoint means it is installed and simply is not running, which is a
        # different sentence and a different button.
        "serverInstalled": find_ollama(),
        # The UI needs to tell three states apart: nothing serving, models but none usable, and
        # ready. They have completely different next actions.
        "state": ("no_endpoint" if not reachable else
                  "none_qualify" if not usable else "ok"),
    }))


def cmd_probe(args):
    """Answer 'is Hermes ready to run a XYCY workflow' by actually making it reason.

    Reports the DISTINCT failure, because each one has a different fix:
      not_installed | no_runner | not_configured | context_too_small | endpoint_down | no_reason

    Runs against a scratch HERMES_HOME with NO mcp_servers. That matters: probing against the
    user's own home would start every server they have, and `autodesk` (npx mcp-remote) opens
    an OAuth browser window. A readiness check must never do that.
    """
    hermes = find_hermes()
    result = {"ok": True, "harness": "hermes", "installed": bool(hermes), "path": hermes or None,
              "runner": os.path.abspath(__file__), "ready": False, "state": "not_installed"}
    if not hermes:
        print(json.dumps(result))
        return

    env = enriched_env()
    try:
        result["version"] = subprocess.run(
            [hermes, "--version"], capture_output=True, text=True, timeout=60, env=env
        ).stdout.strip().splitlines()[0]
    except Exception:
        result["version"] = None

    cfg = read_user_model_config()
    model = args.model or cfg.get("default") or cfg.get("model")
    provider = args.provider or cfg.get("provider")
    base_url = args.base_url or cfg.get("base_url")
    context_length, context_from = resolve_context(args.context_length, cfg, base_url, model)   # XY-FULLCONTEXT
    # XY-FULLCONTEXT2 - the readiness answer carries the window this computer will actually
    # use, beside the one the model was trained for, so the card can say when they differ.
    # Until today nothing reported the first of those and the gap was invisible.
    result.update({"model": model, "provider": provider, "base_url": base_url,
                   "contextLength": context_length, "contextFrom": context_from})
    if not model:
        result["state"] = "not_configured"
        result["hint"] = "no model set - run `hermes setup` or set model.default"
        print(json.dumps(result))
        return

    # Check the model's TRAINED window BEFORE spending two minutes proving it can emit a word.
    # A pass that was bought by rope-extending the model is not a pass.
    native = ollama_native_context(base_url, model)
    if native:
        result["nativeContext"] = native
        if native < MIN_CONTEXT:
            result["state"] = "context_too_small"
            result["minContext"] = MIN_CONTEXT
            result["hint"] = (
                "%s was trained for %s tokens of context; Hermes needs %s. Forcing a bigger "
                "window would run it past its training instead of failing honestly — pick a "
                "model whose NATIVE window is at least %s."
                % (model, "{:,}".format(native), "{:,}".format(MIN_CONTEXT),
                   "{:,}".format(MIN_CONTEXT))
            )
            print(json.dumps(result))
            return

    home = os.path.join(RUN_HOMES, "_probe")
    build_home("_probe", {}, model, provider, base_url, context_length,
               DEFAULT_MAX_TOOL_CALLS, HOME)
    env["HERMES_HOME"] = home
    env.pop("XYCY_RUN_DIR", None)         # keep probes out of the run event feed

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [hermes, "-z", "Reply with exactly the single word: READY",
             "--yolo", "--accept-hooks"],
            capture_output=True, text=True, timeout=args.timeout, env=env, cwd=home,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        result["state"] = "timeout"
        result["hint"] = "the model did not answer within %ss" % args.timeout
        print(json.dumps(result))
        return
    except Exception as exc:
        result["state"] = "no_reason"
        result["hint"] = str(exc)
        print(json.dumps(result))
        return

    result["elapsedSec"] = round(time.monotonic() - started, 1)
    result["reply"] = " ".join(text.split())[:400]

    low = text.lower()
    # Order matters: the context complaint also contains the word "context", and an auth
    # failure can mention the model, so test the most specific cause first.
    if "below the minimum" in low or "tokens of runtime context" in low:
        result["state"] = "context_too_small"
        result["minContext"] = MIN_CONTEXT
        result["hint"] = ("%s cannot serve %s tokens of context. Pick a model whose NATIVE window "
                          "is bigger, and set model.ollama_num_ctx as well as model.context_length."
                          % (model, MIN_CONTEXT))
    elif any(s in low for s in ("connection refused", "failed to connect", "could not connect",
                                "connection error", "name or service not known")):
        result["state"] = "endpoint_down"
        result["hint"] = "nothing is serving %s" % (base_url or "the configured endpoint")
    elif any(s in low for s in ("api key", "unauthorized", "401", "authentication")):
        result["state"] = "not_configured"
        result["hint"] = "the provider rejected the credentials"
    # The same rule as the Claude path: green ONLY when it actually reasoned. A naive /ok/i
    # test once matched the "ok" inside "tOKen" in "Invalid bearer token" and reported a
    # signed-in engine that could not answer. Require the word, and no error alongside it.
    elif "ready" in low and not any(s in low for s in ("error", "failed", "invalid", "unable")):
        result["ready"] = True
        result["state"] = "ready"
    else:
        result["state"] = "no_reason"
        result["hint"] = "Hermes ran but did not answer READY"
    print(json.dumps(result))



# ===== XY-HERMES-THINK - short reasoning, locally, with no tools =============================
# The sparkle features (ranking, classification, drafting a brief, proposing a step list) are
# short prompts over material XYCY already holds. Those a small local model can do. Anything
# needing the live web or big-model judgement is NOT routed here - index.js sends it to XYCY's
# own Anthropic API instead, which is the one thing Sean's no-Claude-CLI rule still allows.

# Hermes' CONFIGURABLE_TOOLSETS, verbatim. Everything here is switched off for a think, minus
# `web` in web mode. Kept as a literal list rather than asked of Hermes at runtime because a
# think must not pay an import of Hermes' toolset machinery on every call.
THINK_TOOLSETS = [
    "web", "browser", "terminal", "file", "code_execution", "vision", "video", "image_gen",
    "video_gen", "bfl", "x_search", "tts", "stt", "skills", "todo", "memory", "context_engine",
    "session_search", "clarify", "delegation", "cronjob", "homeassistant", "spotify", "discord",
    "discord_admin", "yuanbao", "computer_use",
]

# A local model at a 64k window has room for far more than this, but a prompt this long is a
# sign the caller wanted the big brain (a whole catalogue, a whole plan). Refuse and say why,
# so the router can send it to the API rather than getting a confidently truncated answer.
THINK_MAX_CHARS = 24000


def think_home(mode, model, provider, base_url, context_length):
    home = os.path.join(RUN_HOMES, "_think" if mode != "web" else "_think_web")
    os.makedirs(home, exist_ok=True)

    model_cfg = {"default": model} if model else {}
    if provider:
        model_cfg["provider"] = provider
    if base_url:
        model_cfg["base_url"] = base_url
    if context_length:
        model_cfg["context_length"] = int(context_length)
        if (provider or "").lower() in ("ollama", "custom", ""):
            model_cfg["ollama_num_ctx"] = int(context_length)

    disabled = [t for t in THINK_TOOLSETS if not (mode == "web" and t == "web")]
    config = {
        "database": {"journal_mode": "wal"},
        "model": model_cfg,
        "mcp_servers": {},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "agent": {"disabled_toolsets": disabled},
        "plugins": {"enabled": []},
        "hooks_auto_accept": True,
    }
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)

    env_path = os.path.join(home, ".env")
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write("OPENAI_API_KEY=%s\n" % (os.environ.get("XYCY_LOCAL_API_KEY") or "local"))
        os.chmod(env_path, 0o600)
    return home


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _read_arg(value, path_):
    if path_:
        try:
            with open(path_, "r", encoding="utf-8") as handle:
                return handle.read()
        except Exception:
            return ""
    return value or ""


def cmd_think(args):
    """One short reasoning turn on the local model. Returns text, or a REASON it could not."""
    prompt = _read_arg(args.prompt, args.prompt_file).strip()
    system = _read_arg(args.system, args.system_file).strip()
    if not prompt:
        print(json.dumps({"ok": False, "state": "no_prompt", "error": "nothing to think about"}))
        return

    text_in = (system + "\n\n" + prompt) if system else prompt
    if len(text_in) > THINK_MAX_CHARS:
        print(json.dumps({"ok": False, "state": "too_long", "chars": len(text_in),
                          "maxChars": THINK_MAX_CHARS,
                          "hint": "this prompt is bigger than a local model should be handed"}))
        return

    hermes = find_hermes()
    if not hermes:
        print(json.dumps({"ok": False, "state": "not_installed",
                          "hint": "Hermes is not installed on this computer"}))
        return

    cfg = read_user_model_config()
    model = args.model or cfg.get("default") or cfg.get("model")
    provider = args.provider or cfg.get("provider")
    base_url = args.base_url or cfg.get("base_url")
    context_length, _ = resolve_context(args.context_length, cfg, base_url, model)   # XY-FULLCONTEXT
    if not model:
        print(json.dumps({"ok": False, "state": "not_configured",
                          "hint": "no local model is set"}))
        return

    native = ollama_native_context(base_url, model)
    if native and native < MIN_CONTEXT:
        print(json.dumps({"ok": False, "state": "context_too_small", "model": model,
                          "nativeContext": native, "minContext": MIN_CONTEXT,
                          "hint": "%s was trained for %s tokens; Hermes needs %s"
                                  % (model, "{:,}".format(native), "{:,}".format(MIN_CONTEXT))}))
        return

    mode = "web" if args.web else "plain"
    home = think_home(mode, model, provider, base_url, context_length)
    env = enriched_env()
    env["HERMES_HOME"] = home
    env.pop("XYCY_RUN_DIR", None)          # a think is not a run; keep it out of the event feed

    argv = [hermes, "-z", text_in, "--yolo", "--accept-hooks"]
    if args.web:
        argv += ["-t", "web"]
    if args.reasoning:
        argv += ["--reasoning", args.reasoning]

    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=args.timeout, env=env, cwd=home)
    except subprocess.TimeoutExpired:
        print(json.dumps({"ok": False, "state": "timeout", "model": model,
                          "hint": "the local model did not answer within %ss" % args.timeout}))
        return
    except Exception as exc:
        print(json.dumps({"ok": False, "state": "no_reason", "hint": str(exc)}))
        return

    out = _ANSI.sub("", (proc.stdout or "")).strip()
    err = _ANSI.sub("", (proc.stderr or "")).strip()
    elapsed = round(time.monotonic() - started, 1)
    low = (out + "\n" + err).lower()

    if not out:
        state = "no_reason"
        if any(s in low for s in ("connection refused", "failed to connect", "could not connect",
                                  "connection error", "name or service not known")):
            state = "endpoint_down"
        elif any(s in low for s in ("api key", "unauthorized", "401", "authentication")):
            state = "not_configured"
        elif "below the minimum" in low or "tokens of runtime context" in low:
            state = "context_too_small"
        print(json.dumps({"ok": False, "state": state, "model": model,
                          "elapsedSec": elapsed,
                          "hint": " ".join(err.split())[:300] or "Hermes returned nothing"}))
        return

    print(json.dumps({"ok": True, "text": out, "via": "hermes", "model": model,
                      "web": bool(args.web), "elapsedSec": elapsed}))

# ===== XY-ONEDOC - what does Rhino actually have open right now? ==============================
# rhinomcp resolves every call to the ACTIVE document, and on macOS `-_Open` makes a second
# window rather than replacing the current one. Two documents means a step can build correctly
# and screenshot the other one. Nothing can pin it (RhinoCommon exposes no activate and no
# close - both are events, not methods), so the run states the fact and the brief says what to
# do about it.
RHINO_DEFAULT_PORT = 1999
_RHINO_LIST_CODE = (
    "import Rhino\n"
    "a=Rhino.RhinoDoc.ActiveDoc\n"
    "print([[(d.Name or ''), d.Objects.Count, bool(d.Modified),"
    " d.RuntimeSerialNumber==a.RuntimeSerialNumber] for d in Rhino.RhinoDoc.OpenDocuments()])\n"
)


def rhino_documents(port=None, timeout=6.0):
    """[{name, objects, modified, active}] or None when Rhino does not answer."""
    import ast, socket
    port = int(port or os.environ.get("RHINO_PORT") or RHINO_DEFAULT_PORT)
    payload = json.dumps({"type": "execute_rhinoscript_python_code",
                          "params": {"code": _RHINO_LIST_CODE}}).encode("utf-8")
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout)
    except Exception:
        return None
    try:
        sock.settimeout(timeout)
        sock.sendall(payload)
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            try:
                reply = json.loads(buf.decode("utf-8", "replace"))
                break
            except Exception:
                continue
        else:
            return None
    except Exception:
        return None
    finally:
        try:
            sock.close()
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:1788')
    try:
        out = (reply.get("result") or {}).get("output") or ""
        line = [l for l in out.splitlines() if l.strip().startswith("[")]
        if not line:
            return None
        rows = ast.literal_eval(line[0].strip())
    except Exception:
        return None
    docs = []
    for row in rows:
        try:
            docs.append({"name": row[0] or "(unsaved)", "objects": int(row[1]),
                         "modified": bool(row[2]), "active": bool(row[3])})
        except Exception:
            continue
    return docs or None


# A healthy Rhino sits around 30-55 threads. Past this it is measurably eating the machine
# and every turn slows down, which reads from the outside as a slow model.
RHINO_THREAD_ALARM = int(os.environ.get("XYCY_RHINO_THREAD_ALARM", "600") or 600)


def rhino_threads():
    """(pid, thread_count, uptime) for a running Rhino, or None. Never raises."""
    try:
        pid = subprocess.check_output(["pgrep", "-x", "Rhinoceros"]).split()[0].decode()
    except Exception:
        return None
    try:
        n = len(subprocess.check_output(["ps", "-M", pid]).decode().splitlines()) - 1
        up = subprocess.check_output(["ps", "-o", "etime=", "-p", pid]).decode().strip()
    except Exception:
        return None
    return {"pid": int(pid), "threads": n, "uptime": up}


def rhino_leak_notice(info):
    """The sentence the person sees in the run console. Empty when Rhino is healthy."""
    if not info or info["threads"] < RHINO_THREAD_ALARM:
        return ""
    return ("Rhino has been open a long time and is now running %d internal threads "
            "(a fresh Rhino runs about 40). Its MCP plug-in leaks one every time anything "
            "connects to it, and at this level Rhino eats the processor and everything in "
            "this run gets slower -- which looks like a slow model but is not. Quit and "
            "reopen Rhino when this run finishes." % info["threads"])


def rhino_state_brief(docs):
    """The plain-words paragraph that goes at the top of a Rhino step's prompt."""
    if not docs:
        return ""
    front = ([d for d in docs if d["active"]] or [docs[0]])[0]
    lines = ["RHINO RIGHT NOW - read this before your first tool call.",
             "Rhino has %d drawing%s open:" % (len(docs), "" if len(docs) == 1 else "s")]
    for d in docs:
        lines.append("  - \"%s\" - %d object%s%s%s"
                     % (d["name"], d["objects"], "" if d["objects"] == 1 else "s",
                        ", unsaved changes" if d["modified"] else "",
                        "   <- this one is in front" if d["active"] else ""))
    if len(docs) > 1:
        lines.append("Rhino answers EVERY one of your calls in whichever drawing is in front, and "
                     "that can change between calls. With more than one open you cannot build "
                     "safely and you cannot trust a screenshot. Do not open, create or close "
                     "anything: report status \"blocked\" with the reason "
                     "\"Rhino has %d drawings open - close all but one and run again\"."
                     % len(docs))
    else:
        lines.append("Exactly one drawing is open, which is what you want. Keep it that way: "
                     "build in \"%s\" and never open, create or close a drawing."
                     % front["name"])
    return "\n".join(lines) + "\n\n"


def pidfile(project_dir, run_id):
    return os.path.join(project_dir, "runs", run_id, "hermes.pid")


def cmd_start(args):
    project_dir = os.path.abspath(os.path.expanduser(args.dir))
    if not os.path.isdir(project_dir):
        die("project folder not found: %s" % project_dir)
    run_dir = os.path.join(project_dir, "runs", args.run_id)
    os.makedirs(os.path.join(run_dir, "shots"), exist_ok=True)

    hermes = find_hermes()
    if not hermes:
        die("hermes CLI not found — install it, or set HERMES_CLI")

    prompt = args.prompt
    if args.prompt_file:
        with open(os.path.expanduser(args.prompt_file), "r", encoding="utf-8") as handle:
            prompt = handle.read()
    if not prompt:
        # Same default contract as the Claude path: RUN.md is the instruction set.
        # XY-HZPATH - this said "runs/<id>/RUN.md ... you are already in its project
        # folder". Both halves fail a local model. Hermes presents its OWN per-run profile
        # home as the workspace, so a RELATIVE path resolves against ~/.xycy/hermes-runs/<id>
        # and finds nothing - and the parenthetical then tells the model to trust exactly the
        # root that is wrong. Measured 25 Aug on Diyara Tower: qwen3.6:35b-64k burned 61.8 min
        # over 5 sessions, nemotron-lightning-64k 10.6 min over 2 - both hunting RUN.md in the
        # profile home, both ending by ASKING THE USER where the run is, which is fatal in a
        # headless run with nobody watching. 0 steps and 0 outputs between them. Claude survives
        # by searching until it finds the file; that is a capability tax, not a contract.
        # So: absolute paths, name every folder, and say plainly that nobody is there to ask.
        _run_dir = os.path.join(project_dir, "runs", args.run_id)
        prompt = (
            "Execute the XYCY workflow run described in " + _run_dir + "/RUN.md\n"
            "READ THAT FILE FIRST, at that exact absolute path. It is the instruction set.\n"
            "\n"
            "Absolute paths for this run - use these, do not guess and do not search for them:\n"
            "  instructions : " + _run_dir + "/RUN.md\n"
            "  workflow plan: " + _run_dir + "/plan.json\n"
            "  source files : " + project_dir + "/inputs\n"
            "  write outputs: " + project_dir + "/outputs\n"
            "  step progress: " + _run_dir + "/progress-<stepId>.json\n"
            "\n"
            "Follow the step instructions in RUN.md exactly, including its HONESTY RULES: never "
            "mark a step done that did not run in this invocation, and never reuse a previous "
            "run's outputs.\n"
            "\n"
            "YOU ARE RUNNING UNATTENDED. There is no person to answer you. Never ask a question "
            "or request clarification - it ends the run with nothing done. If something is "
            "missing, or an application accepts calls but does no work, write that into that "
            "step's progress file with status \"blocked\" and the reason, and go on to the next step."
        )

    # XY-HZMODEL: `start` was the one command that never asked the machine what model it
    # has. With Auto picked on the canvas the page sends no model, the per-run home got an
    # empty model block, and Hermes fell through to its packaged default provider - no key,
    # 401, dead in about five seconds. Resolve exactly the way `probe` and `think` do.
    cfg = read_user_model_config()
    model = args.model or cfg.get("default") or cfg.get("model")
    provider = args.provider or cfg.get("provider")
    base_url = args.base_url or cfg.get("base_url")
    # XY-CLOUDAUTH2 - two ways a cloud route dies that both used to look like something
    # else, so say the real thing before anything is launched.
    #   1. `openai` is not a Hermes provider. Its OpenAI-shaped endpoints are reached as
    #      `custom`, so a caller sending the obvious name got "Unknown provider 'openai'"
    #      from deep inside auth.py with nothing pointing back here. Accept it instead.
    #   2. a remote endpoint with no key authenticates as the literal placeholder and
    #      returns 401 on every call - or, on the vision route, hangs. Refuse up front.
    if (provider or "").strip().lower() in ("openai", "open-ai", "oai"):
        provider = "custom"
    if not endpoint_is_local(base_url) and not os.environ.get("OPENAI_API_KEY"):
        die("this run is pointed at %s, which is not this machine, and no cloud API key "
            "is set. Add one in the XYCY Bridge settings (Cloud inference API key); "
            "without it every call comes back 401 and a vision call hangs instead of "
            "failing." % base_url, state="cloud_key_missing", baseUrl=base_url)
    # ollama_num_ctx FIRST: context_length is what Hermes believes the window is, while
    # ollama_num_ctx is what the runtime allocates - and it is usually the only one set.
    context_length, context_from = resolve_context(args.context_length, cfg, base_url, model)   # XY-FULLCONTEXT
    if not model:
        die("no model is configured for Hermes on this machine - run `hermes setup`, "
            "or set model.default")
    # XY-LOCALONLY, second surface. The picker marks a cloud model; this refuses to RUN one,
    # including one that arrives as an explicit --model or sits in the user's config as the
    # default. Refused before the profile is built and before anything is launched.
    cloud = cloud_model_reason(model)
    if cloud:
        die("%s %s. Pick a model that is installed on this machine." % (model, cloud),
            state="cloud_model_refused")

    mcp_env = json.loads(args.mcp_env) if args.mcp_env else {}
    servers, missing = select_servers(
        None if args.servers is None else [s.strip() for s in args.servers.split(",")],
        mcp_env,
    )
    # XY-APPDOC - ask every declared application what it HOLDS before the model is handed the
    # step. Only a definite "no document" refuses; anything else, including a probe that could
    # not run at all, lets the run go ahead and is written down in the run record.
    # XY-ONEDRIVER - claim the applications before anything else looks at them. A run that
    # cannot have the application must not probe it, launch it, or start a model against it.
    conflicts = claim_apps(servers, args.run_id)
    if conflicts:
        name, held = conflicts[0]
        die("%s is already being driven by run %s (pid %s). Two runs on one application is how "
            "a deliverable gets overwritten by the run that did the worse job - stop that run, "
            "or wait for it." % (name, held.get("runId"), held.get("pid")),
            state="application_busy", server=name, heldBy=held.get("runId"),
            heldPid=held.get("pid"))

    doc_refusals, doc_asked = check_apps_hold_a_document(servers)
    if doc_refusals:
        release_apps(servers, args.run_id)      # XY-ONEDRIVER - never hold what we will not run
        name, tool, said = doc_refusals[0]
        die("%s has no open document - %s answered %r. A step cannot drive an application "
            "that is not holding anything: open a document (or let XYCY open one) and run it "
            "again." % (name, tool, said),
            state="app_has_no_document", server=name, probe=tool, answer=said,
            probes=doc_asked)
    # XY-INPUTLOADED - and only after XY-APPDOC, because "holds nothing" is the better message.
    in_bad, in_asked = check_declared_inputs_are_open(servers, project_dir, args.run_id)
    if in_bad:
        release_apps(servers, args.run_id)
        srv, want_doc, holding = in_bad[0]
        die("%s is not holding %s - this step declares it as an input and it is not open. It is "
            "holding %s. A step that builds into the wrong document photographs the wrong "
            "document and reports success: open the input and run it again."
            % (srv, want_doc, ", ".join(holding) or "nothing this check could name"),
            state="declared_input_not_open", server=srv, wanted=want_doc, holding=holding)

    want = None
    if args.skills:
        want = [s.strip() for s in args.skills.split(",") if s.strip()]
    # XYCY_TOOL_FILTER=off puts the run back on Hermes' own deferral bridge with the whole
    # server surface - the A/B switch the measurements were taken with, and the escape hatch
    # if a step ever needs a tool its prompt does not name.
    named_tools = ({} if os.environ.get("XYCY_TOOL_FILTER", "").lower() == "off"
                   else tools_named_in(prompt, servers))
    # XY-CODEBYPAYLOAD - only servers the caller named, and only ones this run actually declares.
    allow_code = [s.strip() for s in (args.allow_code or "").split(",") if s.strip()]
    allow_code = [s for s in allow_code if s in servers and s in APP_CODE_ESCAPE_HATCHES]
    home, bridged, skills_info = build_home(
        args.run_id, servers, model, provider, base_url,
        context_length, args.max_tool_calls, project_dir, want, named_tools, allow_code,
    )
    if allow_code:
        try:
            with open(os.path.join(run_dir, "hermes.log"), "a", encoding="utf-8") as _h:
                _h.write("[xycy] XY-CODEBYPAYLOAD - code execution is ALLOWED on %s for this "
                         "run. XY-NOCODEGEN normally removes those tools; the caller asked for "
                         "them because the script is in the prompt, not in the model's head. "
                         "If this run's geometry is wrong, check the prompt carried a script "
                         "before you blame the model.\n" % ", ".join(allow_code))
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2015')

    # XY-PROMPTBYFILE, one level down. The Bridge now hands this runner its step's words in a
    # file rather than on a command line, because Windows caps a command line at 32,767
    # characters and every application step of Shelf Bracket Check was dying on that cap with
    # nothing but "spawn ENAMETOOLONG" to show for it (MEASURED on Sean's PC, 21 Sep 2026;
    # the brief for that step is 37,994 characters). Reading it here fixed the launch and
    # moved the same wall one process along: `hermes -z <prompt>` puts it straight back on a
    # command line, and the session ended before it wrote a single event.
    #
    # Hermes has no --prompt-file. What it does have is the contract this runner already
    # relies on when no prompt is given at all: an absolute path and a plain instruction to
    # read it. So when the words will not fit, they stay in the file the Bridge wrote and
    # Hermes is handed a short note pointing at it. The opening of the brief goes in the note
    # as well, so the model knows what it has been asked to do before it opens anything.
    #
    # Only when it will not fit: a step whose brief fits is handed its brief, which is one
    # fewer thing to go wrong on the common path.
    prompt_arg = prompt
    _limit = 30000 if os.name == "nt" else 120000
    if len(prompt) > _limit:
        _brief = os.path.join(run_dir, "hermes-prompt.txt")
        try:
            if not os.path.exists(_brief):
                with open(_brief, "w", encoding="utf-8") as _bh:
                    _bh.write(prompt)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:promptbyfile')
        prompt_arg = (
            "Your instructions for this step are too long to pass on a command line, so they "
            "are in this file:\n\n    " + _brief + "\n\nRead that WHOLE file first, with "
            "your file-reading tool, using exactly that path. Then do exactly what it says, "
            "including the report it asks you to write at the end. Nobody is watching this "
            "run, so never ask a question - if something is missing, say so in the report.\n\n"
            "It begins:\n\n" + prompt[:1500]
        )
        try:
            with open(os.path.join(run_dir, "hermes.log"), "a", encoding="utf-8") as _h:
                _h.write("[xycy] XY-PROMPTBYFILE - this step's brief is %d characters, over the "
                         "%d this platform allows on a command line, so Hermes was pointed at "
                         "%s instead. The whole brief is in that file.\n"
                         % (len(prompt), _limit, _brief))
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:promptbyfilelog')

    argv = [hermes, "-z", prompt_arg, "--in", project_dir, "--yolo", "--accept-hooks",
            "--usage-file", os.path.join(run_dir, "usage.json")]
    if model:
        argv += ["-m", model]
    if provider:
        argv += ["--provider", provider]
    if args.reasoning:
        argv += ["--reasoning", args.reasoning]
    if args.toolsets:
        argv += ["-t", args.toolsets]
    # XY-SKILLONLY - hand Hermes the FOLDER names that were actually bridged, never the raw
    # plan ids. Hermes resolves a declared skill by folder name and REFUSES TO START a step
    # whose skills it cannot resolve (XY-SKILLID, one argument further in), and a plan id like
    # `skreg:https://github.com/...` is not the folder `skreg_https___github.com_...`.
    # XY-SKILLLOUD - a bridge that failed open must SAY so where a person will see it. Measured
    # 26 Aug: a run id with no `__node` suffix cannot find its plan (the lookup takes the part
    # before `__`), so the filter fails open and bridges every skill on disk - 12 folders, about
    # 27,000 tokens of context, on a step that named none. It was invisible until two runs of
    # the same step were compared and one carried 45k tokens a turn against the other's 18k.
    if skills_info.get("source") == "unfiltered":
        try:
            with open(os.path.join(run_dir, "hermes.log"), "a", encoding="utf-8") as _h:
                _h.write("[xycy] XY-SKILLLOUD - this run's plan could not be read, so the "
                         "skills filter failed OPEN and every staged skill is bridged (%d). "
                         "A run id must be <parentRunId>__<node> for the plan lookup to "
                         "work.\n" % len(bridged))
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2087')
    if skills_info.get("source") == "unfiltered":
        skills = args.skills or ",".join(bridged)
    else:
        skills = ",".join(bridged)
    if skills:
        argv += ["-s", skills]
    if args.worktree:
        argv.append("--worktree")

    env = enriched_env()
    env["HERMES_HOME"] = home
    env["XYCY_RUN_DIR"] = run_dir          # arms the instrumentation plugin
    env["XYCY_RUN_ID"] = args.run_id
    # XY-WRITEGUARD - and arms the write guard, which reads these three and stands aside
    # without them. outputs/ is the project's, shared between steps by contract; the run
    # folder is this run's own. A step writes into one of those two and nowhere else, and
    # writes a program file only when the plan declares a script step.
    env["XYCY_OUTPUTS_DIR"] = os.path.join(project_dir, "outputs")
    env["XYCY_SCRIPT_STEP"] = "1" if _plan_declares_script(run_dir, project_dir, args.run_id) else "0"
    # XY-DOCLIBS2 - a step with no application has to AUTHOR its artifact, and on this product
    # that means Python: the xlsx, docx and pdf skills are Python and there is no other way to
    # make a real workbook or a real PDF. XY-DOCSTEPPY gives such a step code_execution back;
    # the import still has to resolve.
    #
    # MEASURED 19 Sep on Sean's PC (diag_docstep1): code_execution ran and answered
    # "ModuleNotFoundError: No module named 'reportlab'", and the model went on to type a PDF
    # out by hand rather than say it could not. So the check happens HERE, where the run knows
    # it has no application, rather than being left to the installer - the machines that need
    # it most are the ones where Hermes was installed before this existed.
    #
    # The cost when everything is present is one short subprocess; the install only runs when
    # something is actually missing, and a failure is recorded and does not stop the run,
    # because a step that can still read and write files is better than no step at all.
    doc_libs = None
    if not servers:
        doc_libs = _ensure_doc_libs_quietly()
    
    # XY-VISIONLOCAL, second half - and the half that actually does the work. Pointing
    # `auxiliary.vision` at the local endpoint is not enough on its own: Hermes resolves a
    # custom auxiliary endpoint as (base_url, api_key) and bails with
    #     if not custom_base or not custom_key: return None, None
    # so with no OPENAI_API_KEY in the environment the local route is silently declined and the
    # call falls through to the cloud chain and hangs. Ollama does not check the key and does
    # not want one; its presence is what keeps the resolver from walking past the local model.
    # Measured both ways on run_v6__n0, 27 Aug: config alone, 93 s with ollama at 0% CPU and no
    # end in sight; config plus this line, an answer in about 85 s.
    # Never overwrite a real key the user has set for a real endpoint.
    if not env.get("OPENAI_API_KEY") and (provider or "").lower() in ("custom", "ollama", ""):
        env["OPENAI_API_KEY"] = "ollama"

    # XY-HZSTART: this is READ here and only ASSIGNED ~40 lines below, in the XY-RHINOLEAK
    # block, so every cmd_start raised UnboundLocalError and no Hermes run ever launched.
    # Default it where it is first used; the block below still overwrites it when it has
    # something to say. A plain default is the whole fix - the block below still runs.
    env_notice = None
    if env_notice:
        env["XYCY_NOTICE"] = env_notice        # XY-RHINOLEAK: surfaced by the plugin
    shot = getattr(args, "shot", None)
    if not shot:
        # Derive it: a per-step sub-run is "<parentRunId>__n<node>" (with "r2" on a retry),
        # and its capture belongs in the PARENT run's shots folder, which is where the run
        # theater looks. A whole-workflow run has no single node, so it gets nothing here.
        m = re.match(r"^(.+?)__n(\d+)", args.run_id or "")
        if m:
            shot = os.path.join(project_dir, "runs", m.group(1), "shots", "n%s.png" % m.group(2))
    if shot:
        # XY-SHOTCOPY: the plugin copies each capture here as it happens, so the preview the
        # person watches is the app's own image rather than whatever the model managed to write.
        shot = os.path.abspath(os.path.expanduser(shot))
        try:
            os.makedirs(os.path.dirname(shot), exist_ok=True)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2160')
        env["XYCY_SHOT_PATH"] = shot
    if model:
        env["HERMES_INFERENCE_MODEL"] = model

    # XY-RHINOLEAK: how badly has Rhino's thread leak grown? Recorded either way.
    if "rhino" in servers:
        try:
            _leak = rhino_threads()
        except Exception:
            _leak = None
        if _leak:
            try:
                with open(os.path.join(run_dir, "rhino-threads.json"), "w",
                          encoding="utf-8") as _h:
                    json.dump(_leak, _h, indent=2)
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2177')
            _note = rhino_leak_notice(_leak)
            if _note:
                env_notice = _note
            else:
                env_notice = None
        else:
            env_notice = None
    else:
        env_notice = None

    # XY-ONEDOC: look at Rhino before the step does, record it, and say it in the brief.
    if "rhino" in servers:
        try:
            _docs = rhino_documents((mcp_env.get("rhino") or {}).get("RHINO_PORT"))
        except Exception:
            _docs = None
        if _docs:
            try:
                with open(os.path.join(run_dir, "rhino-docs.json"), "w",
                          encoding="utf-8") as _h:
                    json.dump({"at": time.time(), "documents": _docs}, _h, indent=2)
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2200')
            prompt = rhino_state_brief(_docs) + (prompt or "")

    log_path = os.path.join(run_dir, "hermes.log")
    try:
        log = open(log_path, "w")
    except Exception as exc:
        die("couldn't open log: %s" % exc)

    # Detached so the caller returns immediately; XYCY polls the run dir.
    popen_kwargs = {"cwd": project_dir, "env": env, "stdin": subprocess.DEVNULL,
                    "stdout": log, "stderr": subprocess.STDOUT}
    # XY-HZFINISH: spawn the supervisor rather than Hermes itself. It owns the process group
    # and the pid file - so Stop, liveness and the status poll all behave exactly as before -
    # and it runs Hermes again when a session ends with the plan unfinished.
    try:
        with open(os.path.join(run_dir, "hermes-appdoc.json"), "w", encoding="utf-8") as handle:
            json.dump({"runId": args.run_id, "asked": doc_asked,
                       "refused": [n for n, _, _ in doc_refusals],
                       "inputsChecked": in_asked}, handle, indent=2)
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2221')

    spec_path = os.path.join(run_dir, "hermes-supervise.json")
    try:
        with open(spec_path, "w", encoding="utf-8") as handle:
            json.dump({"argv": argv, "runDir": run_dir, "runId": args.run_id,
                       "log": log_path, "cwd": project_dir,
                       # XY-DONEZERO needs to know whether this step DRIVES an application.
                       # A document step legitimately finishes with built-in tools only - the
                       # operating brief says so in as many words - so the rule may only bite
                       # where a server was actually declared.
                       "servers": sorted(servers.keys()) if isinstance(servers, dict)
                                  else list(servers or []),
                       "maxAttempts": SUPERVISE_MAX}, handle, indent=2)
        sup_env = dict(env)
        sup_env[SUPERVISE_ENV] = spec_path
        sup_kwargs = dict(popen_kwargs)
        sup_kwargs["env"] = sup_env
        # the supervisor opens the run log itself, one attempt after another; its own
        # stderr goes somewhere separate so a fault in it is never mistaken for the model's
        try:
            sup_log = open(os.path.join(run_dir, "hermes-supervise.log"), "w")
        except Exception:
            sup_log = subprocess.DEVNULL
        sup_kwargs["stdout"] = sup_log
        sup_kwargs["stderr"] = subprocess.STDOUT
        # XY-HZWORKER - the run id goes on the SUPERVISOR'S COMMAND LINE, where the agent can
        # see it. It is passed by environment (XYCY_HZ_SUPERVISE names the spec file) and that
        # is how main() finds it, so this argument is never parsed and never read here. It is
        # there to be READ FROM OUTSIDE, by the one function that decides whether a run still
        # has a worker: pidIsRun() in the agent asks whether the run id appears in a process's
        # command line, and for a Hermes run it never did.
        #
        # What that costs, measured on Sean's PC, 18-19 Sep 2026: runHasLiveWorker() was false
        # for EVERY Hermes run, always, so release_run_apps - which is written to refuse while
        # a run still has a live worker - never refused. A guard that cannot see the thing it
        # guards is worth fixing on its own account.
        #
        # AND THE CLAIM STOPS THERE. An earlier version of this note said the teardown had
        # force-quit Rhino out from under two live runs and that this was why they died. That
        # went further than the evidence. Looked at again on 18 Sep with the event feeds open,
        # every one of those runs ends on an `api_start` - waiting on the MODEL, not part-way
        # through a tool call - and a force-quit of Rhino would have shown as a failing tool
        # call. None of them has one. So this fixed a real blind spot in the guard; it is not
        # known to be what killed those runs, and what is remains open.
        child = spawn_outliving([sys.executable, os.path.abspath(__file__),
                                 "--supervising", str(args.run_id)], sup_kwargs)
    except Exception:
        child = spawn_outliving(argv, popen_kwargs)   # never let the run fail to start

    with open(pidfile(project_dir, args.run_id), "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))

    # XY-ONEDRIVER - the claim was written under THIS process, which is about to exit. Re-stamp
    # it with the supervisor's pid, or the lock would go stale the moment `start` returns and
    # the next run would sail straight past it. (Our own run id is never a conflict with itself.)
    claim_apps(servers, args.run_id, pid=child.pid)

    print(json.dumps({
        "ok": True, "harness": "hermes", "pid": child.pid, "runId": args.run_id,
        "hermesHome": home, "model": model, "provider": provider,
        "brokeAwayFromJob": SPAWN_LAST_FLAGS.get("breakaway"),
        "contextLength": context_length, "contextFrom": context_from,   # XY-FULLCONTEXT
        "servers": sorted(servers.keys()),
        "missingServers": missing, "skillsBridged": bridged,
        "toolsKept": named_tools or None,
        "codeAllowed": allow_code or None,          # XY-CODEBYPAYLOAD
        "docLibs": doc_libs,                        # XY-DOCLIBS2, None on an application step
        # XY-SKILLONLY - `skillsBridged` used to answer a different question than its name
        # asked: it reported what was SYMLINKED while `-s` decided what was LOADED, so it
        # said 13 for a run that loaded 2. They are the same list now, and what was asked
        # for is reported beside it rather than inferred from it.
        "skillsWanted": skills_info.get("wanted"), "skillsSource": skills_info.get("source"),
        "skillsUnresolved": skills_info.get("unresolved"),
        "skillsAvailable": skills_info.get("available"),
        "events": os.path.join(run_dir, "hermes-events.ndjson"), "log": log_path,
    }))


# XY-HZPIDWRONG - how long a run may go without writing about itself before a missing process
# is allowed to mean it has stopped. See the note in cmd_status for what was measured.
HZ_HEARTBEAT_STALE_SEC = 120
# XY-HZWARMUP - and how long a run that has not written about itself YET is allowed to be
# starting rather than dead. A run has nothing to say until Hermes has loaded its model and
# opened its session; on this machine a cold 22 GB model took 30 seconds to load before the
# first token. Measured on Sean's PC, 19 Sep 2026: the page called a run Failed at 0:45 -
# "no step started, 3 steps left to run" - while that same run went on to finish step 1 and
# was still working four minutes later. Not-yet is not no.
HZ_WARMUP_SEC = 120


def read_pid(project_dir, run_id):
    try:
        with open(pidfile(project_dir, run_id), "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except Exception:
        return None


def alive(pid):
    # XY-KILLNOTASK - this used to be os.kill(pid, 0), which on Windows kills what it is
    # asked about. See the note beside pid_is_alive at the top of this file.
    return pid_is_alive(pid)


def cmd_status(args):
    project_dir = os.path.abspath(os.path.expanduser(args.dir))
    run_dir = os.path.join(project_dir, "runs", args.run_id)
    pid = read_pid(project_dir, args.run_id)
    events = []
    events_path = os.path.join(run_dir, "hermes-events.ndjson")
    try:
        with open(events_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-args.tail:]
        for line in lines:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception as _xy_e:
                    say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2341')
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2343')
    # XY-HEARTBEAT · "is there a process" is not "is the work still going".
    # Watched from a web page, a run that had ENDED at 26s still read alive=true at 60s,
    # because the child lingers after the session closes. Liveness is now the conjunction,
    # and the raw process fact is still reported separately rather than hidden.
    status = load_json(os.path.join(run_dir, "hermes-status.json"))
    process_alive = alive(pid)
    finished = bool((status or {}).get("finishedAt"))
    # XY-HZPIDWRONG - the pid in hermes.pid is the one this helper STARTED. It is not always the
    # one doing the work, and when it is not, "that process is gone" is not "the run is over".
    #
    # MEASURED on Sean's PC, 18 Sep 2026, twice. run_1789759976826, sampled live: hermes.pid held
    # 10488 and that process was gone; the run's OWN status file said pid 21072, state "waiting",
    # 8 api calls, and had been written 1.5 seconds earlier; and a hermes process was running as
    # 13072. Three pids, and the only one this looked at was the dead one. The page read
    # alive:false and painted "Failed - 0 of 3 steps did not finish - 0:44" over a run that went
    # on working for another minute and a half. The first run of the day did the same thing.
    #
    # So liveness now has two witnesses and takes either: a live process, or a heartbeat. The
    # heartbeat is the run writing about itself, which is the strongest evidence there is that it
    # is still going, and it is what XY-HEARTBEAT above already reads for the opposite case - a
    # child that lingers after the work has ENDED, where `finished` is what rules it out. Both
    # answers are reported separately, so a caller can always see which witness spoke.
    #
    # The window is deliberately generous. Between api calls this run went 19.8 seconds without
    # writing, and a "waiting" state can be longer still; two minutes is long enough never to
    # call a thinking model dead, and short enough that a run that really has stopped is noticed
    # before anyone has gone to make tea.
    status_pid = (status or {}).get("pid")
    status_pid_alive = alive(status_pid) if status_pid and status_pid != pid else False
    beat_at = (status or {}).get("updatedAt")
    beat_age = round(time.time() - beat_at, 1) if isinstance(beat_at, (int, float)) else None
    beat_fresh = bool(beat_age is not None and beat_age <= HZ_HEARTBEAT_STALE_SEC
                      and (status or {}).get("state") not in ("finished", "failed", None))
    any_process = process_alive or status_pid_alive
    # XY-HZWARMUP - a third witness, for the one window where neither of the other two can
    # speak: the run has been asked for, the supervisor wrote down when it started, and
    # nothing has reported yet because nothing has had time to. `startedAt` is read below for
    # the wall clock; it is read here first because a run that is still starting is not a run
    # that has stopped.
    _att_started = (load_json(os.path.join(run_dir, "hermes-attempts.json")) or {}).get("startedAt")
    warming = bool(_att_started and not (status or {}).get("updatedAt")
                   and (time.time() - _att_started) < HZ_WARMUP_SEC)
    live_now = (not finished) and (any_process or beat_fresh or warming)
    # Say which witness spoke, whenever they do not agree. A caller that stops a run on this
    # answer should never have to guess whether it was a pid or a heartbeat that decided.
    why = ""
    if warming and not any_process and not beat_fresh:
        why = ("this run was started " + str(round(time.time() - _att_started, 1))
               + "s ago and has not written about itself yet")
    elif live_now and not any_process:
        why = ("no process from this run is still running, but the run wrote about itself "
               + str(beat_age) + "s ago and says it is " + str((status or {}).get("state")))
    elif (not live_now) and not finished:
        why = ("nothing from this run is running and "
               + ("it has not written about itself for " + str(beat_age) + "s"
                  if beat_age is not None else "it has never written about itself"))
    # XY-HZWALL - status["elapsedSec"] is the CHILD's clock and resets on every retry, so a run
    # an hour old reports minutes the moment it re-attempts. The supervisor's own start is in
    # hermes-attempts.json; report that as the run's wall clock and say which attempt it is on,
    # so a caller timing a run is never handed the last retry's stopwatch by mistake.
    att = load_json(os.path.join(run_dir, "hermes-attempts.json")) or {}
    wall = att.get("wallClockSec")
    if att.get("startedAt") and live_now:
        wall = round(time.time() - att["startedAt"], 1)   # live run: the file is only as fresh as the last attempt
    print(json.dumps({
        "ok": True, "alive": live_now,
        "processAlive": process_alive, "finished": finished,
        "statusPid": status_pid, "statusPidAlive": status_pid_alive,
        "heartbeatAgeSec": beat_age, "heartbeatFresh": beat_fresh, "warming": warming, "why": why,
        "state": (status or {}).get("state") or ("finished" if finished else None),
        "pid": pid,
        "wallClockSec": wall,
        "attempt": len(att.get("attempts") or []) + (1 if live_now else 0),
        "status": status,
        "usage": load_json(os.path.join(run_dir, "usage.json")),
        "progress": load_json(os.path.join(run_dir, "progress.json")),
        "events": events,
    }))


def cmd_stop(args):
    project_dir = os.path.abspath(os.path.expanduser(args.dir))
    pid = read_pid(project_dir, args.run_id)
    if not alive(pid):
        print(json.dumps({"ok": True, "stopped": False, "reason": "not running"}))
        return
    # Kill the whole TREE, not just the parent: Hermes launches the app MCP servers as its own
    # children, and killing only the parent leaves uvx/npx servers holding their ports — the next
    # run then fails to bind and looks like a XYCY bug.
    # os.killpg does not exist on Windows, so this used to raise AttributeError and the Stop
    # button did nothing at all there. Each platform gets the call it actually has.
    if os.name == "nt":
        # taskkill /T takes the children with it; /F because a headless agent has no console to
        # deliver a graceful signal through.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=60)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2447')
    for _ in range(20):
        if not alive(pid):
            break
        time.sleep(0.25)
    if alive(pid) and os.name != "nt":
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2456')
    print(json.dumps({"ok": True, "stopped": True, "pid": pid}))


# ------------------------------------------------------------------ XY-HZFINISH
# A run ends when the PLAN is finished, not when the model stops talking. See the
# note at the top of scripts/patch_hzfinish_0818.py for the three measured runs
# that made this necessary.
# ------------------------------------------------------------------ XY-HZDETACH
# A run has to OUTLIVE the call that started it. On macOS that was one keyword,
# start_new_session=True, and on Windows it was nothing at all: the line read
# `if os.name == "posix"` and there was no else. So on the platform this product is
# mainly used on, the supervisor was an ordinary descendant of the desktop app's own
# process, inside its job, and something up that chain reaped the pair of them.
#
# MEASURED on Sean's PC, 18 Sep 2026, two Hermes workflow runs. Run 2 (run_1789759976826):
# Ollama served EVERY request 200, the last one finishing at 13:36:18 after 23.0 s of
# work - and hermes, having received a good answer, was gone. hermes.log empty,
# hermes-supervise.log empty, hermes-attempts.json holding an empty list, no Windows
# error event, and the supervisor gone with it. That is not a crash: a crash writes an
# event and a non-zero exit the supervisor would have recorded. It is an outside kill of
# the whole tree.
#
# The corroboration was on the same machine the same afternoon. Three separate attempts
# to start a long job from the agent - the gate, twice, and a watcher - were killed the
# same silent way, while the same job started from a Windows scheduled task ran for nine
# minutes; and Ollama, which XY-OLLAMAHERE starts with exactly these flags, had been
# serving for hours.
#
# DETACHED_PROCESS gives it no console to be signalled through, CREATE_NEW_PROCESS_GROUP
# takes it out of the caller's group, and CREATE_BREAKAWAY_FROM_JOB takes it out of the
# caller's job object - which is the one that matters here. A job may forbid breakaway,
# and then CreateProcess refuses outright, so that flag is TRIED and dropped rather than
# assumed: a run that starts inside the job is still better than a run that does not start.
#
# Stop is unaffected: cmd_stop on Windows is `taskkill /PID <supervisor> /T /F`, which
# walks the parent-child tree, and a detached child still records the process that created
# it. The supervisor's own children - hermes, and the app MCP servers under it - are still
# its children and still go down with it.
WIN_DETACHED_PROCESS          = 0x00000008
WIN_CREATE_NEW_PROCESS_GROUP  = 0x00000200
WIN_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


SPAWN_LAST_FLAGS = {"breakaway": None}   # None = not Windows, or nothing spawned yet


def spawn_outliving(argv, kwargs):
    """Start a process meant to outlive this one, on either platform."""
    if os.name != "nt":
        k = dict(kwargs)
        k["start_new_session"] = True
        return subprocess.Popen(argv, **k)
    base = WIN_DETACHED_PROCESS | WIN_CREATE_NEW_PROCESS_GROUP
    for flags in (base | WIN_CREATE_BREAKAWAY_FROM_JOB, base):
        k = dict(kwargs)
        k["creationflags"] = flags
        try:
            child = subprocess.Popen(argv, **k)
            # Hostile review, 19 Sep: the fallback was silent, so a run that stayed inside the
            # job looked identical to one that broke away. Record which happened.
            SPAWN_LAST_FLAGS["breakaway"] = bool(flags & WIN_CREATE_BREAKAWAY_FROM_JOB)
            return child
        except OSError as exc:
            last = exc
            continue
    raise last


SUPERVISE_ENV = "XYCY_HZ_SUPERVISE"
SUPERVISE_MAX = 4

RESUME_PROMPT = (
    "The previous session ended before this run was finished. Nothing is lost - the "
    "application still holds everything that was already built, and it must stay there.\n\n"
    "1. Read %(run_md)s.\n"
    "2. Read %(progress)s and work out which steps are NOT yet marked done.\n"
    "3. Continue from the first of those. Do NOT start over, do NOT clear or delete "
    "anything, and do NOT redo a step that is already marked done.\n"
    "4. Keep calling your tools until the work is actually made in the application - a "
    "described result is a failed step.\n"
    "5. Write each step's status into %(progress)s as you finish it, and set the "
    "top-level \"status\" when the whole plan is done or blocked. A run that never sets "
    "it reads as unfinished, however much was built.\n\n"
    # XY-RESUMEBLIND - the door from the model's side. The supervisor no longer sends this
    # prompt when both files are missing, but a file can also be unreadable or empty.
    "IF YOU CANNOT READ EITHER FILE, STOP. Two missing files are not a plan. Do not work "
    "out what this run was for from the application, the folder, the project name or the "
    "file names, and do not build ANYTHING. Say that both files were missing and set the "
    "status to \"blocked\". A resume with nothing to resume from once spent twenty-five "
    "minutes putting a thousand objects nobody asked for into a live document."
)


# ------------------------------------------------------------------ XY-HZSTALL
# _supervise() used to call child.wait(), which blocks until the child EXITS. A
# child that HANGS - one model call issued and never answered - never exits, so the
# retry loop, RESUME_PROMPT and the terminal-status check below all sit unreachable
# behind that one line. Measured 25 Aug on Diyara Tower n7 (qwen3.6:35b-64k): the
# step had already built 30 objects in Blender, then waited 82 minutes on a single
# api call against a declared 14-minute budget.
#
# waitingOn/waitingSec are written by the xycy_progress plugin on every event, so
# "waiting on the model for longer than any real answer takes" is a signal we
# already have. Killing the child hands the run to the retry loop, which resumes
# from progress.json - nothing built is lost, because it lives in the application.
STALL_ENV = "XYCY_HZ_STALL_SEC"
STALL_DEFAULT = 900
# XY-HZSTALL2 - a TOOL that never returns hangs the run exactly as a model that never answers
# does, and the original watchdog watched only the model. Measured 26 Aug on run_qwen_n4: over two
# minutes on `waitingOn: "vision_analyze"` with nothing recorded, invisible to the check above.
# Its own limit, because a slow render and a slow answer are different events.
TOOL_STALL_ENV = "XYCY_HZ_TOOLSTALL_SEC"
TOOL_STALL_DEFAULT = 600


def _stall_limit():
    """Seconds to allow one outstanding model call. 0 disables the watchdog."""
    try:
        value = int(os.environ.get(STALL_ENV) or STALL_DEFAULT)
    except Exception:
        return STALL_DEFAULT
    return value if value > 0 else 0


def _tool_stall_limit():
    """Seconds to allow one outstanding TOOL call. 0 disables that half of the watchdog."""
    try:
        value = int(os.environ.get(TOOL_STALL_ENV) or TOOL_STALL_DEFAULT)
    except Exception:
        return TOOL_STALL_DEFAULT
    return value if value > 0 else 0


# XY-STEPDONE. The step prompt ends with "write the verdict and stop - no closing summary".
# Measured 27 Aug: the models write it and then keep going. n3 wrote `done` at about 445 s and
# was still calling FreeCAD at 578 s; n2a wrote `done` and ran on for another 70 s; every step
# of the 26 Aug full run ended with a 15-35 s epilogue turn. The instruction is not enforceable
# by asking, so it is enforced here.
#
# NOT the moment the file appears - a grace window, because a step legitimately photographs the
# result after writing its verdict (XY-LOOKED asks it to) and cutting that off would destroy the
# picture. The run ends when the verdict is terminal AND nothing has touched the application for
# STEP_DONE_GRACE seconds.
STEP_DONE_GRACE = float(os.environ.get("XYCY_STEP_DONE_GRACE", "25") or 25)


def _wait_watching_for_stall(child, status_path, stop_path, log_path, run_dir=None):
    """Wait for the child, but do not wait forever on a model that stopped answering.

    Returns (exit_code, stalled). A person's Stop is never treated as a stall:
    cmd_stop owns that kill, and this just keeps waiting for the tree to go down.
    """
    model_limit, tool_limit = _stall_limit(), _tool_stall_limit()
    verdict_at, calls_at_verdict = None, None
    while True:
        try:
            # 5 s, not 15: this loop is now also how XY-STEPDONE notices that a finished step
            # has gone quiet, and a 15 s poll made the grace window a 15-30 s window instead.
            return (child.wait(timeout=5), False)
        except subprocess.TimeoutExpired:
            # NOT a swallowed failure, and the one empty handler in this file that is meant to
            # be empty: this timeout IS the loop's beat. Five seconds have passed and the child
            # is still working, which is the normal case on every pass but the last one. The
            # silent-handler count therefore never reaches zero here, and should not.
            pass
        if os.path.exists(stop_path):
            continue
        status = load_json(status_path) or {}
        # XY-STEPDONE - the step said it was finished; hold it to that.
        if run_dir:
            done = any(_terminal_status(d.get("status")) for _, d in _step_verdicts(run_dir))
            calls = _app_calls(run_dir)
            if done and verdict_at is None:
                verdict_at, calls_at_verdict = time.time(), calls
            if done and calls != calls_at_verdict:
                verdict_at, calls_at_verdict = time.time(), calls   # it is still working
            if done and verdict_at and (time.time() - verdict_at) >= STEP_DONE_GRACE:
                try:
                    with open(log_path, "a") as handle:
                        handle.write("\n[xycy] XY-STEPDONE - the step wrote a terminal verdict "
                                     "and has not touched its application for %d s. Ending the "
                                     "session; the verdict stands.\n" % int(STEP_DONE_GRACE))
                except Exception as _xy_e:
                    say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2640')
                try:
                    child.terminate()
                    try:
                        child.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=20)
                except Exception as _xy_e:
                    say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2649')
                code = child.returncode
                return (code if code is not None else 0, False)
        # XY-HZSTALL2 - watch whatever it is waiting ON, not only the model. An empty waitingOn
        # means it is working, and working is never a stall however long it takes.
        waiting_on = str(status.get("waitingOn") or "")
        if not waiting_on:
            continue
        limit = model_limit if waiting_on == "model" else tool_limit
        if not limit:
            continue
        # XY-STALLBLIND. `waitingSec` comes out of hermes-status.json, and the watchdog was
        # treating that file as a clock. It is not one. The xycy_progress plugin refreshes it on
        # every event and otherwise on a three-second heartbeat thread - and the heartbeat is
        # inside the very process that is wedged, so the moment the process stops behaving, the
        # number stops moving and the watchdog reads the last one written for ever.
        #
        # MEASURED on Sean's PC, 19 Sep 2026, both in the Grading step of Survey to Permit
        # Report (qwen3.6:35b, Rhino):
        #   run_1789844358180__n2 - api_start 13:12:02, status last written 13:12:04 saying
        #     waitingSec 2.4, no event ever again. GPU still 72% at 13:23:44, 0% at 13:24:13,
        #     process alive at 13:28:23 and the file still saying 2.4. Sixteen minutes and
        #     twenty-one seconds into a 900-second limit, the watchdog was reading 2.4.
        #   run_1789848283241__n2 - a healthy run, and the heartbeat still went silent for 86
        #     seconds during one model wait with waitingSec left at 0. So the three-second
        #     refresh is not something to rely on even when nothing is wrong.
        #
        # The wait is what the file last said PLUS how long the file has said nothing since.
        # updatedAt is the plugin's own clock and sits in the same file; the file's mtime is the
        # fallback for a status written before updatedAt existed. When the heartbeat is doing
        # its job the second term is about three seconds and this changes nothing.
        try:
            waited = float(status.get("waitingSec") or 0)
        except Exception:
            continue
        try:
            said_at = float(status.get("updatedAt") or 0)
        except Exception:
            said_at = 0.0
        if said_at <= 0:
            try:
                said_at = os.path.getmtime(status_path)
            except Exception:
                said_at = 0.0
        if said_at > 0:
            waited += max(0.0, time.time() - said_at)
        if waited < limit:
            continue
        try:
            with open(log_path, "a") as handle:
                handle.write("\n[xycy] XY-HZSTALL - no answer from %s for %d s "
                             "(limit %d s); ending this attempt so the run can resume\n"
                             % ("the model" if waiting_on == "model" else "tool " + waiting_on,
                                int(waited), limit))
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2704')
        try:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=20)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2713')
        code = child.returncode
        return (code if code is not None else -1, True)


def _step_verdicts(run_dir):
    """Every `progress-<node>.json` in this run directory, as (filename, dict).

    XY-STEPVERDICT. The supervisor has only ever read `progress.json`. A PER-STEP run never
    writes that file - it writes `progress-<node>.json` - so `progressStatus` was null on all
    eight runs of the 26 Aug full workflow and the base file was then stamped
    "the model ended its session without finishing this run" over steps that had passed.
    Could-not-ask read as not-done, one more time.

    XY-STEPPARENT. And it still could not see them, because it looks in the WRONG FOLDER.
    A per-step run's own directory is `runs/<parent>__n<node>/`, and the step prompt sends the
    verdict - absolutely, by XY-PROGABS - to `runs/<parent>/progress-n<node>.json`, one level
    up. So the scan below found nothing on every per-step run, and all three things that read
    it silently believed the step had said nothing: XY-STEPDONE never cut the epilogue off,
    XY-DONEZERO could never downgrade a false `done`, and - the expensive one - the supervisor
    RE-RAN the step. Measured 1 Sep on the first gpt-5 run: n6 wrote {"status":"done"} with a
    real 5-tab workbook on disk and was launched three times anyway, at full price each.

    Only ever THIS step's file in the parent: the parent folder also holds the verdicts of
    every step that has already finished, and reading those as this one's would make every
    step after the first report done before it started.
    """
    out = []
    dirs = [(run_dir, None)]
    m = re.match(r"^(.+?)__n(\d+)", os.path.basename(os.path.normpath(run_dir or "")))
    if m:
        dirs.append((os.path.join(os.path.dirname(os.path.normpath(run_dir)), m.group(1)),
                     "progress-n%s.json" % m.group(2)))
    for folder, only in dirs:
        try:
            names = sorted(os.listdir(folder))
        except Exception:
            continue
        for name in names:
            if only is not None:
                if name != only:
                    continue
            elif not (name.startswith("progress-") and name.endswith(".json")):
                continue
            data = load_json(os.path.join(folder, name))
            if isinstance(data, dict) and data.get("status"):
                out.append((name, data))
    return out


# The calls a model makes to find out what a server offers, rather than to use it. Kept short
# and exact on purpose: anything that reads the open document is real work and stays out of it.
DISCOVERY_TOOLS = ("describe_capabilities", "list_tools", "get_capabilities")


def _app_calls(run_dir):
    """How many calls this run made to an APPLICATION. None means "I could not ask".

    A tool whose name starts with `mcp__` is a server call; `write_file`, `todo` and
    `vision_analyze` are the harness talking to itself. The distinction is the whole point:
    `counts.tool_calls` was 3 on the run that drove nothing at all.

    XY-ASKINGISNOTDRIVING. Asking a server what it can do is not using it. MEASURED on Sean's
    PC, 18 Sep 2026, run_1789793077905: 103 tool calls, of which exactly two went to Rhino -
    `describe_capabilities` and `get_document_summary` - and the document summary came back
    empty because nothing had been built in it. The step then reported done with "Grading &
    Drainage computed via Python (Rhino unavailable)", and no .3dm was written. Two calls was
    enough to get past XY-DONEZERO, which only asks whether the count is zero. A capabilities
    query is the one call that is unambiguously the model reading the manual rather than doing
    the work, so it does not count towards having driven anything. Anything that reads the
    open document still does: a step whose job is to look at a model and report is a real step.
    """
    path = os.path.join(run_dir, "hermes-events.ndjson")
    if not os.path.exists(path):
        return None
    n = 0
    try:
        with io.open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"tool_start"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("kind") != "tool_start":
                    continue
                tool = str(event.get("tool") or "")
                if not tool.startswith("mcp__"):
                    continue
                if tool.rsplit("__", 1)[-1] in DISCOVERY_TOOLS:
                    continue      # XY-ASKINGISNOTDRIVING
                n += 1
    except Exception:
        return None
    return n


def _enforce_done_needs_a_call(run_dir, servers, log_path):
    """XY-DONEZERO - a `done` from a step that never called its application is refused.

    26 Aug, glm-4.7-flash on n6: zero application calls, three `write_file`s, and a verdict
    reading {"status":"done","summary":"Excel tools not in tool list"} - a claim its own
    config disproved. Every check we own passed it, because every check we own reads the
    RESULT of a call and this step made none. The verdict file is the model's uncontested
    word about itself; this is the only thing that contradicts it.

    Deliberately one-sided and narrow, the same shape as XY-OKFAILED:
      * it may only ever DOWNGRADE a done, never hand one out;
      * it needs a declared server - a document step that finishes with built-in tools is
        legitimate and the operating brief says so;
      * "I could not read the feed" is not zero. Only a feed that exists and holds no
        application call accuses anybody;
      * the model's own claim is kept beside the new status, so a wrong rule here is
        visible and reversible.
    """
    if not servers:
        return []
    calls = _app_calls(run_dir)
    if calls is None or calls > 0:
        return []
    changed = []
    for name, data in _step_verdicts(run_dir):
        if str(data.get("status") or "").strip().lower() != "done":
            continue
        data["claimed"] = {"status": "done", "summary": data.get("summary"),
                           "gaps": data.get("gaps")}
        data["status"] = "blocked"
        data["blockedBy"] = "XY-DONEZERO"
        data["appCalls"] = 0
        data["summary"] = ("reported done, and this run made NO call to %s at all - the work "
                           "cannot have happened here. The model's own claim is kept in "
                           "`claimed`." % ", ".join(servers))
        try:
            with open(os.path.join(run_dir, name), "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1)
            changed.append(name)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2851')
    # XY-DONEZEROBASE - and the same for the BASE progress.json, which is the only place a
    # whole-workflow run ever writes a verdict.
    #
    # The loop above reads `progress-<node>.json`, and only a PER-STEP run writes those. A run
    # started from the board as one piece of work writes `progress.json` with a `steps` array
    # instead, so XY-DONEZERO has never once run on that whole class of run. MEASURED on Sean's
    # PC, 18 Sep 2026, run_1789793077905: two steps reported done in the base file, the run made
    # no call that changed anything in Rhino, and no .3dm was written. Nothing looked, so nothing
    # objected - which is the shape of mistake this file exists to stop.
    # Two corrections from the hostile review of 19 Sep, the same day this arm was written:
    #   (a) it downgraded EVERY done step, including a step that declares no application - a
    #       survey step that reads a CSV with built-in tools is a real step and must not be
    #       accused. Only a step whose plan node names a server is held to the rule. plan.json
    #       is in the run folder and is the only place a step's server is written down.
    #   (b) it rewrote the steps and left the run's own top-level status as "done", so the page
    #       would have painted a zero-call run green with a blocked step inside it - the exact
    #       lie the arm exists to stop, produced by the arm. A run with a step it just blocked
    #       is blocked.
    base_path = os.path.join(run_dir, "progress.json")
    base = load_json(base_path)
    if isinstance(base, dict) and isinstance(base.get("steps"), list):
        needs_app = set()
        plan = load_json(os.path.join(run_dir, "plan.json")) or {}
        for node in (plan.get("nodes") or []):
            if isinstance(node, dict) and str(node.get("server") or "").strip():
                needs_app.add(str(node.get("id") or ""))
        hit = []
        for step in base["steps"]:
            if not isinstance(step, dict):
                continue
            if str(step.get("status") or "").strip().lower() != "done":
                continue
            node_id = str(step.get("node") or "")
            if node_id not in needs_app and node_id.lstrip("n") not in needs_app:
                continue      # (a) no application declared on this step - not this rule's business
            step["claimed"] = {"status": "done", "summary": step.get("summary"),
                               "gaps": step.get("gaps")}
            step["status"] = "blocked"
            step["blockedBy"] = "XY-DONEZERO"
            step["appCalls"] = 0
            step["summary"] = ("reported done, and this run made NO call to %s that changed "
                               "anything - the work cannot have happened here. The model's own "
                               "claim is kept in `claimed`." % ", ".join(servers))
            hit.append(node_id or "?")
        if hit:
            if str(base.get("status") or "").strip().lower() == "done":
                base["claimedStatus"] = "done"
                base["status"] = "blocked"          # (b)
            try:
                with open(base_path, "w", encoding="utf-8") as handle:
                    json.dump(base, handle, indent=1)
                changed.append("progress.json(" + ", ".join(hit) + ")")
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:donezerobase')
    if changed:
        try:
            with open(log_path, "a") as handle:
                handle.write("\n[xycy] XY-DONEZERO - %s said done with 0 application calls; "
                             "rewritten as blocked, the claim kept in `claimed`.\n"
                             % ", ".join(changed))
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:2913')
    return changed


def _terminal_status(value):
    return str(value or "").strip().lower() in ("done", "blocked", "error", "cancelled", "stopped")


def _readable(path):
    """True only if the file is there AND has something in it. XY-RESUMEBLIND.

    An empty file is not a plan either, and os.path.exists() would call it one.
    """
    try:
        with io.open(path, encoding="utf-8", errors="replace") as handle:
            return bool(handle.read().strip())
    except Exception:
        return False



# ------------------------------------------------------------------ XY-STEPRESUME
STEP_RESUME_PROMPT = (
    "The previous session ended before this step was finished. Nothing is lost - the "
    "application still holds everything that was already built, and it must stay there.\n\n"
    "1. Read %(spec)s. That ONE file is the whole spec for this step, and this step is your "
    "whole job.\n"
    "2. Find out what is ALREADY done before you make anything: list %(outputs)s, and ask the "
    "application what it holds. Do not rebuild what is there, do not clear it, do not delete "
    "it. Work alongside it.\n"
    "3. Finish what is missing by CALLING YOUR TOOLS. YOUR FIRST ACTION MUST BE A TOOL CALL - "
    "this session ends the moment you take a turn without one, and that is exactly how the "
    "last one ended with the work done in the application and the result unwritten.\n"
    "4. If the step asks for a viewport capture, frame the model and take it.\n"
    "5. LAST, ALWAYS: write %(verdict)s containing exactly\n"
    "{\"node\":\"%(node)s\",\"status\":\"done\",\"summary\":\"<one line of what was produced>\"}\n"
    "Use \"blocked\" instead of \"done\" if you could not do the work. That file is the ONLY "
    "thing that records this step as finished - without it the step counts as never run, "
    "however much is standing in the application."
)


def _step_spec_path(run_dir, run_id):
    """runs/<parent>/steps/<node>.json for a per-step run, '' for anything else.

    XY-STEPSPEC writes these. A run id is `<parent>__<node>` with an optional retry letter,
    the same shape plan_node_for() already parses.
    """
    parent, _, node_id = (run_id or "").partition("__")
    if not node_id:
        return ""
    node = node_id.rstrip("abcdefghijklmnopqrstuvwxyz") or node_id
    project = os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))
    return os.path.join(project, "runs", parent, "steps", node + ".json")


def _step_resume_args(run_dir, run_id):
    """The three paths a per-step resume needs, or None if this is not a per-step run."""
    spec = _step_spec_path(run_dir, run_id)
    if not spec or not _readable(spec):
        return None
    parent, _, node_id = (run_id or "").partition("__")
    node = node_id.rstrip("abcdefghijklmnopqrstuvwxyz") or node_id
    project = os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))
    return {"spec": spec,
            "outputs": os.path.join(project, "outputs"),
            "verdict": os.path.join(project, "runs", parent, "progress-%s.json" % node),
            "node": node}


def _supervise(spec_path):
    """Own the process group and the pid file; keep the run going until the plan ends."""
    spec = load_json(spec_path) or {}
    argv = list(spec.get("argv") or [])
    run_dir = spec.get("runDir") or ""
    run_id = spec.get("runId") or ""
    cwd = spec.get("cwd") or None
    log_path = spec.get("log") or os.path.join(run_dir, "hermes.log")
    tries = int(spec.get("maxAttempts") or SUPERVISE_MAX)
    servers = list(spec.get("servers") or [])      # XY-DONEZERO
    if not argv or not run_dir:
        return
    progress_path = os.path.join(run_dir, "progress.json")
    status_path = os.path.join(run_dir, "hermes-status.json")
    stop_path = os.path.join(run_dir, "STOP")
    # XY-HZWALL - nothing recorded the RUN's own start, so nothing could report its wall clock.
    # hermes-status.json carries startedAt/elapsedSec from the xycy_progress plugin, and the plugin
    # sets _STARTED_AT at import - once per Hermes PROCESS. Every retry spawns a new child, so the
    # clock resets: measured 25 Aug, a run 27 minutes old reported 3.1 minutes the moment it went
    # to attempt 3, and the Player shows that number. hermes-attempts.json only records each
    # attempt's END, so the first attempt's start was written down nowhere at all. Stamp it once,
    # here, before the first child, and keep it across every attempt.
    attempts = []
    run_started_at = time.time()
    stopped_by_person = False

    def _write_attempts():
        try:
            with open(os.path.join(run_dir, "hermes-attempts.json"), "w", encoding="utf-8") as h:
                json.dump({"runId": run_id, "startedAt": round(run_started_at, 3),
                           "wallClockSec": round(time.time() - run_started_at, 1),
                           "attempts": attempts}, h, indent=2)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:3016')
    _write_attempts()

    for i in range(tries):
        try:
            log = open(log_path, "a" if i else "w")
        except Exception:
            log = subprocess.DEVNULL
        # NOT a new session: the child stays in the supervisor's process group, so the
        # existing killpg in cmd_stop takes the whole tree down exactly as before.
        try:
            child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT)
        except Exception as exc:
            attempts.append({"attempt": i + 1, "error": str(exc)})
            break
        code, stalled = _wait_watching_for_stall(child, status_path, stop_path, log_path,
                                                 run_dir)   # XY-HZSTALL + XY-STEPDONE
        try:
            if log not in (subprocess.DEVNULL,):
                log.close()
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:3038')

        st = load_json(status_path) or {}
        tools = int(((st.get("counts") or {}).get("tool_calls")) or 0)
        # XY-DONEZERO runs BEFORE the verdict is read, so a `done` that never touched the
        # application is already a `blocked` by the time anything downstream looks at it.
        _enforce_done_needs_a_call(run_dir, servers, log_path)
        pr = load_json(progress_path) or {}
        # XY-STEPVERDICT - a per-step run writes progress-<node>.json and never the base file.
        steps_seen = _step_verdicts(run_dir)
        step_status = next((d.get("status") for _, d in steps_seen
                            if _terminal_status(d.get("status"))), None)
        app_calls = _app_calls(run_dir)
        attempts.append({"attempt": i + 1, "exit": code, "toolCalls": tools,
                         "appCalls": app_calls,
                         "progressStatus": pr.get("status") or step_status,
                         "stepStatus": step_status,
                         "stalled": bool(stalled),   # XY-HZSTALL
                         "at": round(time.time(), 3)})
        _write_attempts()

        if _terminal_status(pr.get("status")) or step_status:
            release_apps(servers, run_id)    # XY-ONEDRIVER
            return                       # the model said how it went. Nothing to add.
        if os.path.exists(stop_path):
            stopped_by_person = True
            break
        if tools <= 0 and not stalled:   # XY-HZSTALL - a hang has not earned this guard
            break                        # it did nothing at all; running it again would spin
        if i + 1 >= tries:
            break

        # Keep the run readable as still-going across the gap: the plugin sets finishedAt at
        # session_end, and XYCY reads alive as "process there AND not finished". Without this
        # the one second between attempts looks like a death.
        try:
            st["finishedAt"] = None
            st["state"] = "resuming"
            with open(status_path, "w", encoding="utf-8") as handle:
                json.dump(st, handle, indent=2)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:3079')
        # XY-RESUMEBLIND - do not send a model to continue a plan that is not there. A
        # per-step run has no RUN.md and no progress.json (it writes progress-<node>.json),
        # so the retry used to open two missing files and then decide for itself what the
        # run was for. On 26 Aug that decision was 54 floor slabs and ~1000 columns into a
        # live FreeCAD document, over twenty-five minutes, on a run that had ALREADY passed.
        # The check sits ABOVE the log line: a run that is not resuming must not say it is.
        run_md_path = os.path.join(run_dir, "RUN.md")
        progress_for_resume = os.path.join(run_dir, "progress.json")
        # XY-STEPRESUME - a per-step run has neither of those files and is still not blind:
        # its own step spec says what it was for. Measured 29 Aug on Cliff House n4 - exit 0,
        # nine calls into Rhino, three solids built, and no deliverable, no capture and no
        # verdict written, because nothing ever started a second attempt.
        _sr = _step_resume_args(run_dir, run_id)
        if _sr:
            try:
                with open(log_path, "a") as handle:
                    handle.write("\n[xycy] XY-STEPRESUME - the session ended with this step "
                                 "unfinished; continuing from %s (attempt %d of %d)\n"
                                 % (_sr["spec"], i + 2, tries))
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:3100')
            argv = list(argv)
            argv[2] = STEP_RESUME_PROMPT % _sr
            continue
        if not (_readable(run_md_path) or _readable(progress_for_resume)):
            try:
                with open(log_path, "a") as handle:
                    handle.write("\n[xycy] XY-RESUMEBLIND - neither RUN.md nor progress.json "
                                 "is readable in this run directory, so there is no plan to "
                                 "resume from. Not starting another attempt; whatever the "
                                 "model reported stands.\n")
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:3112')
            break
        try:
            with open(log_path, "a") as handle:
                handle.write("\n[xycy] the session ended with the run unfinished — "
                             "continuing from progress.json (attempt %d of %d)\n"
                             % (i + 2, tries))
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:3120')
        argv = list(argv)
        argv[2] = RESUME_PROMPT % {
            "run_md": run_md_path,
            "progress": progress_for_resume,
        }

    # Out of attempts, or it stopped doing anything. Say so where the file says it - a run
    # that has ended must not keep reading "running". No STEP is touched: whatever the model
    # marked is what stands.
    release_apps(servers, run_id)          # XY-ONEDRIVER - the run is over, give them back
    pr = load_json(progress_path) or {}
    # XY-STEPVERDICT, second half. The base file is not the only place a verdict can live, and
    # writing "the model ended its session without finishing" over a step that reported `done`
    # in progress-<node>.json is the same could-not-ask-read-as-no this corpus keeps logging.
    step_terminal = any(_terminal_status(d.get("status")) for _, d in _step_verdicts(run_dir))
    if not _terminal_status(pr.get("status")) and not stopped_by_person and not step_terminal:
        pr["runId"] = pr.get("runId") or run_id
        pr["status"] = "error"
        pr["note"] = ("the model ended its session %d time%s without finishing this run — "
                      "what it did report is below, and nothing has been marked done on its "
                      "behalf" % (len(attempts), "" if len(attempts) == 1 else "s"))
        pr.setdefault("steps", [])
        try:
            with open(progress_path, "w", encoding="utf-8") as handle:
                json.dump(pr, handle, indent=2)
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_hermes_run.py:3147')


def main():
    # XY-HZFINISH: same file, second job. Set by cmd_start when it spawns the supervisor.
    _spec = os.environ.get(SUPERVISE_ENV)
    if _spec:
        _supervise(_spec)
        return
    parser = argparse.ArgumentParser(description="Run a XYCY workflow on the Hermes harness")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    def shared(sub):
        sub.add_argument("--dir", required=True, help="XYCY project folder")
        sub.add_argument("--run-id", required=True, dest="run_id")

    start = subparsers.add_parser("start")
    shared(start)
    start.add_argument("--prompt")
    start.add_argument("--prompt-file", dest="prompt_file")
    start.add_argument("--model", default=os.environ.get("XYCY_HERMES_MODEL"))
    start.add_argument("--provider", default=os.environ.get("XYCY_HERMES_PROVIDER"))
    start.add_argument("--base-url", dest="base_url",
                       default=os.environ.get("XYCY_HERMES_BASE_URL"))
    # XY-FULLCONTEXT - the default is None, not the floor. It WAS the floor, which meant
    # args.context_length was always filled and always won, so resolve_context never reached
    # the model's own window and the fix did nothing at all. MEASURED on the first run after
    # it shipped, 20 Sep 2026: the start record said contextLength 65536, contextFrom "asked
    # for", on a machine whose config had just been raised to 262,144. A default that cannot
    # be told apart from a choice is not a default.
    start.add_argument("--context-length", dest="context_length", type=int,
                       default=(int(os.environ["XYCY_HERMES_CONTEXT"])
                                if os.environ.get("XYCY_HERMES_CONTEXT") else None))
    start.add_argument("--max-tool-calls", dest="max_tool_calls", type=int,
                       default=DEFAULT_MAX_TOOL_CALLS)
    start.add_argument("--servers", help="comma-separated registry server ids (default: all app servers)")
    start.add_argument("--mcp-env", dest="mcp_env", help="JSON {server: {ENV: val}}; '*' applies to all")
    start.add_argument("--toolsets", "-t")
    # XY-SHOTCOPY: absolute path of the PNG this run's viewport captures should land in.
    start.add_argument("--shot")
    start.add_argument("--skills", "-s")
    start.add_argument("--reasoning")
    # XY-CODEBYPAYLOAD
    start.add_argument("--allow-code", dest="allow_code",
                       default=os.environ.get(CODE_ALLOWED_ENV),
                       help="comma-separated server ids whose code tools this run may use "
                            "because the SCRIPT IS IN THE PROMPT (XY-NOCODEGEN otherwise "
                            "removes them)")
    start.add_argument("--worktree", action="store_true")
    start.set_defaults(func=cmd_start)

    status = subparsers.add_parser("status")
    shared(status)
    status.add_argument("--tail", type=int, default=40)
    status.set_defaults(func=cmd_status)

    stop = subparsers.add_parser("stop")
    shared(stop)
    stop.set_defaults(func=cmd_stop)

    models = subparsers.add_parser("models")
    models.add_argument("--base-url", dest="base_url", default=None,
                        help="probe only this OpenAI-compatible endpoint")
    models.set_defaults(func=cmd_models)


    think = subparsers.add_parser("think")
    think.add_argument("--prompt")
    think.add_argument("--prompt-file", dest="prompt_file")
    think.add_argument("--system")
    think.add_argument("--system-file", dest="system_file")
    think.add_argument("--web", action="store_true")
    think.add_argument("--reasoning", default=None)
    think.add_argument("--model", default=None)
    think.add_argument("--provider", default=None)
    think.add_argument("--base-url", dest="base_url", default=None)
    think.add_argument("--context-length", dest="context_length", type=int, default=None)
    think.add_argument("--timeout", type=int, default=180)
    think.set_defaults(func=cmd_think)

    probe = subparsers.add_parser("probe")
    probe.add_argument("--model", default=None)
    probe.add_argument("--provider", default=None)
    probe.add_argument("--base-url", dest="base_url", default=None)
    probe.add_argument("--context-length", dest="context_length", type=int, default=None)
    probe.add_argument("--timeout", type=int, default=180)
    probe.set_defaults(func=cmd_probe)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
