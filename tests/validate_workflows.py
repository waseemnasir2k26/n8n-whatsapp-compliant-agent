#!/usr/bin/env python3
"""Structural validator for the n8n workflow JSONs in workflows/.

An importable n8n workflow that is subtly malformed fails at import time with an
unhelpful error, or worse, imports and silently drops a connection. This asserts
the invariants that actually bite:

  * valid JSON, top-level `nodes` array + `connections` object + `settings`
  * every node has id, name, type, typeVersion, position, parameters
  * node ids and node names are unique
  * every name used in `connections` (source AND target) exists in `nodes`
  * connection payloads are well formed ([[{node,type,index}]])
  * Code node jsCode is ASCII only - n8n rejects non-ASCII in jsCode
  * no smart quotes / em dashes anywhere in the file
  * no leaked secrets: no real-looking tokens, no E.164 phone numbers
  * every workflow ships inactive
  * the six expected workflow files are present

Run:  python tests/validate_workflows.py
Exit code 0 = pass. Anything else = do not import these into n8n.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(ROOT, "workflows")

EXPECTED = [
    "window-tracker.json",
    "message-router.json",
    "optin-optout.json",
    "rate-limiter.json",
    "quality-monitor.json",
    "example-agent.json",
]

REQUIRED_NODE_KEYS = ["id", "name", "type", "typeVersion", "position", "parameters"]

SMART_CHARS = {
    "‘": "left single quote",
    "’": "right single quote",
    "“": "left double quote",
    "”": "right double quote",
    "–": "en dash",
    "—": "em dash",
    "…": "ellipsis",
    " ": "non-breaking space",
}

# Things that must never be committed.
SECRET_PATTERNS = [
    (re.compile(r"EAA[A-Za-z0-9]{20,}"), "Meta/Facebook access token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style secret key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"\+\d{9,15}\b"), "E.164 phone number"),
    (re.compile(r"postgres(?:ql)?://[^\s\"]+:[^\s\"@]+@"), "Postgres URL with password"),
]

failures = []
checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)
    return condition


def validate_file(fname):
    path = os.path.join(WORKFLOW_DIR, fname)
    if not check(os.path.isfile(path), fname + ": file is missing"):
        return

    raw = open(path, "r", encoding="utf-8").read()

    for ch, label in SMART_CHARS.items():
        check(ch not in raw, fname + ": contains " + label + " (U+%04X)" % ord(ch))

    for pattern, label in SECRET_PATTERNS:
        m = pattern.search(raw)
        check(m is None, fname + ": looks like a leaked " + label +
              (" -> " + m.group(0)[:12] + "..." if m else ""))

    try:
        wf = json.loads(raw)
    except ValueError as exc:
        check(False, fname + ": invalid JSON: " + str(exc))
        return

    if not check(isinstance(wf, dict), fname + ": top level must be an object"):
        return
    check("nodes" in wf and isinstance(wf["nodes"], list), fname + ": missing top-level nodes array")
    check("connections" in wf and isinstance(wf["connections"], dict),
          fname + ": missing top-level connections object")
    check("settings" in wf and isinstance(wf["settings"], dict),
          fname + ": missing top-level settings object")
    check(wf.get("active") is not True, fname + ": ships with active=true; workflows must import inactive")

    nodes = wf.get("nodes", [])
    check(len(nodes) > 0, fname + ": has no nodes")

    names = []
    ids = []
    for i, n in enumerate(nodes):
        where = fname + " node[" + str(i) + "]"
        if not check(isinstance(n, dict), where + ": is not an object"):
            continue
        for key in REQUIRED_NODE_KEYS:
            check(key in n, where + " (" + str(n.get("name", "?")) + "): missing '" + key + "'")
        check(isinstance(n.get("position"), list) and len(n.get("position", [])) == 2,
              where + ": position must be [x, y]")
        check(isinstance(n.get("parameters"), dict), where + ": parameters must be an object")
        check(isinstance(n.get("typeVersion"), (int, float)), where + ": typeVersion must be numeric")
        check(bool(str(n.get("type", ""))), where + ": empty type")
        names.append(n.get("name"))
        ids.append(n.get("id"))

        if n.get("type") == "n8n-nodes-base.code":
            js = n.get("parameters", {}).get("jsCode", "")
            check(isinstance(js, str) and js.strip() != "",
                  where + " (" + str(n.get("name")) + "): Code node has empty jsCode")
            bad = [(idx, ch) for idx, ch in enumerate(js) if ord(ch) > 127]
            check(not bad, where + " (" + str(n.get("name")) + "): jsCode has non-ASCII at offset " +
                  (str(bad[0][0]) + " -> U+%04X" % ord(bad[0][1]) if bad else ""))
            for marker in ("TODO", "FIXME", "lorem ipsum"):
                check(marker.lower() not in js.lower(),
                      where + " (" + str(n.get("name")) + "): jsCode contains '" + marker + "'")

    check(len(names) == len(set(names)), fname + ": duplicate node names: " +
          str(sorted({x for x in names if names.count(x) > 1})))
    check(len(ids) == len(set(ids)), fname + ": duplicate node ids: " +
          str(sorted({x for x in ids if ids.count(x) > 1})))

    known = set(names)
    for source, outputs in wf.get("connections", {}).items():
        check(source in known, fname + ": connections reference unknown source node '" + str(source) + "'")
        if not check(isinstance(outputs, dict), fname + ": connections['" + str(source) + "'] must be an object"):
            continue
        for ctype, branches in outputs.items():
            if not check(isinstance(branches, list),
                         fname + ": connections['" + str(source) + "']['" + str(ctype) + "'] must be a list"):
                continue
            for bi, branch in enumerate(branches):
                if branch is None:
                    continue  # n8n allows a null placeholder for an unused output
                if not check(isinstance(branch, list),
                             fname + ": " + str(source) + " output " + str(bi) + " must be a list"):
                    continue
                for link in branch:
                    if not check(isinstance(link, dict),
                                 fname + ": " + str(source) + " output " + str(bi) + " has a non-object link"):
                        continue
                    for key in ("node", "type", "index"):
                        check(key in link, fname + ": link from " + str(source) + " missing '" + key + "'")
                    check(link.get("node") in known,
                          fname + ": " + str(source) + " connects to unknown node '" +
                          str(link.get("node")) + "'")
                    check(link.get("type") == ctype,
                          fname + ": " + str(source) + " link type '" + str(link.get("type")) +
                          "' does not match connection key '" + str(ctype) + "'")

    # Every node except triggers, sticky notes and terminal nodes should be
    # reachable. We only assert that non-trigger nodes are reachable from some
    # source, which catches orphaned nodes left behind by an edit.
    targets = set()
    for outputs in wf.get("connections", {}).values():
        for branches in outputs.values():
            for branch in branches or []:
                for link in branch or []:
                    if isinstance(link, dict):
                        targets.add(link.get("node"))
    for n in nodes:
        t = str(n.get("type", ""))
        if t == "n8n-nodes-base.stickyNote":
            continue
        is_trigger = ("trigger" in t.lower()) or t.endswith(".webhook")
        if is_trigger:
            continue
        # LangChain sub-nodes (models, memory, tools) attach upward: they are
        # the SOURCE of a non-main connection rather than a target.
        attaches_upward = any(
            ctype != "main"
            for ctype in wf.get("connections", {}).get(n.get("name"), {})
        )
        check(n.get("name") in targets or attaches_upward,
              fname + ": node '" + str(n.get("name")) + "' is orphaned (nothing connects to it)")

    print("  ok  " + fname + "  (" + str(len(nodes)) + " nodes, " +
          str(len(wf.get("connections", {}))) + " connected sources)")


def main():
    print("validating workflows in " + os.path.relpath(WORKFLOW_DIR, ROOT) + "/")
    present = sorted(f for f in os.listdir(WORKFLOW_DIR) if f.endswith(".json"))
    check(set(present) == set(EXPECTED),
          "workflows/ should contain exactly " + str(EXPECTED) + " but has " + str(present))
    for fname in EXPECTED:
        validate_file(fname)

    print("")
    if failures:
        print("FAILED " + str(len(failures)) + " of " + str(checks) + " checks:")
        for f in failures:
            print("  - " + f)
        return 1
    print("PASSED all " + str(checks) + " checks across " + str(len(EXPECTED)) + " workflows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
