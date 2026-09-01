/*
 * gate.js - the compliance decision core for n8n-whatsapp-compliant-agent.
 *
 * Pure functions, no I/O, no n8n globals. Everything between the
 * GATE-CORE-START and GATE-CORE-END markers is copied VERBATIM into the
 * Code nodes of workflows/message-router.json and workflows/example-agent.json
 * by tools/build_workflows.py, and tests/parity.test.js proves the copy is
 * byte-identical. Never edit the copy inside a workflow JSON by hand.
 *
 * ASCII only. n8n rejects non-ASCII characters in Code node jsCode.
 *
 * Policy sources (verified 2026-09-01, re-verify before you rely on them):
 *  - 24h customer service window, resets on each inbound user message or call;
 *    when it closes only pre-approved templates may be sent.
 *    https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
 *  - Template categories are AUTHENTICATION, MARKETING, UTILITY; templates must
 *    be APPROVED before use and can be PAUSED for negative feedback.
 *    https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview
 *  - Opt-in must be obtained before messaging, must name the business, and
 *    opt-out requests must be honored.
 *    https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in
 *  - Messaging limits are per unique customer per 24 hours (250 / 2K / 10K /
 *    100K / unlimited).
 *    https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits
 *  - Spam enforcement escalates from warning to send blocks to permanent
 *    disabling; excessive negative user feedback can get you offboarded.
 *    https://developers.facebook.com/documentation/business-messaging/whatsapp/policy-enforcement
 */

// GATE-CORE-START
var WINDOW_SECONDS = 86400; // 24 hours. Verified: Meta send-messages guide.

// Template categories Meta actually defines. "SERVICE" is NOT a template
// category - service messages are the free-form messages you may send while a
// customer service window is open.
var TEMPLATE_CATEGORIES = ["AUTHENTICATION", "MARKETING", "UTILITY"];

// UNVERIFIED against Meta docs: WhatsApp does not publish a mandated STOP
// keyword list the way SMS carriers do. This list is this kit's own convention
// for honoring "clear instructions for how people can opt out". Extend it for
// your own markets - it is deliberately conservative, because a false opt-out
// is cheaper than an ignored one.
var STOP_KEYWORDS = [
  "stop",
  "unsubscribe",
  "cancel",
  "end",
  "quit",
  "optout",
  "opt out",
  "baja",
  "parar",
  "cancelar",
  "detener",
  "arret",
  "arreter",
  "desabonner",
  "stopp",
  "abmelden",
  "beenden",
  "sair",
  "cancelar tudo",
  "berhenti",
  "henti",
  "basta",
  "annulla",
  "stop tout",
];

function normalizeText(value) {
  if (typeof value !== "string") {
    return "";
  }
  return value
    .toLowerCase()
    .replace(/[^a-z0-9 ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// True only when the whole message is an opt-out instruction. A message that
// merely contains the word "stop" ("stop by tomorrow") is not an opt-out.
function isOptOutMessage(text) {
  var normalized = normalizeText(text);
  if (!normalized) {
    return false;
  }
  for (var i = 0; i < STOP_KEYWORDS.length; i++) {
    if (normalized === STOP_KEYWORDS[i]) {
      return true;
    }
  }
  return false;
}

function toEpochSeconds(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  if (typeof value === "number") {
    return Math.floor(value > 1e12 ? value / 1000 : value);
  }
  var parsed = Date.parse(value);
  if (isNaN(parsed)) {
    return null;
  }
  return Math.floor(parsed / 1000);
}

// The customer service window: opens/resets on every inbound user message or
// call and lasts 24h. Exactly 24h elapsed counts as CLOSED - the boundary is
// treated conservatively so a one-second clock skew cannot produce an
// out-of-window free-form send.
function windowState(input) {
  var now = toEpochSeconds(input && input.now);
  var last = toEpochSeconds(input && input.lastInboundAt);
  if (now === null) {
    throw new Error("windowState: now is required");
  }
  if (last === null) {
    return {
      open: false,
      elapsedSeconds: null,
      secondsRemaining: 0,
      everContacted: false,
    };
  }
  var elapsed = now - last;
  if (elapsed < 0) {
    elapsed = 0;
  } // future timestamp = clock skew, treat as just now
  var open = elapsed < WINDOW_SECONDS;
  return {
    open: open,
    elapsedSeconds: elapsed,
    secondsRemaining: open ? WINDOW_SECONDS - elapsed : 0,
    everContacted: true,
  };
}

function rateState(input) {
  var sent24h = Number((input && input.sent24h) || 0);
  var dailyCap = Number((input && input.dailyCap) || 0);
  var sentBurstWindow = Number((input && input.sentBurstWindow) || 0);
  var burstCap = Number((input && input.burstCap) || 0);
  var dailyExhausted = dailyCap > 0 && sent24h >= dailyCap;
  var burstExhausted = burstCap > 0 && sentBurstWindow >= burstCap;
  return {
    dailyExhausted: dailyExhausted,
    burstExhausted: burstExhausted,
    dailyRemaining: dailyCap > 0 ? Math.max(0, dailyCap - sent24h) : null,
    burstRemaining:
      burstCap > 0 ? Math.max(0, burstCap - sentBurstWindow) : null,
  };
}

function deny(code, reason, extra) {
  var out = {
    allow: false,
    action: "block",
    code: code,
    reason: reason,
    requiresTemplate: false,
  };
  if (extra) {
    for (var k in extra) {
      if (Object.prototype.hasOwnProperty.call(extra, k)) {
        out[k] = extra[k];
      }
    }
  }
  return out;
}

/*
 * evaluateSend - the single gate every outbound message must pass.
 *
 * request: {
 *   now, lastInboundAt,
 *   optedOut, optedOutAt, optInAt, marketingOptIn,
 *   messageType: 'freeform' | 'template',
 *   templateName, templateStatus, templateCategory,
 *   sent24h, dailyCap, sentBurstWindow, burstCap,
 *   circuitBreakerOpen, circuitBreakerReason
 * }
 *
 * Returns { allow, action, code, reason, window, rate, messageType }.
 * action is one of: 'send_freeform' | 'send_template' | 'block'.
 * A refusal is never silent: the caller MUST log every allow:false result.
 */
function evaluateSend(request) {
  var req = request || {};
  var win = windowState({ now: req.now, lastInboundAt: req.lastInboundAt });
  var rate = rateState(req);
  var messageType = req.messageType === "template" ? "template" : "freeform";
  var context = { window: win, rate: rate, messageType: messageType };

  // 1. Circuit breaker - quality degraded, all outbound halted.
  if (req.circuitBreakerOpen === true) {
    return deny(
      "CIRCUIT_BREAKER_OPEN",
      "Outbound halted by the quality circuit breaker: " +
        (req.circuitBreakerReason || "unspecified"),
      context,
    );
  }

  // 2. Opt-out is absolute and applies inside the window too. Reversible only
  // by an explicit new opt-in event written to the ledger.
  if (req.optedOut === true) {
    return deny(
      "OPTED_OUT",
      "Contact opted out at " +
        (req.optedOutAt || "unknown time") +
        ". Sending requires a new explicit opt-in.",
      context,
    );
  }

  // 3. Rate limits, checked before the window so a burst cannot be laundered
  // through an open window.
  if (rate.dailyExhausted) {
    return deny(
      "DAILY_CAP_REACHED",
      "Daily send cap reached (" +
        Number(req.sent24h || 0) +
        "/" +
        Number(req.dailyCap || 0) +
        ").",
      context,
    );
  }
  if (rate.burstExhausted) {
    return deny(
      "BURST_CAP_REACHED",
      "Burst cap reached (" +
        Number(req.sentBurstWindow || 0) +
        "/" +
        Number(req.burstCap || 0) +
        " in the burst window).",
      context,
    );
  }

  // 4. Window open -> free-form service messages are allowed.
  if (win.open) {
    if (messageType === "template") {
      if (req.templateStatus !== "APPROVED") {
        return deny(
          "TEMPLATE_NOT_APPROVED",
          "Template " +
            (req.templateName || "(unnamed)") +
            " has status " +
            (req.templateStatus || "UNKNOWN") +
            ", not APPROVED.",
          context,
        );
      }
      return {
        allow: true,
        action: "send_template",
        code: "OK_TEMPLATE_IN_WINDOW",
        reason: "Window open; approved template.",
        requiresTemplate: false,
        window: win,
        rate: rate,
        messageType: messageType,
      };
    }
    return {
      allow: true,
      action: "send_freeform",
      code: "OK_WINDOW_OPEN",
      reason:
        "Customer service window open, " +
        win.secondsRemaining +
        "s remaining.",
      requiresTemplate: false,
      window: win,
      rate: rate,
      messageType: messageType,
    };
  }

  // 5. Window closed. Business-initiated territory: opt-in is mandatory.
  if (toEpochSeconds(req.optInAt) === null) {
    return deny(
      "NO_OPT_IN",
      "No opt-in on record and the customer service window is closed. Business-initiated messages require opt-in.",
      context,
    );
  }

  if (messageType !== "template") {
    var refusal = deny(
      "TEMPLATE_REQUIRED",
      "Customer service window closed" +
        (win.everContacted
          ? " (" + win.elapsedSeconds + "s since last inbound)"
          : " (no inbound on record)") +
        ". Free-form messages outside the window are a policy violation - send an approved template instead.",
      context,
    );
    refusal.requiresTemplate = true;
    return refusal;
  }

  if (req.templateStatus !== "APPROVED") {
    return deny(
      "TEMPLATE_NOT_APPROVED",
      "Template " +
        (req.templateName || "(unnamed)") +
        " has status " +
        (req.templateStatus || "UNKNOWN") +
        ", not APPROVED.",
      context,
    );
  }

  var category = String(req.templateCategory || "").toUpperCase();
  if (TEMPLATE_CATEGORIES.indexOf(category) === -1) {
    return deny(
      "TEMPLATE_CATEGORY_INVALID",
      "Template category " +
        (req.templateCategory || "(none)") +
        " is not one of " +
        TEMPLATE_CATEGORIES.join(", ") +
        ".",
      context,
    );
  }

  // Marketing needs marketing-specific consent. UNVERIFIED as a separate,
  // explicit Meta requirement: Meta requires opt-in and requires honoring
  // opt-out "for specific categories of messages". This kit enforces a distinct
  // marketing consent flag because that is the safe reading.
  if (category === "MARKETING" && req.marketingOptIn !== true) {
    return deny(
      "NO_MARKETING_OPT_IN",
      "MARKETING template requested but the contact has no marketing opt-in on record.",
      context,
    );
  }

  return {
    allow: true,
    action: "send_template",
    code: "OK_TEMPLATE_OUT_OF_WINDOW",
    reason:
      "Window closed; approved " +
      category +
      " template with opt-in on record.",
    requiresTemplate: false,
    window: win,
    rate: rate,
    messageType: messageType,
  };
}
// GATE-CORE-END

module.exports = {
  WINDOW_SECONDS: WINDOW_SECONDS,
  TEMPLATE_CATEGORIES: TEMPLATE_CATEGORIES,
  STOP_KEYWORDS: STOP_KEYWORDS,
  normalizeText: normalizeText,
  isOptOutMessage: isOptOutMessage,
  toEpochSeconds: toEpochSeconds,
  windowState: windowState,
  rateState: rateState,
  evaluateSend: evaluateSend,
};
