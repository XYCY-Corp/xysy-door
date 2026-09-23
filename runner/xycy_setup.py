#!/usr/bin/env python3
"""xycy_setup — the no-terminal install actions behind the Hermes setup screen.

Every empty state on that screen is a button, and each button has to do real work that takes
minutes. So all three run as BACKGROUND JOBS with a progress file XYCY polls, exactly the shape
the run path already uses — rather than a synchronous call that makes the UI look hung.

    xycy_setup.py install-hermes                 -> {jobId}
    xycy_setup.py install-ollama                 -> {jobId}
    xycy_setup.py pull-model --model llama3.1:8b -> {jobId}
    xycy_setup.py job --id <jobId>               -> the progress snapshot
    xycy_setup.py job --list

Three rules learned the hard way elsewhere in this codebase, applied here:

1. NEVER SCRAPE A TUI. The `claude setup-token` saga burned days on a token mangled by an
   animated terminal redraw. `ollama pull` has the same problem — carriage returns and a spinner.
   So the model download talks to Ollama's HTTP API (`POST /api/pull`, streaming JSON with exact
   `completed`/`total` byte counts) and gets a real percentage instead of a parsed one.
2. SINGLE-FLIGHT. Two concurrent installs of the same thing fight; the login work proved that.
   One job per kind at a time, enforced by a lock file that records the owning pid.
3. VERIFY, DON'T TRUST THE EXIT CODE. An installer can exit 0 and leave nothing usable. Each job
   ends by proving the thing works — `hermes --version`, an HTTP hit on Ollama, the model present
   in `/api/tags` — and reports failure if that proof doesn't land.

Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

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
JOBS_DIR = os.path.join(XYCY_ROOT, "jobs")
OLLAMA_URL = os.environ.get("XYCY_OLLAMA_URL", "http://127.0.0.1:11434")
HERMES_INSTALLER = ("https://raw.githubusercontent.com/NousResearch/"
                    "hermes-agent/main/scripts/install.sh")
OLLAMA_DMG_ZIP = "https://ollama.com/download/Ollama-darwin.zip"


# --------------------------------------------------------------------------- job plumbing
def job_path(job_id):
    return os.path.join(JOBS_DIR, job_id + ".json")


def lock_path(kind):
    return os.path.join(JOBS_DIR, kind + ".lock")


def write_job(job_id, **fields):
    """Atomic, because XYCY polls this file while we are writing it."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    path = job_path(job_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception:
        state = {"id": job_id, "startedAt": time.time()}
    state.update(fields)
    state["updatedAt"] = time.time()
    state["elapsedSec"] = round(state["updatedAt"] - state.get("startedAt", state["updatedAt"]), 1)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    os.replace(tmp, path)
    return state


def pid_alive(pid):
    # XY-KILLNOTASK - os.kill(pid, 0) is the POSIX "are you there" idiom. On Windows CPython
    # implements os.kill as TerminateProcess for every signal but the two console ones, and
    # whether zero is excluded depends on the Python version. See the note beside pid_is_alive
    # in xycy_hermes_run.py, including the measurement showing this Python does NOT kill.
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


def claim(kind):
    """Single-flight. Returns the existing jobId when one is genuinely still running."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    path = lock_path(kind)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            held = json.load(handle)
        if held.get("pid") and pid_alive(held["pid"]):
            return held.get("jobId")
        # A stale lock from a crashed or killed job must not block forever.
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_setup.py:148')
    return None


def take_lock(kind, job_id):
    with open(lock_path(kind), "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "jobId": job_id, "at": time.time()}, handle)


def drop_lock(kind):
    try:
        os.unlink(lock_path(kind))
    except Exception as _xy_e:
        say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_setup.py:161')


def spawn_worker(kind, argv_extra):
    """Start ourselves detached as the worker for this job, return the jobId immediately."""
    existing = claim(kind)
    if existing:
        print(json.dumps({"ok": True, "jobId": existing, "alreadyRunning": True,
                          "note": "an install of this kind is already in progress"}))
        return
    job_id = "%s_%d" % (kind, int(time.time() * 1000))
    os.makedirs(JOBS_DIR, exist_ok=True)
    write_job(job_id, kind=kind, state="starting", percent=0, phase="starting", ok=None)
    argv = [sys.executable, os.path.abspath(__file__), "_worker", "--kind", kind,
            "--job-id", job_id] + argv_extra
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    child = subprocess.Popen(argv, **kwargs)
    write_job(job_id, pid=child.pid)
    print(json.dumps({"ok": True, "jobId": job_id, "kind": kind, "pid": child.pid}))


def enriched_env():
    env = dict(os.environ)
    path = env.get("PATH", "")
    for entry in [os.path.join(HOME, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin"]:
        if entry not in path.split(os.pathsep):
            path += os.pathsep + entry
    env["PATH"] = path
    return env


def http_json(url, payload=None, timeout=10):
    try:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def hermes_home():
    """Where Hermes keeps its own home folder. The same three lines as the runner and the door.

    Hermes' own installer sets HERMES_HOME, so that wins whenever it is set. When it is not,
    the default is not the same on every operating system: measured 8 September 2026, a Hermes
    Desktop install on Windows put its home in %LOCALAPPDATA%\\hermes and left ~/.hermes
    holding nothing at all.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return env
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
        return os.path.join(local, "hermes")
    return os.path.join(HOME, ".hermes")


def which_hermes():
    """Where the hermes program is, or "". The same places the runner's find_hermes looks.

    XY-DOCLIBS3. The comment below has said since August that two functions answering one
    question must use one list, and they did not: the runner's find_hermes() looks in
    <hermes home>/bin and this did not. MEASURED on Sean's PC, 20 Sep 2026, in a live Hermes
    run started through the door - the run's own start record carried
    docLibs {"ok": false, "why": "Hermes was not found, so there is no interpreter to install
    into"}, on the machine whose hermes.exe the agent had already reported at
    <LOCALAPPDATA>\\hermes\\bin\\hermes.exe a minute earlier. The consequence is precisely the
    defect XY-DOCLIBS was written to end: a document step on a Windows machine gets no
    reportlab, no openpyxl, no python-docx and no pypdf, and finds out by failing an import
    mid-run. Three lists is one too many; until they are one function, test_hermes_home.py
    plants a binary and requires this and the runner to return the SAME answer.
    """
    home_bin = os.path.join(hermes_home(), "bin")
    cands = [os.environ.get("HERMES_CLI"), shutil.which("hermes"),
             os.path.join(home_bin, "hermes"),
             os.path.join(hermes_home(), "hermes-agent", "venv", "bin", "hermes"),
             os.path.join(HOME, ".local", "bin", "hermes"),
             os.path.join(HOME, ".hermes", "hermes-agent", "venv", "bin", "hermes")]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
        appdata = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
        # Bare "hermes" belongs here too, and its absence was a real defect: the runner's
        # find_hermes() included it, this did not, so on the same machine the readiness probe
        # could say Hermes is installed while the setup card offered to install it. Two functions
        # answering one question must use one list.
        names = ["hermes.exe", "hermes.cmd", "hermes.bat", "hermes"]
        cands = [os.environ.get("HERMES_CLI")] + [shutil.which(n) for n in names] + [
            os.path.join(b, n) for b in (
                home_bin,
                os.path.join(hermes_home(), "hermes-agent", "venv", "Scripts"),
                os.path.join(HOME, ".hermes", "hermes-agent", "venv", "Scripts"),
                os.path.join(local, "Programs", "Python", "Scripts"),
                os.path.join(appdata, "Python", "Scripts"),
                os.path.join(HOME, ".local", "bin"),
                os.path.join(local, "Microsoft", "WindowsApps")) for n in names]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


# XY-DOCLIBS (19 Sep 2026) - the libraries a document step cannot make a document without.
#
# A step that drives no application has to AUTHOR its artifact, and the xlsx, docx and pdf
# skills all do that in Python. XY-DOCSTEPPY gave those steps code_execution back; this is the
# other precondition, and without it the tool is there and the import is not.
#
# MEASURED on Sean's PC, 19 Sep 2026, diag_docstep1: with code_execution restored the model
# ran `from reportlab...` and got "ModuleNotFoundError: No module named 'reportlab'" - and none
# of reportlab, openpyxl, python-docx or pypdf was present in the interpreter Hermes runs its
# code in. It then fell back to typing a PDF by hand, which is the 675-byte permit-report.pdf
# XY-DOCSTEPPY's note is about. After installing the four, the same request produced a real
# 1,610-byte reportlab PDF carrying the right numbers (425 rows, 39.619, 42.493), checked
# against the CSV independently.
#
# The import name is not the package name for two of these, so both are written down.
DOC_LIBS = (("reportlab", "reportlab"), ("openpyxl", "openpyxl"),
            ("docx", "python-docx"), ("pypdf", "pypdf"))


def hermes_python():
    """The interpreter Hermes runs code_execution in, or "".

    MEASURED, not assumed: a probe run on 19 Sep printed sys.executable from inside
    execute_code and got the venv's own python.exe beside the hermes binary. So the answer is
    derived from where hermes itself lives rather than from PATH, which on this machine
    resolves `python` to a third interpreter entirely (XY-NOTVENV).
    """
    binary = which_hermes()
    places = []
    if binary:
        # The Mac layout: hermes and its interpreter sit in the same venv bin folder.
        places.append(os.path.dirname(os.path.abspath(binary)))
    # XY-DOCLIBS3. The Windows layout is different and the first version of this did not know
    # it. MEASURED on Sean's PC, 20 Sep 2026: <hermes home>\\bin holds hermes.exe, uv.exe and
    # the browser tools and NO interpreter at all, while the interpreter Hermes runs its code
    # in is <hermes home>\\hermes-agent\\venv\\Scripts\\python.exe - proved by asking that
    # python for hermes_cli, which it has. So looking only beside the binary answered "" on a
    # machine that has everything, and the run recorded "there is no interpreter to install
    # into" while all four document libraries were in fact already sitting in that venv.
    places.append(os.path.join(hermes_home(), "hermes-agent", "venv", "Scripts"))
    places.append(os.path.join(hermes_home(), "hermes-agent", "venv", "bin"))
    for folder in places:
        for name in ("python.exe", "python3", "python"):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
    return ""


def doc_libs_missing(python_path=None):
    """Which of DOC_LIBS the Hermes interpreter cannot import. [] means all four are there.

    Returns None when the question could not be asked at all - no Hermes, or the probe did not
    run - because "could not look" is not the same answer as "nothing is missing".
    """
    python_path = python_path or hermes_python()
    if not python_path:
        return None
    code = ("import importlib.util as u\n"
            "print(','.join(m for m in %r if u.find_spec(m) is None))"
            % ([m for m, _ in DOC_LIBS],))
    try:
        out = subprocess.run([python_path, "-c", code], capture_output=True, text=True,
                             timeout=60, env=enriched_env())
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return [m for m in (out.stdout or "").strip().split(",") if m]


def ensure_doc_libs(timeout=300):
    """Install any missing DOC_LIBS into the Hermes interpreter. Answers what it did.

    uv first, because the Hermes virtual environment on Windows is uv-managed and has no pip
    inside it at all - `python -m pip` there answers "No module named pip" (measured). The
    plain pip route stays as the fallback for an environment that does have it.
    """
    python_path = hermes_python()
    if not python_path:
        return {"ok": False, "why": "Hermes was not found, so there is no interpreter to install into"}
    missing = doc_libs_missing(python_path)
    if missing is None:
        return {"ok": False, "why": "the interpreter would not answer which libraries it has"}
    if not missing:
        return {"ok": True, "installed": [], "already": [m for m, _ in DOC_LIBS]}
    wanted = [pkg for mod, pkg in DOC_LIBS if mod in missing]
    uv = shutil.which("uv") or os.path.join(HOME, "AppData", "Local", "hermes", "bin", "uv.exe")
    tries = []
    if uv and os.path.isfile(uv):
        tries.append([uv, "pip", "install", "--python", python_path] + wanted)
    tries.append([python_path, "-m", "pip", "install", "--disable-pip-version-check",
                  "--no-input"] + wanted)
    last = ""
    for argv in tries:
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                 env=enriched_env())
        except Exception as exc:
            last = str(exc)
            continue
        last = ((out.stdout or "") + (out.stderr or ""))[-400:]
        if out.returncode == 0:
            break
    still = doc_libs_missing(python_path)
    if still:
        return {"ok": False, "wanted": wanted, "stillMissing": still, "detail": last}
    if still is None:
        return {"ok": False, "wanted": wanted,
                "why": "the install ran but the interpreter would not say what it has now",
                "detail": last}
    return {"ok": True, "installed": wanted}


def work_doc_libs(job_id, args):
    write_job(job_id, state="running", percent=10, phase="checking the document libraries")
    answer = ensure_doc_libs()
    if answer.get("ok"):
        done = answer.get("installed") or []
        write_job(job_id, state="done", ok=True, percent=100, phase="ready",
                  message=("the document libraries were already there" if not done
                           else "installed " + ", ".join(done)))
    else:
        write_job(job_id, state="failed", ok=False, phase="could not install",
                  error=answer.get("why") or ("still missing: "
                                              + ", ".join(answer.get("stillMissing") or [])),
                  detail=answer.get("detail") or "")


def which_ollama():
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


def human(n):
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


# --------------------------------------------------------------------------- workers
def work_install_hermes(job_id, args):
    if which_hermes():
        write_job(job_id, state="done", ok=True, percent=100, phase="already installed",
                  message="Hermes is already installed", path=which_hermes())
        return
    if os.name == "nt":
        # The shell installer is bash-only. Hermes does publish a pip route (its own installer
        # documents a --postinstall step "for pip installs"), so try that rather than refusing —
        # but treat it as unproven: the job only reports success if `hermes --version` answers
        # afterwards. Upstream calls native Windows experimental, so an honest failure here is a
        # real possible outcome, not a bug to paper over.
        write_job(job_id, state="running", percent=10, phase="installing via pip")
        log = os.path.join(JOBS_DIR, "hermes-install.log")
        with open(log, "w") as out:
            for step, pct, argv in ((("pip install"), 45, [sys.executable, "-m", "pip", "install",
                                                           "--upgrade", "hermes-agent"]),
                                    (("post-install"), 80, ["hermes", "--postinstall"])):
                write_job(job_id, state="running", percent=pct, phase=step, log=log)
                try:
                    subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, timeout=1800, env=enriched_env())
                except Exception as exc:
                    out.write("\n%s failed: %s\n" % (step, exc))
        binary = which_hermes()
        if not binary:
            tail = ""
            try:
                tail = open(log, "r", errors="replace").read()[-600:]
            except Exception as _xy_e:
                say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_setup.py:450')
            write_job(job_id, state="failed", ok=False, phase="install failed",
                      error="Hermes did not appear after the pip install. Native Windows support "
                            "is experimental upstream — installing under WSL2 is the supported "
                            "route, and XYCY will find it there.", detail=tail)
            return
        try:
            version = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                     timeout=90, env=enriched_env()).stdout.strip().splitlines()[0]
        except Exception:
            version = None
        write_job(job_id, state="running", percent=92, phase="installing the document libraries")
        _docs = ensure_doc_libs()   # XY-DOCLIBS
        write_job(job_id, state="done", ok=True, percent=100, phase="installed",
                  path=binary, version=version, docLibs=_docs,
                  message="Hermes installed" + (" — %s" % version if version else ""))
        return

    script = os.path.join(JOBS_DIR, "hermes-install.sh")
    write_job(job_id, state="running", percent=2, phase="downloading the installer")
    try:
        urllib.request.urlretrieve(HERMES_INSTALLER, script)
    except Exception as exc:
        write_job(job_id, state="failed", ok=False, phase="download failed", error=str(exc))
        return

    # The installer prints phase banners but no percentage. Rather than invent one, map the
    # banners we know to coarse checkpoints and always show the phase TEXT — an honest
    # "installing dependencies" beats a fake 47%.
    marks = [("Cloned", 20, "downloading Hermes"),
             ("Virtual environment ready", 35, "setting up Python"),
             ("Installing dependencies", 45, "installing dependencies"),
             ("All dependencies installed", 75, "installing dependencies"),
             ("Node.js", 85, "installing Node components"),
             ("Commands:", 95, "finishing up")]
    log = os.path.join(JOBS_DIR, "hermes-install.log")
    write_job(job_id, state="running", percent=5, phase="starting the installer", log=log)
    with open(log, "w") as out:
        proc = subprocess.Popen(["bash", script, "--skip-setup", "--skip-browser"],
                                stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=enriched_env(), cwd=JOBS_DIR)
        seen = set()
        while proc.poll() is None:
            time.sleep(2)
            try:
                text = open(log, "r", errors="replace").read()
            except Exception:
                continue
            for needle, pct, phase in marks:
                if needle in text and needle not in seen:
                    seen.add(needle)
                    write_job(job_id, state="running", percent=pct, phase=phase)
        proc.wait()

    # Trust the proof, not the exit code.
    binary = which_hermes()
    if not binary:
        tail = ""
        try:
            tail = open(log, "r", errors="replace").read()[-600:]
        except Exception as _xy_e:
            say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_setup.py:511')
        write_job(job_id, state="failed", ok=False, phase="install failed",
                  error="Hermes did not appear after the installer ran", detail=tail)
        return
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                 timeout=90, env=enriched_env()).stdout.strip().splitlines()[0]
    except Exception:
        version = None
    write_job(job_id, state="running", percent=92, phase="installing the document libraries")
    _docs = ensure_doc_libs()   # XY-DOCLIBS
    write_job(job_id, state="done", ok=True, percent=100, phase="installed",
              path=binary, version=version, docLibs=_docs,
              message="Hermes installed" + (" — %s" % version if version else ""))


def ollama_serving_window():
    """The context window a resident model was actually loaded with, or None.

    This is the only number that matters, and it is not the one the Hermes profile asks for.
    Returns None when nothing is loaded, which is not a failure - it just cannot be checked
    until something is.
    """
    info = http_json(OLLAMA_URL + "/api/ps") or {}
    for model in info.get("models") or []:
        try:
            return int(model.get("context_length"))
        except Exception:
            continue
    return None


# XY-OLLAMAHERE (18 Sep 2026) - the flags that make a 64k window fit, in one place, because
# they now have two callers: the post-install start below and the start-what-is-already-here
# path in work_install_ollama.
OLLAMA_SERVE_ENV = {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0"}

# XY-CTXFLOOR (19 Sep 2026) - 65536 was written as a FLOOR and was acting as a CEILING.
#
# MEASURED on Sean's PC, 18 Sep 2026, from Ollama's own server log. A Hermes run opens with
# roughly 45,000 tokens of prompt - RUN.md, the tool schemas of every server the step needs,
# and the staged skills - and each turn adds about 800 more. Task 14959, seventeen turns into
# run_1789783766153: `new prompt, n_ctx_slot = 65536, task.n_tokens = 52716`. Task 99354, the
# last request the 18-minute run ever made: `task.n_tokens = 56741`. So a 65,536-token window
# leaves a real workflow about twenty turns of headroom, and every long run on this machine
# ran out of room rather than out of work.
#
# The same log shows Ollama choosing better by itself when nothing overrides it:
# `vram-based default context total_vram="96.0 GiB" default_num_ctx=262144`. Setting
# OLLAMA_CONTEXT_LENGTH unconditionally replaced that 262,144 with 65,536 - the variable was
# added in August because llama-server used to start at 32,768 whatever the run asked for, and
# a floor written as a fixed number becomes a ceiling on any machine bigger than the one it
# was written on.
#
# So: work out what this computer can afford, ask for that, and never ask for less than the
# 65,536 the August measurement established as the minimum a workflow needs. 262,144 is the
# cap because it is the trained window of every model XYCY currently qualifies.
CTX_FLOOR = 65536
CTX_CAP = 262144


_VRAM_CACHE = {"asked": False, "gib": None}


def total_vram_gib():
    """Memory a model can be served from on this computer, in GiB, or None if unknowable.

    None is a real answer and is never folded into a number: a machine whose memory cannot
    be counted keeps the floor rather than being guessed at.

    Hostile review, 19 Sep 2026, two corrections. (1) This was nvidia-smi only, so a Mac -
    where the GPU shares the machine's memory and Ollama serves from it - was left at the
    65,536 floor for ever, which is the floor-as-ceiling defect XY-CTXFLOOR describes,
    preserved on the other platform the product must work on. On macOS the answer is physical
    memory, read from sysctl, and the same 24 GB allowance for the model comes off it.
    (2) It was called three times per start (parallel, context, env), each with a 20 s timeout,
    so a wedged GPU driver could hold Ollama's start for a minute. Asked once, cached.
    """
    if _VRAM_CACHE["asked"]:
        return _VRAM_CACHE["gib"]
    _VRAM_CACHE["asked"] = True
    gib = None
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=10)
            if out.returncode == 0 and (out.stdout or "").strip().isdigit():
                gib = int(out.stdout.strip()) / (1024.0 ** 3)
        else:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                total = 0.0
                for line in (out.stdout or "").splitlines():
                    line = line.strip()
                    if line:
                        total += float(line) / 1024.0
                gib = total or None
    except Exception:
        gib = None
    _VRAM_CACHE["gib"] = gib
    return gib


def ollama_parallel():
    """How many requests Ollama should serve at once from ONE loaded model.

    XY-OLLAMAPAR. Ollama's default is a single slot, and a second concurrent request makes it
    try to LOAD A SECOND RUNNER. MEASURED on Sean's PC, 18 Sep 2026, during run_1789788593302:
    Hermes had spawned sub-agents, four hermes.exe were live at once, and the server log filled
    with `llama-server GPU discovery watchdog timed out ... context deadline exceeded` followed
    by `[GIN] 500 | 30.0029603s | POST "/v1/chat/completions"` - a request that waited the full
    thirty-second discovery timeout and then failed. A Hermes step delegates by design, so this
    is the normal case for XYCY, not an edge one. Slots on the existing runner cost key-value
    cache and nothing else; a second runner costs another copy of the model and usually cannot
    be had at all.
    """
    vram = total_vram_gib()
    if not vram:
        return 1
    spare = vram - 24.0
    if spare >= 32:
        return 4
    if spare >= 12:
        return 2
    return 1


def ollama_context_length():
    """The context window to ask Ollama for on this computer, as a string.

    Sized from the video memory that is really here, then shared out between the slots, because
    the window is per slot and every slot pays for its own cache. The 16,384-tokens-per-spare-
    gigabyte figure is deliberately four times more cautious than the machine measured: on
    Sean's PC a 262,144-token window at q8_0 took the resident model from 23 GB to 27 GB, about
    64,000 tokens to the gigabyte. Floored and capped.
    """
    vram = total_vram_gib()
    if not vram:
        return str(CTX_FLOOR)
    spare = vram - 24.0            # a 30-35B model at q4 is about this much
    if spare <= 0:
        return str(CTX_FLOOR)
    want = int(spare * 16384) // max(1, ollama_parallel())
    return str(max(CTX_FLOOR, min(CTX_CAP, want)))


def ollama_serve_env():
    """OLLAMA_SERVE_ENV plus the window and the slot count this computer can afford."""
    env = dict(OLLAMA_SERVE_ENV)
    env["OLLAMA_CONTEXT_LENGTH"] = ollama_context_length()
    env["OLLAMA_NUM_PARALLEL"] = str(ollama_parallel())
    return env


def start_ollama(binary):
    """Start an Ollama that is already on this computer. Returns (started, why_not).

    Prefers the same thing a person's own double-click starts, because that is what keeps
    serving after this short-lived setup process exits: Ollama's tray application on Windows,
    the app bundle on macOS. `ollama serve` is the fallback everywhere.
    """
    env = enriched_env()
    env.update(ollama_serve_env())
    try:
        if os.name == "nt":
            tray = os.path.join(os.path.dirname(binary), "ollama app.exe")
            cmd = [tray] if os.path.isfile(tray) else [binary, "serve"]
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP - start_new_session is POSIX only,
            # and without detaching, the server dies with this setup process.
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env,
                             creationflags=0x00000008 | 0x00000200)
            return True, ""
        # NOT `open -a Ollama.app` on macOS, however much more like a person's own start that
        # looks. `open` hands the launch to LaunchServices, the app is started by launchd in
        # ITS environment, and not one of the three variables above reaches the server - which
        # is the whole reason for starting it ourselves. The comment at the post-install start
        # below says what that costs: llama-server comes up at -c 32768, a prompt that outgrows
        # it is silently cut down to the last ~16k, and the run dies with no verdict. So the
        # binary is started directly on every platform.
        subprocess.Popen([binary, "serve"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def work_install_ollama(job_id, args):
    if which_ollama() and http_json(OLLAMA_URL + "/api/version"):
        window = ollama_serving_window()
        if window is not None and window < 65536:
            # Serving, but too small to run on. Saying "already running" here is how this went
            # unnoticed: runs died mid-step with no verdict because their prompts were being
            # silently truncated. Report it instead of passing it.
            write_job(job_id, state="failed", ok=False, phase="window too small",
                      window=window,
                      error="Ollama is running, but it is only giving models a %s-token "
                            "window and XYCY needs %s. Steps will die part-way through "
                            "with no result. Set OLLAMA_CONTEXT_LENGTH=%s in Ollama's "
                            "environment and restart it."
                            % (window, 65536, 65536))
            return
        write_job(job_id, state="done", ok=True, percent=100, phase="already running",
                  window=window,
                  message="Ollama is already installed and serving"
                          + ("" if window is None else " with a %s-token window" % window))
        return
    # XY-OLLAMAHERE (18 Sep 2026) - installed but not running is its own case, and it used to
    # have no branch. The check above needs BOTH a program and a live endpoint, so a computer
    # that already had Ollama and had simply not started it fell straight through to the
    # download below: 173 MB and an interactive setup wizard for a program that was already
    # there. MEASURED on Sean's PC, 18 Sep: Ollama at
    # %LOCALAPPDATA%\Programs\Ollama\ollama.exe, endpoint dead, Hermes parked on "Needs
    # attention", and starting what was there took eight seconds and made it ready. Nothing is
    # downloaded on this path.
    here = which_ollama()
    if here:
        write_job(job_id, state="running", percent=20, phase="starting Ollama",
                  path=here, message="Ollama is already on this computer \u2014 starting it")
        started, why = start_ollama(here)
        if not started:
            write_job(job_id, state="failed", ok=False, phase="could not start",
                      path=here, error="Ollama is installed at %s but would not start: %s"
                                       % (here, why))
            return
        for _ in range(45):
            time.sleep(1)
            info = http_json(OLLAMA_URL + "/api/version")
            if info:
                window = ollama_serving_window()
                # The same refusal the already-running branch above makes, for the same
                # reason: serving with a window too small to run on is not success, and
                # saying "serving now" over it is how steps came to die part-way through
                # with no verdict. Answering is not the test; the window is.
                if window is not None and window < 65536:
                    write_job(job_id, state="failed", ok=False, phase="window too small",
                              path=here, window=window,
                              error="Ollama is running now, but it is only giving models a %s-"
                                    "token window and XYCY needs %s. Steps will die part-way "
                                    "through with no result. Set OLLAMA_CONTEXT_LENGTH=%s in "
                                    "Ollama's environment and restart it."
                                    % (window, 65536, 65536))
                    return
                write_job(job_id, state="done", ok=True, percent=100, phase="running",
                          version=info.get("version"), path=here, window=window,
                          message="Ollama %s was already installed here and is serving now"
                                  % info.get("version", ""))
                return
        write_job(job_id, state="failed", ok=False, phase="not serving", path=here,
                  error="Ollama is installed at %s and was started, but nothing is answering "
                        "on %s. Start Ollama yourself, then press Re-check."
                        % (here, OLLAMA_URL))
        return
    if os.name == "nt":
        # Ollama ships a signed Windows installer. It is an interactive setup program, so XYCY
        # downloads it with a real progress bar and then LAUNCHES it — the last click is the
        # person's. Saying that plainly beats either refusing outright or pretending a silent
        # install worked; the job then waits for the endpoint, so "done" still means serving.
        exe = os.path.join(JOBS_DIR, "OllamaSetup.exe")
        write_job(job_id, state="running", percent=1, phase="downloading Ollama")
        try:
            with urllib.request.urlopen(
                    "https://ollama.com/download/OllamaSetup.exe", timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done, last = 0, 0.0
                with open(exe, "wb") as out:
                    while True:
                        chunk = resp.read(1 << 18)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if time.time() - last > 0.5:
                            last = time.time()
                            write_job(job_id, state="running",
                                      percent=round(done * 60.0 / total, 1) if total else 5,
                                      phase="downloading Ollama", bytesDone=done, bytesTotal=total,
                                      message="%s of %s" % (human(done), human(total)))
        except Exception as exc:
            write_job(job_id, state="failed", ok=False, phase="download failed", error=str(exc))
            return
        write_job(job_id, state="running", percent=65,
                  phase="finish the installer",
                  message="Ollama's installer is open — complete it and this will continue")
        try:
            subprocess.Popen([exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=enriched_env())
        except Exception as exc:
            write_job(job_id, state="failed", ok=False, phase="could not start the installer",
                      error=str(exc))
            return
        # Generous: a person has to click through a setup wizard.
        for _ in range(300):
            time.sleep(2)
            info = http_json(OLLAMA_URL + "/api/version")
            if info:
                write_job(job_id, state="done", ok=True, percent=100, phase="running",
                          version=info.get("version"),
                          message="Ollama %s installed and serving" % info.get("version", ""))
                return
        write_job(job_id, state="failed", ok=False, phase="not serving",
                  error="The installer was launched but nothing is answering on %s yet. "
                        "Finish the Ollama installer, then press Re-check." % OLLAMA_URL)
        return
    if sys.platform != "darwin":
        write_job(job_id, state="failed", ok=False, phase="unsupported",
                  error="XYCY can only install Ollama automatically on macOS and Windows. "
                        "On Linux, install it from ollama.com first — XYCY will find it.")
        return

    binary = which_ollama()
    if not binary:
        zip_path = os.path.join(JOBS_DIR, "Ollama-darwin.zip")
        write_job(job_id, state="running", percent=1, phase="downloading Ollama")
        # A real percentage: ask for the size first, then report bytes as they land.
        try:
            with urllib.request.urlopen(OLLAMA_DMG_ZIP, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done, last = 0, 0.0
                with open(zip_path, "wb") as out:
                    while True:
                        chunk = resp.read(1 << 18)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if time.time() - last > 0.5:
                            last = time.time()
                            write_job(job_id, state="running",
                                      percent=round(done * 55.0 / total, 1) if total else 5,
                                      phase="downloading Ollama", bytesDone=done,
                                      bytesTotal=total,
                                      message="%s of %s" % (human(done), human(total)))
        except Exception as exc:
            write_job(job_id, state="failed", ok=False, phase="download failed", error=str(exc))
            return

        write_job(job_id, state="running", percent=60, phase="installing")
        stage = os.path.join(JOBS_DIR, "ollama-x")
        shutil.rmtree(stage, ignore_errors=True)
        try:
            subprocess.run(["unzip", "-q", zip_path, "-d", stage], check=True, timeout=300)
            app = os.path.join(stage, "Ollama.app")
            dest = "/Applications/Ollama.app"
            if not os.path.isdir(app):
                raise RuntimeError("the download did not contain Ollama.app")
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(app, dest)
            # Downloaded apps are quarantined; without this the first launch is blocked.
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", dest], timeout=120)
            binary = os.path.join(dest, "Contents", "Resources", "ollama")
            os.makedirs(os.path.join(HOME, ".local", "bin"), exist_ok=True)
            link = os.path.join(HOME, ".local", "bin", "ollama")
            if os.path.islink(link) or os.path.exists(link):
                os.unlink(link)
            os.symlink(binary, link)
        except Exception as exc:
            write_job(job_id, state="failed", ok=False, phase="install failed", error=str(exc))
            return

    write_job(job_id, state="running", percent=85, phase="starting Ollama")
    env = enriched_env()
    # Flash attention + a quantised KV cache is what makes a 64k context window fit in a
    # normal amount of memory; without them the cache alone is several gigabytes.
    #
    # OLLAMA_CONTEXT_LENGTH is what actually ASKS for the 64k. It has to be set here, on the
    # server, because XYCY talks to Ollama through its OpenAI-compatible endpoint and that
    # endpoint ignores the per-request window - `model.ollama_num_ctx` in the Hermes profile
    # has no effect on it. Measured 2026-08-15: without this, llama-server starts at -c 32768
    # no matter what the run asks for, and a step that outgrows it does not fail cleanly. Its
    # prompt is silently cut down to the last ~16k, discarding the system prompt and the tool
    # definitions, and the run dies with no verdict. The extra memory is negligible: 21.5 GB
    # resident at 32k versus 21.9 GB at 64k on a 48 GB machine.
    env.update(ollama_serve_env())
    try:
        subprocess.Popen([binary, "serve"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
    except Exception as exc:
        write_job(job_id, state="failed", ok=False, phase="could not start", error=str(exc))
        return

    for _ in range(30):
        time.sleep(1)
        info = http_json(OLLAMA_URL + "/api/version")
        if info:
            write_job(job_id, state="done", ok=True, percent=100, phase="running",
                      version=info.get("version"), path=binary,
                      message="Ollama %s installed and serving" % info.get("version", ""))
            return
    write_job(job_id, state="failed", ok=False, phase="not responding",
              error="Ollama was installed but is not answering on %s" % OLLAMA_URL)


def phase_label(status):
    """Turn Ollama's raw status into something worth putting next to a progress bar.

    It reports `pulling 7f4030143c1c` — the content digest. Correct, and meaningless to the
    person watching; a sha next to a progress bar looks like something has gone wrong.
    """
    s = (status or "").strip()
    low = s.lower()
    if low.startswith("pulling manifest"):
        return "looking up the model"
    if low.startswith("pulling"):
        return "downloading"
    if low.startswith("verifying"):
        return "checking the download"
    if low.startswith("writing"):
        return "saving"
    if low.startswith("success"):
        return "installed"
    return s or "working"


def work_pull_model(job_id, args):
    model = args.model
    if not model:
        write_job(job_id, state="failed", ok=False, error="no model given")
        return
    if not http_json(OLLAMA_URL + "/api/version"):
        write_job(job_id, state="failed", ok=False, phase="no server",
                  error="Nothing is serving models — install Ollama first.")
        return

    write_job(job_id, state="running", percent=0, phase="starting download", model=model)
    # Streaming HTTP, NOT `ollama pull`: the CLI renders a spinner with carriage returns, and
    # this codebase has already lost days to scraping an animated terminal. The API gives exact
    # byte counts, so the progress bar is measured rather than guessed.
    started, last_emit, last_done, rate = time.time(), 0.0, 0, 0.0
    try:
        req = urllib.request.Request(
            OLLAMA_URL + "/api/pull",
            data=json.dumps({"model": model, "stream": True}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                if msg.get("error"):
                    write_job(job_id, state="failed", ok=False, phase="failed",
                              error=str(msg["error"]))
                    return
                status = msg.get("status") or ""
                done, total = msg.get("completed") or 0, msg.get("total") or 0
                now = time.time()
                if now - last_emit > 0.5 or status.startswith("success"):
                    if done and now - last_emit > 0:
                        inst = (done - last_done) / max(now - last_emit, 0.001)
                        rate = inst if not rate else rate * 0.7 + inst * 0.3
                    pct = round(done * 100.0 / total, 1) if total else None
                    eta = round((total - done) / rate) if (total and rate > 0) else None
                    fields = {"state": "running", "phase": phase_label(status),
                              "rateBps": round(rate) if rate else None, "etaSec": eta}
                    # Ollama's later messages ("verifying", "success") carry no byte counts.
                    # Writing those zeros in made a finished download report "0 of 0" at 100%,
                    # which on screen reads as a broken bar rather than a completed one. Only
                    # touch the byte fields when this message actually has bytes in it.
                    if total:
                        fields.update({"percent": pct, "bytesDone": done, "bytesTotal": total,
                                       "message": "%s of %s" % (human(done), human(total))})
                    else:
                        fields["message"] = phase_label(status)
                    write_job(job_id, **fields)
                    last_emit, last_done = now, done
    except Exception as exc:
        write_job(job_id, state="failed", ok=False, phase="download failed", error=str(exc))
        return

    # Prove it landed rather than believing the stream said success.
    tags = http_json(OLLAMA_URL + "/api/tags") or {}
    names = {(m.get("name") or m.get("model")) for m in (tags.get("models") or [])}
    if model not in names:
        write_job(job_id, state="failed", ok=False, phase="missing after download",
                  error="%s is not in the installed list after the download finished" % model)
        return
    size = next((m.get("size") for m in (tags.get("models") or [])
                 if (m.get("name") or m.get("model")) == model), None)
    write_job(job_id, state="done", ok=True, percent=100, phase="installed", model=model,
              bytesDone=size, bytesTotal=size, etaSec=0,
              message="%s downloaded (%s)" % (model, human(size)) if size
                      else "%s downloaded" % model)


WORKERS = {"doc-libs": work_doc_libs,
           "install-hermes": work_install_hermes,
           "install-ollama": work_install_ollama,
           "pull-model": work_pull_model}


def cmd_worker(args):
    take_lock(args.kind, args.job_id)
    try:
        WORKERS[args.kind](args.job_id, args)
    except Exception as exc:
        write_job(args.job_id, state="failed", ok=False, error="unexpected: %s" % exc)
    finally:
        drop_lock(args.kind)


def cmd_job(args):
    if args.list:
        out = []
        for name in sorted(os.listdir(JOBS_DIR)) if os.path.isdir(JOBS_DIR) else []:
            if name.endswith(".json"):
                try:
                    out.append(json.load(open(os.path.join(JOBS_DIR, name), encoding="utf-8")))
                except Exception as _xy_e:
                    say_something(_xy_e, 'local-agent/mcpb/server/hermes/xycy_setup.py:1027')
        print(json.dumps({"ok": True, "jobs": out}))
        return
    try:
        state = json.load(open(job_path(args.id), encoding="utf-8"))
    except Exception:
        print(json.dumps({"ok": False, "error": "no such job: %s" % args.id}))
        return
    # A worker killed mid-flight would otherwise read as "running" forever.
    if state.get("state") == "running" and state.get("pid") and not pid_alive(state["pid"]):
        state = write_job(args.id, state="failed", ok=False,
                          error="the install stopped unexpectedly")
    print(json.dumps({"ok": True, "job": state}))


def main():
    parser = argparse.ArgumentParser(description="XYCY setup actions (background jobs)")
    subs = parser.add_subparsers(dest="cmd", required=True)

    for kind in ("install-hermes", "install-ollama"):
        p = subs.add_parser(kind)
        p.set_defaults(func=lambda a, k=kind: spawn_worker(k, []))

    pull = subs.add_parser("pull-model")
    pull.add_argument("--model", required=True)
    pull.set_defaults(func=lambda a: spawn_worker("pull-model", ["--model", a.model]))

    job = subs.add_parser("job")
    job.add_argument("--id")
    job.add_argument("--list", action="store_true")
    job.set_defaults(func=cmd_job)

    worker = subs.add_parser("_worker")
    worker.add_argument("--kind", required=True)
    worker.add_argument("--job-id", required=True, dest="job_id")
    worker.add_argument("--model")
    worker.set_defaults(func=cmd_worker)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
