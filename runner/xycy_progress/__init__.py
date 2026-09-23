"""xycy_progress — XYCY run instrumentation for the Hermes Agent harness.

WHY THIS EXISTS
---------------
XYCY's Run Theater renders from two things a run produces on disk:

  runs/<runId>/progress.json   the step ledger the MODEL writes (status per step)
  a live event feed            what the harness is doing right now

On Claude Code the second one came free: `claude -p --output-format stream-json`
emits a line per tool call and `cliEventLine()` in xycy.html parses it. Hermes's
`-z/--oneshot` deliberately prints ONLY the final response text, so there is
nothing to tail. This plugin supplies the missing feed from Hermes's OWN
observer-hook contract (`hermes.observer.v1`) instead of by scraping stdout —
which is strictly better: the payloads carry real correlation IDs
(session/turn/tool_call), timings, statuses and errors.

CONTRACT
--------
Activated only when XYCY_RUN_DIR points at a run directory. Writes:

  <XYCY_RUN_DIR>/hermes-events.ndjson   one JSON object per line, append-only
  <XYCY_RUN_DIR>/hermes-status.json     rewritten snapshot: liveness + counters

It NEVER writes progress.json. That file is the model's honest ledger, and the
XYCY honesty rules forbid anything else marking a step "done" — a plugin that
"helpfully" closed out steps would fake exactly the outcome those rules exist to
prevent. The status snapshot is how XYCY can still show life when the model has
gone quiet, and how it can tell "still working" from "died".

Every callback is fail-open and self-silencing: Hermes catches exceptions and
logs a warning, but a telemetry plugin that throws on every tool call would
bury the log, so failures here disable the plugin for the rest of the process.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time

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


SCHEMA = "xycy.hermes.events.v1"

_LOCK = threading.Lock()
_DEAD = False           # set True after a write failure; stops all further work
_RUN_DIR = None
_EVENTS_PATH = None
_STATUS_PATH = None

# XY-SHOTCOPY -----------------------------------------------------------------
# Where this run's viewport captures should land, and how to recognise one. An app's
# capture tool answers with a path into the harness cache (rhinomcp returns
# "MEDIA:~/.xycy/hermes-runs/<run>/cache/images/img_*.png"); the step was then asked to
# copy that file itself and could not, because its only writing tool writes text.
_SHOT_PATH = None
_SHOT_N = 0
_MEDIA_RE = re.compile(r"(?:MEDIA:\s*)?((?:~|/)[^\s\"'\\,;)\]}]+\.(?:png|jpg|jpeg|webp))", re.I)


def _shot_candidates(value):
    """Every image path mentioned in a tool result, best (MEDIA:-tagged) first."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        return []
    tagged, plain = [], []
    for m in _MEDIA_RE.finditer(text):
        (tagged if m.group(0).upper().startswith("MEDIA") else plain).append(m.group(1))
    return tagged + plain


_LAST_MEDIA = None
_PENDING_SHOT = None
_SHOTFILE_RE = re.compile(r"((?:~|/)[^\s\"'\\,;)\]}]*shots/[^\s\"'\\,;)\]}]+\.(?:png|jpg|jpeg))", re.I)


def _remember_media(result):
    """Hold on to the newest real capture an app returned, whoever asked for it."""
    global _LAST_MEDIA
    for cand in _shot_candidates(result):
        src = os.path.abspath(os.path.expanduser(cand))
        try:
            if os.path.isfile(src) and os.path.getsize(src) > 0:
                _LAST_MEDIA = src
                return
        except Exception:
            continue


def _note_shot_target(args):
    """pre_tool_call is the hook that reliably carries args -- remember the path here."""
    global _PENDING_SHOT
    _PENDING_SHOT = None
    try:
        text = args if isinstance(args, str) else json.dumps(args, default=str)
    except Exception:
        return
    m = _SHOTFILE_RE.search(text or "")
    if m:
        _PENDING_SHOT = os.path.abspath(os.path.expanduser(m.group(1)))


def _fill_written_shot():
    """The model just wrote a shots/*.png with its TEXT writer. Put the real image there."""
    global _PENDING_SHOT
    dst, _PENDING_SHOT = _PENDING_SHOT, None
    if not _LAST_MEDIA or not dst:
        return None
    if dst == _LAST_MEDIA:
        return None
    try:
        # Only step in when what landed is not already a real image -- never clobber a
        # step that genuinely managed to save its own capture.
        if os.path.isfile(dst) and os.path.getsize(dst) >= os.path.getsize(_LAST_MEDIA):
            return None
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".part"
        shutil.copyfile(_LAST_MEDIA, tmp)
        os.replace(tmp, dst)
        return {"from": _LAST_MEDIA, "to": dst, "bytes": os.path.getsize(dst)}
    except Exception:
        return None


def _copy_shot(result):
    """Copy the capture an app just produced into the run's shots/ file. Never raises."""
    global _SHOT_N
    if not _SHOT_PATH:
        return None
    for cand in _shot_candidates(result):
        src = os.path.abspath(os.path.expanduser(cand))
        if src == _SHOT_PATH:
            return None
        try:
            if not os.path.isfile(src) or os.path.getsize(src) <= 0:
                continue
            tmp = _SHOT_PATH + ".part"
            shutil.copyfile(src, tmp)
            os.replace(tmp, _SHOT_PATH)
            _SHOT_N += 1
            return {"from": src, "to": _SHOT_PATH, "bytes": os.path.getsize(_SHOT_PATH)}
        except Exception:
            continue
    return None


_STARTED_AT = time.time()
_COUNTS = {"api_calls": 0, "tool_calls": 0, "tool_errors": 0, "subagents": 0}
_TOOL_T0 = {}           # tool_call_id -> start monotonic
_LAST = {"event": None, "tool": None, "at": None}
# XY-HEARTBEAT ---------------------------------------------------------------
# What the run is waiting for right now, and since when. Without this a status
# snapshot can only say "nothing has happened lately", which is equally true of a
# model thinking hard and a process that died.
_WAITING = {"on": None, "since": None}
_FINISHED_AT = None
# XY-SUBEND: the run's own session id. A delegated sub-agent opens and closes a
# session of its own mid-run, and only the root session ending means the run is over.
_ROOT_SESSION = None
_BEAT = None
_BEAT_SECONDS = float(os.environ.get("XYCY_HEARTBEAT_SECONDS", "3") or 3)

# Tool args/results can be enormous (a whole RhinoScript body, a base64 PNG from
# capture_viewport). The console only ever shows a one-line preview, so truncate
# at the source rather than writing megabytes per step to disk.
_PREVIEW_CHARS = int(os.environ.get("XYCY_EVENT_PREVIEW_CHARS", "400") or 400)


def _preview(value):
    """A short, always-JSON-safe rendering of an arbitrary payload."""
    if value is None:
        return None
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        try:
            text = str(value)
        except Exception:
            return "<unrenderable>"
    text = " ".join(text.split())
    if len(text) > _PREVIEW_CHARS:
        return text[:_PREVIEW_CHARS] + "…"
    return text


def _init():
    """Resolve the run directory. Returns False when XYCY isn't driving this run."""
    global _RUN_DIR, _EVENTS_PATH, _STATUS_PATH
    if _RUN_DIR is not None:
        return True
    run_dir = os.environ.get("XYCY_RUN_DIR", "").strip()
    if not run_dir:
        return False
    run_dir = os.path.abspath(os.path.expanduser(run_dir))
    try:
        os.makedirs(run_dir, exist_ok=True)
    except Exception:
        return False
    _RUN_DIR = run_dir
    _EVENTS_PATH = os.path.join(run_dir, "hermes-events.ndjson")
    global _SHOT_PATH
    shot = os.environ.get("XYCY_SHOT_PATH", "").strip()
    if shot:
        _SHOT_PATH = os.path.abspath(os.path.expanduser(shot))
        try:
            os.makedirs(os.path.dirname(_SHOT_PATH), exist_ok=True)
        except Exception:
            _SHOT_PATH = None
    _STATUS_PATH = os.path.join(run_dir, "hermes-status.json")
    _start_beat()
    return True



def _beat():
    """Refresh the snapshot on a timer, so waiting looks different from stopping.

    XY-BEATLIVES: this used to `return` on the first exception, which ended the
    heartbeat for the rest of the run. On Windows that is not a hypothetical: the
    snapshot is published with os.replace(), and os.replace onto a file another
    process currently has open raises PermissionError. XYCY's run poller reads
    hermes-status.json every couple of seconds, so a collision is a matter of time.
    MEASURED on Sean's PC, 21 Sep 2026, run_1790010071400__n115: the file stopped at
    11:22:38 with a hermes-status.json.tmp left beside it, and the page showed the
    step as NOT RESPONDING for the rest of it while the GPU sat at 92% and the model
    went on building the bracket. The same .tmp fingerprint is in __n114.
    A failed beat is a missed beat, never the last beat.
    """
    while not _DEAD and _FINISHED_AT is None:
        time.sleep(_BEAT_SECONDS)
        try:
            with _LOCK:
                if _FINISHED_AT is None:
                    _write_status()
        except Exception:
            continue


def _start_beat():
    global _BEAT
    if _BEAT is not None or _BEAT_SECONDS <= 0:
        return
    try:
        _BEAT = threading.Thread(target=_beat, name="xycy-heartbeat", daemon=True)
        _BEAT.start()
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_progress/__init__.py:280')

def _emit(kind, **fields):
    """Append one event and refresh the status snapshot. Never raises."""
    global _DEAD
    if _DEAD or not _init():
        return
    event = {"schema": SCHEMA, "ts": round(time.time(), 3), "kind": kind}
    for key, value in fields.items():
        if value is not None:
            event[key] = value
    try:
        with _LOCK:
            with open(_EVENTS_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, default=str) + "\n")
            _LAST["event"] = kind
            _LAST["at"] = event["ts"]
            if kind in ("tool_start", "tool_end"):
                _LAST["tool"] = fields.get("tool")
            # XY-BEATLIVES: the snapshot is a CONVENIENCE and the event log is the
            # record. Publishing the snapshot can fail on its own for a reason that
            # says nothing about the log - on Windows, os.replace refuses while
            # XYCY's poller holds the destination open. That used to set _DEAD and
            # silence the event log too, so one lost snapshot cost the rest of the
            # run: no events, no heartbeat, and a page that read NOT RESPONDING
            # while the model went on working. Measured on run_1790010071400__n115,
            # 21 Sep 2026 - both files stop at 11:22:38, with the .tmp left behind.
            try:
                _write_status()
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_progress/__init__.py:snapshot')
    except Exception:
        # One bad write (full disk, deleted run dir) means every subsequent one
        # fails too. Go quiet instead of logging a warning per tool call.
        _DEAD = True


def _write_status():
    """Rewrite the snapshot atomically — XYCY may poll it mid-write."""
    snapshot = {
        "schema": SCHEMA,
        "runId": os.environ.get("XYCY_RUN_ID") or None,
        "harness": "hermes",
        "model": os.environ.get("HERMES_INFERENCE_MODEL") or None,
        "pid": os.getpid(),
        "startedAt": round(_STARTED_AT, 3),
        "updatedAt": round(time.time(), 3),
        "elapsedSec": round(time.time() - _STARTED_AT, 1),
        "counts": dict(_COUNTS),
        "last": dict(_LAST),
        # XY-HEARTBEAT: what is outstanding, and for how long. `finishedAt` is what
        # lets a reader tell "the work is over" from "the process is still winding down".
        "waitingOn": _WAITING["on"],
        "waitingSec": (round(time.time() - _WAITING["since"], 1)
                       if _WAITING["since"] else None),
        "finishedAt": _FINISHED_AT,
        "state": "finished" if _FINISHED_AT else ("waiting" if _WAITING["on"] else "working"),
    }
    tmp = _STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, default=str)
    # XY-BEATLIVES: os.replace is atomic on POSIX and refuses on Windows while a
    # reader holds the destination open. The reader is XYCY's own poller and it lets
    # go in milliseconds, so retry briefly before giving up on THIS beat.
    last = None
    for attempt in range(6):
        try:
            os.replace(tmp, _STATUS_PATH)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05 * (attempt + 1))
    raise last


# --- XY-HOUSEGRAPH6 - the Hermes lane learns too ------------------------------------------
# The XYCY engine writes ~/.xycy/house-graph.json as tools refuse it (xycy-engine/house.mjs):
# "Prerequisites failed: ...", "'X' object has no attribute 'Y'", and the rest. Until this was
# added only that lane learned; a step run through Hermes met the same refusals and wrote
# nothing down, so the next Hermes step rediscovered them. This is a port of REFUSAL_PATTERNS
# and recordFact, writing the SAME file in the SAME schema, so the page reads one graph
# however the step was run. Kept in step by hand: change one, change the other, and
# local-agent/test_house_graph.mjs runs the same sentences through both.
#
# Hermes names an MCP tool mcp__<server>__<tool>; the engine names it <server>__<tool>. The
# leading mcp__ is dropped so the same refusal from the same tool is one fact, not two.
_HOUSE_PATH = os.path.join(os.path.expanduser("~"), ".xycy", "house-graph.json")
_HOUSE_SCHEMA = "xycy.house.v1"
_HOUSE_PATTERNS = (
    ("requires", re.compile(r'Prerequisites failed:\s*([^\n"\\]+?)\.?\s*(?:\\n|\n|"|$)', re.I), None),
    ("requires", re.compile(r'validation error for \w+\s*\n\s*(\w+)\s*\n\s*Field required', re.I),
     lambda m: "the argument `" + m.group(1) + "` (it is required)"),
    ("conflictsWith", re.compile(r'method "?([A-Za-z0-9_]+)"? is not supported', re.I),
     lambda m: "this application build - it does not implement " + m.group(1)),
    ("causes", re.compile(r"('[^']+' object has no attribute '[^']+')", re.I), None),
    ("causes", re.compile(r"(cannot import name '[^']+' from '[^']+')", re.I), None),
    ("causes", re.compile(r'\b((?:KeyError|AttributeError|TypeError|ValueError|IndexError): [^\n"\\]{1,120})'), None),
)
_HOUSE_INNER = {}       # tool_call_id -> the real tool behind a generic tool_call wrapper


def _house_norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def house_refusal_fact(tool, text, ctx=None):
    """One tool answer -> a fact, or None when the answer is not a refusal."""
    ctx = ctx or {}
    t = str(text or "")
    tool = str(tool or "")
    if tool.startswith("mcp__"):
        tool = tool[5:]
    for rel, rx, shape in _HOUSE_PATTERNS:
        m = rx.search(t)
        if not m:
            continue
        sentence = (shape(m) if shape else m.group(1)).strip().rstrip(".")
        if not sentence or len(sentence) < 6:
            continue
        parts = tool.split("__")
        server = parts[0] if len(parts) > 1 else str(ctx.get("server") or "")
        when = " ".join([x for x in (ctx.get("app") or server, ctx.get("appVersion"), ctx.get("os") or sys.platform) if x])
        return {
            "from": tool, "rel": rel, "to": sentence,
            "when": ("on " + when) if when else "",
            "server": server,
            "measured": {"on": time.strftime("%Y-%m-%d"), "machine": ctx.get("machine") or _hostname(),
                         "run": ctx.get("run") or "", "model": ctx.get("model") or ""},
        }
    return None


def _hostname():
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return ""


def house_record_fact(fact, path=None):
    """Write one fact into the house graph. Same sentence from the same tool = one fact seen again."""
    path = path or _HOUSE_PATH
    if not fact or not fact.get("from") or not fact.get("to"):
        return None
    g = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            g = json.load(fh)
        if not isinstance(g, dict) or g.get("schema") != _HOUSE_SCHEMA:
            g = None
    except Exception:
        g = None
    if g is None:
        g = {"schema": _HOUSE_SCHEMA, "name": "What XYCY has learned on this computer", "nodes": [], "edges": []}
    g["nodes"] = g.get("nodes") if isinstance(g.get("nodes"), list) else []
    g["edges"] = g.get("edges") if isinstance(g.get("edges"), list) else []
    key = _house_norm(fact["from"]) + "|" + _house_norm(fact.get("rel")) + "|" + _house_norm(fact["to"])
    edge = None
    for e in g["edges"]:
        if _house_norm(e.get("from")) + "|" + _house_norm(e.get("rel")) + "|" + _house_norm(e.get("to")) == key:
            edge = e
            break
    for nid, ntype in ((fact["from"], "tool"), (fact["to"], "condition")):
        if not any(n.get("id") == nid for n in g["nodes"]):
            g["nodes"].append({"id": nid, "label": nid, "type": ntype})
    if edge is not None:
        edge["seen"] = (edge.get("seen") or 1) + 1
        edge["conf"] = min(1, 0.6 + 0.1 * edge["seen"])
        edge["last"] = fact.get("measured")
    else:
        edge = dict(fact)
        edge.update({"seen": 1, "conf": 0.7, "cite": "measured on this computer"})
        g["edges"].append(edge)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(g, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        return None
    return edge


def _house_learn(tool, result, error):
    """Called on every tool_end. Cheap when the answer is not a refusal, which is nearly always."""
    try:
        text = ""
        for v in (result, error):
            if v is None:
                continue
            text += (v if isinstance(v, str) else json.dumps(v, default=str)) + "\n"
        if not text or ("mcp" not in str(tool or "") and "__" not in str(tool or "")):
            return None   # only what an application's own tool said; a file reader's error names a path, not a fact
        ctx = {"os": sys.platform, "run": os.path.basename(_RUN_DIR or ""), "model": os.environ.get("XYCY_MODEL", "")}
        fact = house_refusal_fact(tool, text[:8000], ctx)
        if not fact:
            return None
        edge = house_record_fact(fact)
        if edge:
            _emit("learned", tool=fact["from"], rel=fact["rel"], fact=fact["to"], seen=edge.get("seen"))
        return edge
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_progress/__init__.py:houselearn')
        return None


# ---------------------------------------------------------------- hook bodies
# Every callback takes **kwargs: the observer contract is explicitly additive,
# so naming parameters positionally would break on the next Hermes release.

def on_session_start(**kwargs):
    global _ROOT_SESSION
    if _ROOT_SESSION is None and kwargs.get("session_id"):
        _ROOT_SESSION = kwargs.get("session_id")     # XY-SUBEND: first one wins
    _emit("session_start",
          session_id=kwargs.get("session_id"),
          task_id=kwargs.get("task_id"),
          model=kwargs.get("model"),
          provider=kwargs.get("provider"))


def on_session_end(**kwargs):
    # XY-SUBEND: this fired for a delegated sub-agent too, and setting _FINISHED_AT
    # both froze the snapshot at "finished" and killed the heartbeat thread. XYCY read
    # that as the session ending, took its verdict from a progress.json that still said
    # "running" with nothing done, and told the person the run FAILED - while the parent
    # session went on working for another 74 seconds. Measured on run_1786864518322,
    # Diyara Demo, nemotron-3.5-lightning, 2026-08-16.
    global _FINISHED_AT
    sid = kwargs.get("session_id")
    is_root = (sid is None) or (_ROOT_SESSION is None) or (sid == _ROOT_SESSION)
    if is_root:
        _FINISHED_AT = round(time.time(), 3)
        _WAITING["on"], _WAITING["since"] = None, None
    _emit("session_end",
          session_id=sid,
          root=is_root,
          reason=kwargs.get("reason") or kwargs.get("status"))


def on_session_finalize(**kwargs):
    _emit("session_finalize", session_id=kwargs.get("session_id"))


def pre_api_request(**kwargs):
    _WAITING["on"], _WAITING["since"] = "model", time.time()
    _COUNTS["api_calls"] += 1
    _emit("api_start",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          api_request_id=kwargs.get("api_request_id"),
          model=kwargs.get("model"),
          provider=kwargs.get("provider"),
          api_call_count=kwargs.get("api_call_count"))


def post_api_request(**kwargs):
    _WAITING["on"], _WAITING["since"] = None, None
    _emit("api_end",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          api_request_id=kwargs.get("api_request_id"),
          status=kwargs.get("status"),
          duration_ms=kwargs.get("duration_ms"),
          input_tokens=kwargs.get("input_tokens"),
          output_tokens=kwargs.get("output_tokens"))


def api_request_error(**kwargs):
    _WAITING["on"], _WAITING["since"] = None, None
    _emit("api_error",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          error=_preview(kwargs.get("error") or kwargs.get("message")))


def pre_tool_call(**kwargs):
    # XY-SHOTCOPY: this is the hook that carries the arguments. If the model is about to
    # write a shots/*.png with its text writer, remember where, so the real capture can be
    # dropped in behind it the moment the call returns.
    try:
        _note_shot_target(kwargs.get("args"))
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_progress/__init__.py:565')
    _WAITING["on"], _WAITING["since"] = (kwargs.get("tool_name") or "a tool"), time.time()
    _COUNTS["tool_calls"] += 1
    call_id = kwargs.get("tool_call_id")
    if call_id:
        _TOOL_T0[call_id] = time.monotonic()
        # XY-HOUSEGRAPH6 - Hermes' generic tool_call wrapper hides the real tool in its args
        try:
            if kwargs.get("tool_name") == "tool_call" and isinstance(kwargs.get("args"), dict):
                _HOUSE_INNER[call_id] = str(kwargs["args"].get("name") or kwargs["args"].get("tool") or "")
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_progress/__init__.py:houseinner')
    _emit("tool_start",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          tool_call_id=call_id,
          tool=kwargs.get("tool_name"),
          args=_preview(kwargs.get("args")))
    # Returning None leaves the call alone. This hook CAN block a tool
    # ({"action": "block"}) — deliberately unused: XYCY runs with --yolo and
    # policy belongs in the launcher's server allowlist, not in telemetry.
    return None


def post_tool_call(**kwargs):
    # XY-SHOTCOPY: do this BEFORE the event is written, so the copied byte count is in the
    # same feed line that reports the capture -- that is what makes the fix provable.
    copied = None
    try:
        _remember_media(kwargs.get("result"))
        copied = _copy_shot(kwargs.get("result"))
        if not copied:
            copied = _fill_written_shot()
    except Exception:
        copied = None
    if copied:
        _emit("shot_saved", tool=kwargs.get("tool_name"),
              path=copied["to"], source=copied["from"], bytes=copied["bytes"])
    _WAITING["on"], _WAITING["since"] = None, None
    call_id = kwargs.get("tool_call_id")
    started = _TOOL_T0.pop(call_id, None) if call_id else None
    status = kwargs.get("status")
    envelope = status
    green = not (status and str(status).lower() not in ("ok", "success", "succeeded", "completed"))
    # XY-OKFAILED - read the RESULT, not just the envelope. The full result, before _preview
    # truncates it, because the sentence that matters is not always in the first 200 characters.
    said = _op_failed(kwargs.get("result")) if green else None
    if said:
        status = "failed"
    if not green or said:
        _COUNTS["tool_errors"] += 1
    # XY-HOUSEGRAPH6 - what the tool refused, written down for every later step
    try:
        _house_learn(_HOUSE_INNER.pop(call_id, None) or kwargs.get("tool_name"), kwargs.get("result"), kwargs.get("error"))
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_progress/__init__.py:houselearn2')
    _emit("tool_end",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          tool_call_id=call_id,
          tool=kwargs.get("tool_name"),
          status=status,
          # Never destroy the envelope's own answer, and always say what changed the verdict:
          # a rule that silently rewrites a status is as hard to trust as the bug it replaces.
          envelope=envelope if said else None,
          op_failed=said,
          duration_ms=round((time.monotonic() - started) * 1000, 1) if started else None,
          error=_preview(kwargs.get("error")),
          result=_preview(kwargs.get("result")))


# XY-OKFAILED - the phrases a FAILED operation actually used on this machine, inside a call the
# MCP envelope reported as ok. Every one of these was copied from a real event feed; none is a
# guess. Keep it that way: a phrase added on a hunch turns this check into the thing it exists to
# stop, a verdict that was never a measurement.
_OP_FAILED_NEEDLES = (
    "failed to create",            # rhino create_objects, 25 and 26 Aug
    "error executing code:",       # blender execute_blender_code, 26 Aug - the operator was
                                   # rejected outright and the envelope still said ok
    "communication error with",    # rhino and blender bridges, 26 Aug
    "could not be found",          # "Calling operator ... error, could not be found"
    "undefined method",            # sketchup run_ruby, 26 Aug
    "command failed:",             # rhino run_command, 26 Aug
    "error running ",              # rhino run_command timeout, 26 Aug
    "traceback (most recent call last)",
    '"success": false',
    "'success': false",
)


def _op_failed(result):
    """The phrase that says this operation failed, or None.

    None means "I could not ask" - an unreadable result, a shape json cannot render - and NOT
    "it succeeded". The caller leaves the envelope's own answer alone in that case, which is the
    safe direction: this check may only ever take a green away, never hand one out.
    """
    if result is None:
        return None
    try:
        text = result if isinstance(result, str) else json.dumps(result, default=str)
    except Exception:
        return None
    low = text[:8000].lower()
    # A result that arrived as JSON-inside-a-string carries escaped quotes, so a needle written
    # the way a human reads it ('"success": false') never matches the bytes ('\"success\": false').
    # Measured 26 Aug on sketchup run_ruby: the operation failed, the needle was already in this
    # list, and it still reported ok. Match against the de-escaped copy as well.
    flat = low.replace(chr(92) + chr(34), chr(34)).replace(chr(92) + "n", " ")
    for needle in _OP_FAILED_NEEDLES:
        if needle in low or needle in flat:
            return needle
    return None


def subagent_start(**kwargs):
    _COUNTS["subagents"] += 1
    _emit("subagent_start",
          parent_session_id=kwargs.get("parent_session_id"),
          child_session_id=kwargs.get("child_session_id"),
          task=_preview(kwargs.get("task") or kwargs.get("prompt")))


def subagent_stop(**kwargs):
    _emit("subagent_stop",
          parent_session_id=kwargs.get("parent_session_id"),
          child_session_id=kwargs.get("child_session_id"),
          status=kwargs.get("status"))


def register(ctx):
    """Hermes plugin entry point."""
    if not _init():
        return          # not an XYCY run — stay completely inert
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("pre_api_request", pre_api_request)
    ctx.register_hook("post_api_request", post_api_request)
    ctx.register_hook("api_request_error", api_request_error)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("subagent_start", subagent_start)
    ctx.register_hook("subagent_stop", subagent_stop)
    _emit("plugin_loaded", plugin="xycy_progress", version="0.1.0")
    # XY-RHINOLEAK: something the PERSON needs to read, not the model. The launcher measures
    # it before the run starts and hands it over here, because the event feed is the only
    # channel that reaches the run console.
    _notice = os.environ.get("XYCY_NOTICE", "").strip()
    if _notice:
        _emit("notice", text=_notice)
