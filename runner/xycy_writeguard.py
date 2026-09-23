#!/usr/bin/env python3
"""XY-WRITEGUARD - refuse a file write that leaves the run's own folders, or invents a script.

A Hermes pre_tool_call hook. Hermes pipes {tool_name, tool_input, cwd, ...} in as JSON on
stdin; exiting 2 refuses the call and hands the message on stderr back to the model.

WHY THIS EXISTS, measured on Sean's PC on 19 Sep 2026. One run of Survey - Grading - Permit
Report wrote SEVEN Python files nobody asked for: node2_rhino.py (three times),
node2_execute.py, n2_grade_analysis.py, and n3_build_permit_report.py and
build_permit_report.py straight into the PROJECT ROOT beside his real files. No step in that
plan carries a `script`; RUN.md says DO NOT WRITE CODE FOR RHINO in capitals; node2_rhino.py
was rhinoscript to add the layers, the design surface and the cut-fill grid - the exact work
the step was meant to do through typed tools. The brief already forbade all of it. A rule the
brief states and nothing enforces is not a rule, and the next model will ignore it too.

TWO RULES, and both are about where a run may put things:

1. A write lands inside the run's own folder or the project's outputs/ folder. Nowhere else -
   not the project root, not the home directory, not another run's folder. It is named as a
   FULL path, because a relative one is resolved by the tool and not by this guard, and on
   20 Sep 2026 that difference put a file in the person's home folder while this guard was
   satisfied it was inside outputs/.
2. A .py, .rb, .js, .ps1, .sh or .bat is only written at all when this run DECLARES a script
   step. That is XY-SCRIPTAGENT's contract: a script in a workflow is a Script Sub-Agent the
   person can see on the canvas, never something a model invents mid-run.

DELIBERATELY NARROW. It only ever refuses a WRITE. Reading is untouched, because a step has
to read its plan, its inputs and what it just made. And it fails CLOSED only on the two rules
above: anything it cannot parse is allowed through, because a guard that refuses what it does
not understand would stop real work on the first tool it has not met.
"""
from __future__ import annotations

import json
import os
import re
import sys

WRITE_TOOLS = ("write_file", "patch", "edit_file", "str_replace", "apply_patch",
               "create_file", "append_file", "multi_edit", "notebook_edit")
CODE_SUFFIXES = (".py", ".rb", ".js", ".mjs", ".cjs", ".ps1", ".sh", ".bat", ".cmd", ".vbs")
PATH_KEYS = ("path", "file_path", "filename", "file", "target", "resolved_path", "notebook_path")


def norm(p):
    """An absolute, resolved, comparable path - or None when there was no path.

    The empty string is None, not the current directory. os.path.abspath("") answers the
    process's cwd, so without this an UNSET XYCY_RUN_DIR would quietly become "wherever the
    model happens to be", and the guard would enforce against a root nobody chose. Caught by
    its own test on 19 Sep: with no XYCY variables at all it refused a write it should have
    stood aside for.
    """
    text = str(p or "").strip()
    if not text:
        return None
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(text))))
    except Exception:
        return None


def inside(child, parent):
    if not child or not parent:
        return False
    return child == parent or child.startswith(parent + os.sep)


VERDICT_NAME = re.compile(r"^progress-n[0-9a-z_]+\.json$", re.I)


def run_folder_of(run_dir):
    """XY-VERDICTHOME - the RUN's own folder, when run_dir is one step's folder inside it.

    A per-step run works in runs/<run>__<node>, but the verdict every reader in the product
    looks for is runs/<run>/progress-<node>.json, one level up, because that is where the
    run's bookkeeping lives. The guard knew only the step's own folder, so the LAST
    instruction in every step brief was refused, the step finished without a verdict, and
    the run reported "it stopped without writing its result" over work that had been done.
    Measured 2026-09-20 on his PC on four sub-runs of the Survey workflow.

    Widened by one FILE NAME, not by a folder: a step may write its own verdict there and
    nothing else, so it still cannot reach into another step's.
    """
    run = norm(run_dir)
    if not run:
        return None
    base = os.path.basename(run)
    cut = base.find("__")
    if cut <= 0:
        return None
    return norm(os.path.join(os.path.dirname(run), base[:cut]))


def wanted_path(tool_input):
    """The path this call would write, or None when the shape is one we do not know."""
    if not isinstance(tool_input, dict):
        return None
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                                    # unparseable: not this guard's business
    if str(payload.get("hook_event_name") or "") not in ("pre_tool_call", ""):
        return 0
    tool = str(payload.get("tool_name") or "")
    if tool not in WRITE_TOOLS:
        return 0
    raw = wanted_path(payload.get("tool_input") or {})
    if not raw:
        return 0                                    # a write whose path we cannot see: allowed

    run_said = str(os.environ.get("XYCY_RUN_DIR") or "")      # as the caller wrote it
    out_said = str(os.environ.get("XYCY_OUTPUTS_DIR") or "")   # XY-SAYWHERE quotes these back
    run_dir = norm(run_said)
    out_dir = norm(out_said)
    if not run_dir and not out_dir:
        return 0                                    # not an XYCY run: this guard does not apply

    # XY-WRITEGUARD3 - a RELATIVE path is refused, because this guard cannot know where it
    # will land and MEASURED 20 Sep 2026 it lands somewhere nobody chose.
    #
    # What this line used to do was resolve a relative path against payload["cwd"] and judge
    # THAT. It looked careful and it was wrong, because the guard's answer and the tool's
    # answer are worked out by two different programs. Proved on Sean's PC with the Hermes
    # door, the model asking for write_file {"path": "outputs/allowed.txt"}: the supervisor
    # had started hermes with cwd = the project folder, the guard resolved the path inside
    # the project's outputs/ and allowed it, and Hermes wrote the file to
    # an outputs folder directly under the person's HOME folder, not the project's. The tool's own
    # answer came back in its result as "resolved_path", so there is no ambiguity about what
    # happened. Rule 1 says a write lands in the run folder or outputs/ and nowhere else, and
    # any relative path walked straight past it; `outputs/probe.py` in the same run was caught
    # only by rule 2, so the same write with a .txt name would have landed in the home folder
    # too.
    #
    # So the guard asks for the one thing it can actually check. The message says what to do
    # instead, which costs the model one retry, and the brief already gives every step its
    # outputs folder as a full path (XY-OUTPATH, written after a relative "outputs/" sent
    # three finished steps into an invented folder). A guard that can be walked past by
    # leaving off a drive letter is not a guard.
    if not os.path.isabs(raw):
        sys.stderr.write(
            "XY-WRITEGUARD: write to a FULL path, not a relative one - %s refused. A relative "
            "path is resolved by the tool, not by this check, and it has been measured landing "
            "in the home folder instead of the run. Use the absolute run folder or outputs/ "
            "path you were given in the brief.\n" % raw)
        return 2
    target = norm(raw)
    if not target:
        return 0

    home = run_folder_of(run_dir)                 # XY-VERDICTHOME
    own_verdict = bool(home) and norm(os.path.dirname(target)) == home \
        and bool(VERDICT_NAME.match(os.path.basename(target)))
    if not (inside(target, run_dir) or inside(target, out_dir) or own_verdict):
        # XY-SAYWHERE - the refusal names the two folders, spelled out. It used to say "into
        # outputs/" and leave the model to work out where that was, and MEASURED 20 Sep 2026 the
        # thing the model got wrong WAS the spelling: it typed the project folder with ASCII
        # arrows where the real folder has Unicode ones, and could not recover in nine turns
        # because nothing ever told it the right characters.
        sys.stderr.write(
            "XY-WRITEGUARD: a step writes into its own run folder or into outputs/, and nowhere "
            "else. %s is outside both. Use one of these two exactly as written, character for "
            "character: deliverables go in %s and working files in %s.\n"
            % (raw, out_said or "(no outputs folder)", run_said or "(no run folder)"))
        return 2

    if os.path.splitext(target)[1].lower() in CODE_SUFFIXES and \
            str(os.environ.get("XYCY_SCRIPT_STEP") or "") != "1":
        sys.stderr.write(
            "XY-WRITEGUARD: this run declares no script step, so it does not write program "
            "files - %s refused. A script in a workflow is a Script Sub-Agent the person put on "
            "the canvas. Drive the application through its own tools instead; if a tool you need "
            "is missing, say so in your gaps and mark the step blocked.\n" % raw)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
