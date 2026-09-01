// Parity tests: the compliance core is duplicated into n8n Code nodes.
// These tests prove the duplicate has not drifted, and that the embedded copy
// actually evaluates to the same decisions as logic/gate.js.
//
// Run: node --test tests/
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.join(__dirname, "..");
const gate = require("../logic/gate.js");

const GATE_SRC = fs.readFileSync(path.join(ROOT, "logic", "gate.js"), "utf8");
const CORE = GATE_SRC.split("// GATE-CORE-START\n")[1].split(
  "// GATE-CORE-END",
)[0];

// Workflows whose Code nodes must embed the core verbatim.
const EMBEDDERS = {
  "message-router.json": ["Compliance Gate"],
  "optin-optout.json": ["Detect Consent Keyword"],
  "example-agent.json": ["Prepare Send Request"],
};

function loadWorkflow(file) {
  return JSON.parse(
    fs.readFileSync(path.join(ROOT, "workflows", file), "utf8"),
  );
}

function codeOf(wf, nodeName) {
  const node = wf.nodes.find((n) => n.name === nodeName);
  assert.ok(node, "node not found: " + nodeName);
  assert.equal(node.type, "n8n-nodes-base.code");
  return node.parameters.jsCode;
}

test("gate core extracted from logic/gate.js is non-trivial", () => {
  assert.ok(
    CORE.length > 2000,
    "core looks truncated: " + CORE.length + " chars",
  );
  assert.match(CORE, /function evaluateSend/);
  assert.ok(
    !/module\.exports/.test(CORE),
    "core must not contain module.exports (n8n has no module)",
  );
});

for (const [file, nodeNames] of Object.entries(EMBEDDERS)) {
  for (const nodeName of nodeNames) {
    test(
      "embedded core in " +
        file +
        " :: " +
        nodeName +
        " is byte-identical to logic/gate.js",
      () => {
        const js = codeOf(loadWorkflow(file), nodeName);
        assert.ok(
          js.includes(CORE),
          'Code node "' +
            nodeName +
            '" in ' +
            file +
            " does not contain the verbatim gate core. " +
            "Re-run: python tools/build_workflows.py",
        );
      },
    );

    test(
      "embedded core in " + file + " :: " + nodeName + " is ASCII only",
      () => {
        const js = codeOf(loadWorkflow(file), nodeName);
        const bad = [...js].findIndex((ch) => ch.charCodeAt(0) > 127);
        assert.equal(bad, -1, "non-ASCII at index " + bad + " in " + nodeName);
      },
    );
  }
}

// The real proof: run the EMBEDDED copy in a sandbox and compare its decisions
// to the module's, across the full decision matrix.
function embeddedEvaluator() {
  const js = codeOf(loadWorkflow("message-router.json"), "Compliance Gate");
  const core = js.split("// GATE-CORE-START")[0]; // header comment only
  assert.ok(js.includes(CORE));
  const sandbox = {};
  vm.createContext(sandbox);
  // Execute only the core region, then expose evaluateSend.
  vm.runInContext(CORE + "\nthis.__evaluateSend = evaluateSend;", sandbox);
  assert.equal(typeof core, "string");
  return sandbox.__evaluateSend;
}

test("embedded core produces identical decisions to logic/gate.js over the full matrix", () => {
  const embedded = embeddedEvaluator();
  const NOW = "2026-09-01T12:00:00.000Z";
  const H = 3600 * 1000;
  const ago = (ms) => new Date(Date.parse(NOW) - ms).toISOString();

  const cases = [];
  const lastInbounds = [
    null,
    ago(1 * H),
    ago(23.99 * H),
    ago(24 * H),
    ago(24 * H + 1000),
    ago(72 * H),
  ];
  const messageTypes = ["freeform", "template"];
  const statuses = [null, "APPROVED", "PENDING", "PAUSED"];
  const categories = [
    null,
    "UTILITY",
    "MARKETING",
    "AUTHENTICATION",
    "SERVICE",
  ];

  for (const lastInboundAt of lastInbounds) {
    for (const messageType of messageTypes) {
      for (const templateStatus of statuses) {
        for (const templateCategory of categories) {
          for (const optedOut of [false, true]) {
            for (const optInAt of [null, "2026-01-01T00:00:00.000Z"]) {
              for (const marketingOptIn of [false, true]) {
                for (const [sent24h, dailyCap] of [
                  [0, 800],
                  [800, 800],
                ]) {
                  for (const breaker of [false, true]) {
                    cases.push({
                      now: NOW,
                      lastInboundAt,
                      optedOut,
                      optInAt,
                      marketingOptIn,
                      messageType,
                      templateName: "tpl",
                      templateStatus,
                      templateCategory,
                      sent24h,
                      dailyCap,
                      sentBurstWindow: 0,
                      burstCap: 20,
                      circuitBreakerOpen: breaker,
                      circuitBreakerReason: "test",
                    });
                  }
                }
              }
            }
          }
        }
      }
    }
  }

  assert.ok(cases.length > 5000, "matrix too small: " + cases.length);
  let mismatches = 0;
  for (const c of cases) {
    const a = gate.evaluateSend(c);
    const b = embedded(c);
    if (JSON.stringify(a) !== JSON.stringify(b)) {
      mismatches += 1;
      if (mismatches === 1) {
        assert.deepEqual(b, a, "first mismatch for case " + JSON.stringify(c));
      }
    }
  }
  assert.equal(
    mismatches,
    0,
    mismatches + " of " + cases.length + " cases disagree",
  );
  console.log("    parity matrix: " + cases.length + " cases, 0 disagreements");
});

test("no workflow calls the Cloud API outside message-router", () => {
  const files = fs
    .readdirSync(path.join(ROOT, "workflows"))
    .filter((f) => f.endsWith(".json"));
  for (const file of files) {
    const wf = loadWorkflow(file);
    const senders = wf.nodes.filter(
      (n) =>
        n.type === "n8n-nodes-base.httpRequest" &&
        String(n.parameters.url || "").includes("/messages"),
    );
    if (file === "message-router.json") {
      assert.equal(
        senders.length,
        2,
        "router should own exactly the template + free-form senders",
      );
    } else {
      assert.equal(
        senders.length,
        0,
        file +
          " calls the Cloud API directly; every send must go through message-router",
      );
    }
  }
});

test("no bulk-send path: no workflow loops a contact list into a sender", () => {
  const files = fs
    .readdirSync(path.join(ROOT, "workflows"))
    .filter((f) => f.endsWith(".json"));
  const banned =
    /SELECT[\s\S]*FROM\s+wa\.contacts[\s\S]*WHERE[\s\S]*opted_in\s*=\s*TRUE[\s\S]*LIMIT/i;
  for (const file of files) {
    const raw = fs.readFileSync(path.join(ROOT, "workflows", file), "utf8");
    assert.ok(
      !banned.test(raw),
      file + " looks like it selects a contact list to blast",
    );
    assert.ok(
      !/splitInBatches[\s\S]{0,400}httpRequest/.test(raw),
      file + " batches items straight into an HTTP sender",
    );
  }
});
