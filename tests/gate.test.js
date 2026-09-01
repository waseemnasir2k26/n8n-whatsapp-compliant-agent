// Unit tests for logic/gate.js - the compliance decision core.
// Run: node --test tests/
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const gate = require("../logic/gate.js");

const NOW = "2026-09-01T12:00:00.000Z";
const HOUR = 3600 * 1000;

function ago(ms) {
  return new Date(Date.parse(NOW) - ms).toISOString();
}

function req(overrides) {
  return Object.assign(
    {
      now: NOW,
      lastInboundAt: ago(1 * HOUR),
      optedOut: false,
      optInAt: "2026-01-01T00:00:00.000Z",
      marketingOptIn: false,
      messageType: "freeform",
      sent24h: 0,
      dailyCap: 800,
      sentBurstWindow: 0,
      burstCap: 20,
      circuitBreakerOpen: false,
    },
    overrides || {},
  );
}

// --------------------------------------------------------------------------
test("window: open one hour after inbound, 23h remaining", () => {
  const w = gate.windowState({ now: NOW, lastInboundAt: ago(1 * HOUR) });
  assert.equal(w.open, true);
  assert.equal(w.secondsRemaining, 23 * 3600);
  assert.equal(w.everContacted, true);
});

test("window: one second before 24h is still OPEN", () => {
  const w = gate.windowState({
    now: NOW,
    lastInboundAt: ago(24 * HOUR - 1000),
  });
  assert.equal(w.open, true);
  assert.equal(w.secondsRemaining, 1);
});

test("window: EXACTLY 24h elapsed is CLOSED (the edge that gets accounts banned)", () => {
  const w = gate.windowState({ now: NOW, lastInboundAt: ago(24 * HOUR) });
  assert.equal(w.open, false);
  assert.equal(w.elapsedSeconds, 86400);
  assert.equal(w.secondsRemaining, 0);
});

test("window: one second past 24h is CLOSED", () => {
  const w = gate.windowState({
    now: NOW,
    lastInboundAt: ago(24 * HOUR + 1000),
  });
  assert.equal(w.open, false);
});

test("window: never contacted us = closed, everContacted false", () => {
  const w = gate.windowState({ now: NOW, lastInboundAt: null });
  assert.equal(w.open, false);
  assert.equal(w.everContacted, false);
  assert.equal(w.elapsedSeconds, null);
});

test("window: a future lastInboundAt (clock skew) does not throw or go negative", () => {
  const w = gate.windowState({ now: NOW, lastInboundAt: ago(-5 * HOUR) });
  assert.equal(w.open, true);
  assert.equal(w.elapsedSeconds, 0);
  assert.equal(w.secondsRemaining, 86400);
});

test("window: now is required", () => {
  assert.throws(
    () => gate.windowState({ lastInboundAt: NOW }),
    /now is required/,
  );
});

// --------------------------------------------------------------------------
test("open window: free-form is allowed", () => {
  const d = gate.evaluateSend(req());
  assert.equal(d.allow, true);
  assert.equal(d.action, "send_freeform");
  assert.equal(d.code, "OK_WINDOW_OPEN");
});

test("open window: free-form allowed even with no opt-in on record (they messaged us)", () => {
  const d = gate.evaluateSend(req({ optInAt: null }));
  assert.equal(d.allow, true);
  assert.equal(d.code, "OK_WINDOW_OPEN");
});

test("open window: approved template is allowed", () => {
  const d = gate.evaluateSend(
    req({
      messageType: "template",
      templateName: "order_shipped_v2",
      templateStatus: "APPROVED",
      templateCategory: "UTILITY",
    }),
  );
  assert.equal(d.allow, true);
  assert.equal(d.action, "send_template");
  assert.equal(d.code, "OK_TEMPLATE_IN_WINDOW");
});

test("open window: unapproved template is blocked", () => {
  const d = gate.evaluateSend(
    req({
      messageType: "template",
      templateName: "seasonal_offer_v1",
      templateStatus: "PAUSED",
      templateCategory: "MARKETING",
    }),
  );
  assert.equal(d.allow, false);
  assert.equal(d.code, "TEMPLATE_NOT_APPROVED");
});

// --------------------------------------------------------------------------
test("closed window: free-form is REFUSED and a template is demanded", () => {
  const d = gate.evaluateSend(req({ lastInboundAt: ago(25 * HOUR) }));
  assert.equal(d.allow, false);
  assert.equal(d.action, "block");
  assert.equal(d.code, "TEMPLATE_REQUIRED");
  assert.equal(d.requiresTemplate, true);
  assert.match(d.reason, /approved template/);
});

test("closed window at exactly 24h: free-form is REFUSED", () => {
  const d = gate.evaluateSend(req({ lastInboundAt: ago(24 * HOUR) }));
  assert.equal(d.allow, false);
  assert.equal(d.code, "TEMPLATE_REQUIRED");
});

test("closed window: approved UTILITY template is allowed", () => {
  const d = gate.evaluateSend(
    req({
      lastInboundAt: ago(30 * HOUR),
      messageType: "template",
      templateName: "appointment_reminder_v3",
      templateStatus: "APPROVED",
      templateCategory: "UTILITY",
    }),
  );
  assert.equal(d.allow, true);
  assert.equal(d.code, "OK_TEMPLATE_OUT_OF_WINDOW");
});

test("closed window: no opt-in on record blocks even an approved template", () => {
  const d = gate.evaluateSend(
    req({
      lastInboundAt: ago(30 * HOUR),
      optInAt: null,
      messageType: "template",
      templateStatus: "APPROVED",
      templateCategory: "UTILITY",
    }),
  );
  assert.equal(d.allow, false);
  assert.equal(d.code, "NO_OPT_IN");
});

test("closed window: never-contacted contact with opt-in still needs a template", () => {
  const d = gate.evaluateSend(req({ lastInboundAt: null }));
  assert.equal(d.allow, false);
  assert.equal(d.code, "TEMPLATE_REQUIRED");
  assert.match(d.reason, /no inbound on record/);
});

test("closed window: MARKETING template needs marketing opt-in", () => {
  const base = {
    lastInboundAt: ago(30 * HOUR),
    messageType: "template",
    templateName: "seasonal_offer_v1",
    templateStatus: "APPROVED",
    templateCategory: "MARKETING",
  };
  const blocked = gate.evaluateSend(req(base));
  assert.equal(blocked.allow, false);
  assert.equal(blocked.code, "NO_MARKETING_OPT_IN");

  const allowed = gate.evaluateSend(
    req(Object.assign({ marketingOptIn: true }, base)),
  );
  assert.equal(allowed.allow, true);
});

test("closed window: SERVICE is not a real template category and is rejected", () => {
  const d = gate.evaluateSend(
    req({
      lastInboundAt: ago(30 * HOUR),
      messageType: "template",
      templateStatus: "APPROVED",
      templateCategory: "SERVICE",
    }),
  );
  assert.equal(d.allow, false);
  assert.equal(d.code, "TEMPLATE_CATEGORY_INVALID");
});

// --------------------------------------------------------------------------
test("opt-out blocks even inside an open window", () => {
  const d = gate.evaluateSend(
    req({ optedOut: true, optedOutAt: ago(2 * HOUR) }),
  );
  assert.equal(d.allow, false);
  assert.equal(d.code, "OPTED_OUT");
  assert.match(d.reason, /new explicit opt-in/);
});

test("opt-out beats an approved template outside the window", () => {
  const d = gate.evaluateSend(
    req({
      optedOut: true,
      lastInboundAt: ago(40 * HOUR),
      messageType: "template",
      templateStatus: "APPROVED",
      templateCategory: "UTILITY",
    }),
  );
  assert.equal(d.code, "OPTED_OUT");
});

test("opt-out keyword detection is whole-message and multi-language", () => {
  [
    "STOP",
    "stop",
    " Stop! ",
    "unsubscribe",
    "BAJA",
    "arret",
    "abmelden",
    "berhenti",
    "sair",
  ].forEach((t) =>
    assert.equal(gate.isOptOutMessage(t), true, "should opt out: " + t),
  );
});

test("opt-out keyword detection does not false-positive on normal sentences", () => {
  [
    "stop by tomorrow",
    "can you cancel my order please",
    "I want to end this call",
    "",
    null,
    42,
  ].forEach((t) =>
    assert.equal(gate.isOptOutMessage(t), false, "should NOT opt out: " + t),
  );
});

// --------------------------------------------------------------------------
test("rate limit: daily cap trips at the cap, not one over", () => {
  assert.equal(
    gate.evaluateSend(req({ sent24h: 799, dailyCap: 800 })).allow,
    true,
  );
  const d = gate.evaluateSend(req({ sent24h: 800, dailyCap: 800 }));
  assert.equal(d.allow, false);
  assert.equal(d.code, "DAILY_CAP_REACHED");
});

test("rate limit: burst cap trips", () => {
  const d = gate.evaluateSend(req({ sentBurstWindow: 20, burstCap: 20 }));
  assert.equal(d.allow, false);
  assert.equal(d.code, "BURST_CAP_REACHED");
});

test('rate limit: a cap of 0 means unset, not "block everything"', () => {
  const d = gate.evaluateSend(req({ sent24h: 5000, dailyCap: 0, burstCap: 0 }));
  assert.equal(d.allow, true);
});

test("rate limit: caps apply inside an open window too", () => {
  const d = gate.evaluateSend(
    req({ lastInboundAt: ago(1 * HOUR), sent24h: 900, dailyCap: 800 }),
  );
  assert.equal(d.code, "DAILY_CAP_REACHED");
});

// --------------------------------------------------------------------------
test("circuit breaker beats everything, including an in-window reply", () => {
  const d = gate.evaluateSend(
    req({ circuitBreakerOpen: true, circuitBreakerReason: "quality RED" }),
  );
  assert.equal(d.allow, false);
  assert.equal(d.code, "CIRCUIT_BREAKER_OPEN");
  assert.match(d.reason, /quality RED/);
});

// --------------------------------------------------------------------------
test("every refusal carries a code and a human-readable reason", () => {
  const refusals = [
    req({ circuitBreakerOpen: true }),
    req({ optedOut: true }),
    req({ sent24h: 800, dailyCap: 800 }),
    req({ sentBurstWindow: 99, burstCap: 5 }),
    req({ lastInboundAt: ago(48 * HOUR) }),
    req({ lastInboundAt: ago(48 * HOUR), optInAt: null }),
    req({
      lastInboundAt: ago(48 * HOUR),
      messageType: "template",
      templateStatus: "PENDING",
    }),
  ].map(gate.evaluateSend);

  refusals.forEach((d) => {
    assert.equal(d.allow, false);
    assert.equal(d.action, "block");
    assert.ok(d.code && d.code.length > 3, "missing code");
    assert.ok(d.reason && d.reason.length > 10, "missing reason for " + d.code);
    assert.ok(d.window, "refusal must carry window context for the log");
  });
  assert.equal(
    new Set(refusals.map((d) => d.code)).size,
    refusals.length,
    "each refusal path must have a distinct code",
  );
});

test("constants match Meta: 24h window, three template categories", () => {
  assert.equal(gate.WINDOW_SECONDS, 86400);
  assert.deepEqual(gate.TEMPLATE_CATEGORIES, [
    "AUTHENTICATION",
    "MARKETING",
    "UTILITY",
  ]);
});
