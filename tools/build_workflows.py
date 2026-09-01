#!/usr/bin/env python3
"""Generate the importable n8n workflow JSONs in workflows/.

Why a generator: the compliance decision core lives in logic/gate.js and is
unit-tested there. The Code nodes in message-router.json and example-agent.json
must run the SAME code, byte for byte. This script copies the region between the
GATE-CORE-START and GATE-CORE-END markers into those nodes, and
tests/parity.test.js fails the build if the copies ever drift.

Run:  python tools/build_workflows.py
Then: python tests/validate_workflows.py && node --test tests/
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(ROOT, "workflows")
GATE_PATH = os.path.join(ROOT, "logic", "gate.js")

PG_CRED = {"postgres": {"id": "REPLACE_WITH_YOUR_POSTGRES_CRED_ID",
                        "name": "WhatsApp Compliance Postgres"}}
WA_CRED = {"httpHeaderAuth": {"id": "REPLACE_WITH_YOUR_HTTP_HEADER_CRED_ID",
                              "name": "WhatsApp Cloud API Token"}}

GRAPH = "https://graph.facebook.com/v21.0"


def gate_core():
    src = open(GATE_PATH, "r", encoding="utf-8").read()
    m = re.search(r"// GATE-CORE-START\n(.*?)// GATE-CORE-END", src, re.S)
    if not m:
        raise SystemExit("gate.js: GATE-CORE markers not found")
    core = m.group(1)
    if any(ord(ch) > 127 for ch in core):
        raise SystemExit("gate.js core contains non-ASCII characters")
    return core


CORE = gate_core()


def node(nid, name, ntype, type_version, position, parameters, **extra):
    n = {
        "id": nid,
        "name": name,
        "type": ntype,
        "typeVersion": type_version,
        "position": list(position),
        "parameters": parameters,
    }
    n.update(extra)
    return n


def code_node(nid, name, position, js):
    if any(ord(ch) > 127 for ch in js):
        raise SystemExit("non-ASCII jsCode in node " + name)
    return node(nid, name, "n8n-nodes-base.code", 2, position,
                {"mode": "runOnceForAllItems", "jsCode": js})


def pg_node(nid, name, position, query, options=None):
    params = {
        "operation": "executeQuery",
        "query": query,
        "options": options or {},
    }
    return node(nid, name, "n8n-nodes-base.postgres", 2.4, position, params,
                credentials=dict(PG_CRED))


def http_node(nid, name, position, url, body_expr, method="POST"):
    return node(nid, name, "n8n-nodes-base.httpRequest", 4.2, position, {
        "method": method,
        "url": url,
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": body_expr,
        "options": {"response": {"response": {"neverError": True, "fullResponse": True}}},
    }, credentials=dict(WA_CRED))


def if_node(nid, name, position, left, operator, right=None, value_type="boolean"):
    condition = {
        "id": nid + "-c1",
        "leftValue": left,
        "rightValue": right if right is not None else "",
        "operator": {"type": value_type,
                     "operation": operator,
                     "singleValue": True} if right is None else
                    {"type": value_type, "operation": operator},
    }
    return node(nid, name, "n8n-nodes-base.if", 2.2, position, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict",
                        "version": 2},
            "conditions": [condition],
            "combinator": "and",
        },
        "options": {},
    })


def noop(nid, name, position):
    return node(nid, name, "n8n-nodes-base.noOp", 1, position, {})


def sticky(nid, position, content, width=460, height=240):
    # The note bodies are written with literal \n escapes for readability in
    # this file; sticky notes render real newlines.
    return node(nid, "Note " + nid, "n8n-nodes-base.stickyNote", 1, position,
                {"content": content.replace("\\n", "\n"),
                 "width": width, "height": height, "color": 4})


def conn(pairs):
    """pairs: list of (from_name, output_index, to_name, conn_type)."""
    out = {}
    for frm, idx, to, ctype in pairs:
        slot = out.setdefault(frm, {}).setdefault(ctype, [])
        while len(slot) <= idx:
            slot.append([])
        slot[idx].append({"node": to, "type": ctype, "index": 0})
    return out


def workflow(name, nodes, connections, tags):
    return {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": {
            "executionOrder": "v1",
            "saveManualExecutions": True,
            "saveDataErrorExecution": "all",
            "saveDataSuccessExecution": "all",
            "callerPolicy": "workflowsFromSameOwner",
        },
        "active": False,
        "pinData": {},
        "meta": {"templateCredsSetupCompleted": False},
        "tags": tags,
    }


def write(fname, wf):
    path = os.path.join(WORKFLOW_DIR, fname)
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        json.dump(wf, fh, indent=2, ensure_ascii=True)
        fh.write("\n")
    print("wrote", os.path.relpath(path, ROOT))


# ---------------------------------------------------------------------------
# 1. window-tracker
# ---------------------------------------------------------------------------
def build_window_tracker():
    parse_js = """// Parse a WhatsApp Cloud API inbound webhook payload.
// Meta batches entries; one inbound webhook can carry several messages.
// Reference: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages
const out = [];
for (const item of $input.all()) {
  const body = item.json.body || item.json;
  const entries = body.entry || [];
  for (const entry of entries) {
    for (const change of (entry.changes || [])) {
      const value = (change || {}).value || {};
      const meta = value.metadata || {};
      const contacts = value.contacts || [];
      for (const msg of (value.messages || [])) {
        const profile = contacts.find(function (c) { return c.wa_id === msg.from; }) || {};
        out.push({
          json: {
            wa_id: msg.from,
            business_phone_id: meta.phone_number_id || '',
            display_name: (profile.profile || {}).name || null,
            wamid: msg.id,
            message_type: msg.type,
            text: (msg.text || {}).body || '',
            // Meta sends a unix timestamp in seconds as a string.
            inbound_at: new Date(Number(msg.timestamp) * 1000).toISOString()
          }
        });
      }
      // A call also opens/resets the customer service window. Verified:
      // https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
      for (const call of (value.calls || [])) {
        out.push({
          json: {
            wa_id: call.from,
            business_phone_id: meta.phone_number_id || '',
            display_name: null,
            wamid: call.id || null,
            message_type: 'call',
            text: '',
            inbound_at: new Date(Number(call.timestamp || Date.now() / 1000) * 1000).toISOString()
          }
        });
      }
    }
  }
}
if (out.length === 0) {
  // Status callbacks and other fields land here. Not an error, just nothing to do.
  return [];
}
return out;
"""

    upsert_sql = """-- Reset the 24h customer service window on every inbound message or call.
INSERT INTO wa.contacts (wa_id, business_phone_id, display_name, last_inbound_at)
VALUES ('{{ $json.wa_id }}', '{{ $json.business_phone_id }}',
        NULLIF('{{ $json.display_name }}', 'null'), '{{ $json.inbound_at }}'::timestamptz)
ON CONFLICT (business_phone_id, wa_id) DO UPDATE
SET last_inbound_at = GREATEST(wa.contacts.last_inbound_at, EXCLUDED.last_inbound_at),
    display_name    = COALESCE(EXCLUDED.display_name, wa.contacts.display_name),
    updated_at      = now()
RETURNING id AS contact_id, wa_id, business_phone_id, last_inbound_at;"""

    log_sql = """INSERT INTO wa.message_log
  (contact_id, wa_id, business_phone_id, direction, message_type, wamid, body_preview,
   gate_code, gate_reason, window_open, window_remaining_s)
VALUES
  ({{ $json.contact_id }}, '{{ $json.wa_id }}', '{{ $json.business_phone_id }}',
   'inbound', 'freeform', {{ $json.wamid ? "'" + $json.wamid + "'" : 'NULL' }},
   left({{ JSON.stringify($('Parse Inbound Payload').item.json.text) }}, 160),
   'INBOUND_WINDOW_RESET', 'Inbound message or call reset the 24h window',
   TRUE, 86400)
ON CONFLICT DO NOTHING;"""

    read_sql = """SELECT contact_id, wa_id, business_phone_id, last_inbound_at,
       window_open, window_remaining_s,
       opted_in, opted_in_at, marketing_opted_in, opted_out, opted_out_at
FROM wa.contact_gate_state
WHERE business_phone_id = '{{ $json.business_phone_id }}'
  AND wa_id = '{{ $json.wa_id }}';"""

    compute_js = """// Answer the question every other workflow asks this one:
// is the customer service window open, and for how much longer?
// The database view already computes it; we recompute here so the answer is
// identical whether the row exists or not, and so a caller that passes a
// synthetic 'now' (tests, replays) gets the same shape back.
const WINDOW_SECONDS = 86400; // https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
const results = [];
for (const item of $input.all()) {
  const row = item.json || {};
  const last = row.last_inbound_at ? Date.parse(row.last_inbound_at) : null;
  const now = Date.now();
  let elapsed = last === null ? null : Math.floor((now - last) / 1000);
  if (elapsed !== null && elapsed < 0) { elapsed = 0; }
  const open = elapsed !== null && elapsed < WINDOW_SECONDS;
  results.push({
    json: {
      wa_id: row.wa_id || null,
      business_phone_id: row.business_phone_id || null,
      known_contact: Boolean(row.contact_id),
      window_open: open,
      seconds_remaining: open ? (WINDOW_SECONDS - elapsed) : 0,
      seconds_since_last_inbound: elapsed,
      last_inbound_at: row.last_inbound_at || null,
      opted_in: row.opted_in === true,
      marketing_opted_in: row.marketing_opted_in === true,
      opted_out: row.opted_out === true,
      opted_out_at: row.opted_out_at || null,
      // If the window is closed, the only legal outbound is an approved template.
      template_required: !open
    }
  });
}
return results;
"""

    nodes = [
        sticky("wt-note", [-460, -160],
               "## window-tracker\\n\\nWrites the 24h customer service window state.\\n\\n"
               "Top lane: Meta inbound webhook -> reset window -> log.\\n"
               "Bottom lane: called by other workflows to READ window state.\\n\\n"
               "Window rule verified against\\n"
               "https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages",
               520, 300),
        node("wt-webhook", "Meta Inbound Webhook", "n8n-nodes-base.webhook", 2, [140, -60], {
            "httpMethod": "POST",
            "path": "wa/inbound",
            "responseMode": "lastNode",
            "options": {"rawBody": False},
        }, webhookId="11111111-1111-4111-8111-111111111111"),
        code_node("wt-parse", "Parse Inbound Payload", [360, -60], parse_js),
        pg_node("wt-upsert", "Upsert Contact And Reset Window", [580, -60], upsert_sql),
        pg_node("wt-log", "Log Inbound Message", [800, -60], log_sql),
        noop("wt-done", "Inbound Recorded", [1020, -60]),
        node("wt-sub", "Called By Another Workflow", "n8n-nodes-base.executeWorkflowTrigger", 1.1,
             [140, 200], {"inputSource": "workflowInputs",
                          "workflowInputs": {"values": [
                              {"name": "wa_id", "type": "string"},
                              {"name": "business_phone_id", "type": "string"}]}}),
        pg_node("wt-read", "Read Window State", [360, 200], read_sql),
        code_node("wt-compute", "Compute Window State", [580, 200], compute_js),
        noop("wt-return", "Return Window State", [800, 200]),
    ]

    connections = conn([
        ("Meta Inbound Webhook", 0, "Parse Inbound Payload", "main"),
        ("Parse Inbound Payload", 0, "Upsert Contact And Reset Window", "main"),
        ("Upsert Contact And Reset Window", 0, "Log Inbound Message", "main"),
        ("Log Inbound Message", 0, "Inbound Recorded", "main"),
        ("Called By Another Workflow", 0, "Read Window State", "main"),
        ("Read Window State", 0, "Compute Window State", "main"),
        ("Compute Window State", 0, "Return Window State", "main"),
    ])
    return workflow("window-tracker", nodes, connections,
                    [{"name": "whatsapp-compliance"}])


# ---------------------------------------------------------------------------
# 2. message-router  (the core gate)
# ---------------------------------------------------------------------------
def build_message_router():
    load_sql = """-- One read: window state, consent, counters, breaker.
SELECT
  g.contact_id, g.wa_id, g.business_phone_id, g.last_inbound_at,
  g.window_open, g.window_remaining_s,
  g.opted_in, g.opted_in_at, g.marketing_opted_in, g.opted_out, g.opted_out_at,
  COALESCE(s.sent_24h, 0)            AS sent_24h,
  COALESCE(s.sent_burst_window, 0)   AS sent_burst_window,
  COALESCE(b.open, FALSE)            AS breaker_open,
  b.reason                           AS breaker_reason
FROM wa.contact_gate_state g
LEFT JOIN wa.send_counters   s ON s.business_phone_id = g.business_phone_id
LEFT JOIN wa.circuit_breaker b ON b.business_phone_id = g.business_phone_id
WHERE g.business_phone_id = '{{ $json.business_phone_id }}'
  AND g.wa_id = '{{ $json.wa_id }}';"""

    gate_js = (
        "// ===========================================================\n"
        "// COMPLIANCE GATE - generated from logic/gate.js by\n"
        "// tools/build_workflows.py. Do not edit here; edit logic/gate.js\n"
        "// and re-run the generator. tests/parity.test.js enforces this.\n"
        "// ===========================================================\n"
        + CORE +
        """
// ------------------------- n8n adapter -------------------------
const request = $('Send Request In').first().json;
const state = $input.first().json || {};
const decision = evaluateSend({
  now: new Date().toISOString(),
  lastInboundAt: state.last_inbound_at || null,
  optedOut: state.opted_out === true,
  optedOutAt: state.opted_out_at || null,
  optInAt: state.opted_in === true ? (state.opted_in_at || null) : null,
  marketingOptIn: state.marketing_opted_in === true,
  messageType: request.message_type === 'template' ? 'template' : 'freeform',
  templateName: request.template_name || null,
  templateStatus: request.template_status || null,
  templateCategory: request.template_category || null,
  sent24h: Number(state.sent_24h || 0),
  dailyCap: Number(request.daily_cap || 0),
  sentBurstWindow: Number(state.sent_burst_window || 0),
  burstCap: Number(request.burst_cap || 0),
  circuitBreakerOpen: state.breaker_open === true,
  circuitBreakerReason: state.breaker_reason || null
});

return [{
  json: {
    wa_id: request.wa_id,
    business_phone_id: request.business_phone_id,
    contact_id: state.contact_id || null,
    body: request.body || '',
    template_name: request.template_name || null,
    template_category: request.template_category || null,
    template_language: request.template_language || 'en_US',
    template_components: request.template_components || [],
    allow: decision.allow,
    action: decision.action,
    gate_code: decision.code,
    gate_reason: decision.reason,
    requires_template: decision.requiresTemplate === true,
    window_open: decision.window.open,
    window_remaining_s: decision.window.secondsRemaining,
    message_type: decision.messageType,
    decided_at: new Date().toISOString()
  }
}];
""")

    freeform_body = ("={\n"
                     '  "messaging_product": "whatsapp",\n'
                     '  "recipient_type": "individual",\n'
                     '  "to": "{{ $json.wa_id }}",\n'
                     '  "type": "text",\n'
                     '  "text": { "preview_url": false, "body": {{ JSON.stringify($json.body) }} }\n'
                     "}")
    template_body = ("={\n"
                     '  "messaging_product": "whatsapp",\n'
                     '  "recipient_type": "individual",\n'
                     '  "to": "{{ $json.wa_id }}",\n'
                     '  "type": "template",\n'
                     '  "template": {\n'
                     '    "name": "{{ $json.template_name }}",\n'
                     '    "language": { "code": "{{ $json.template_language }}" },\n'
                     '    "components": {{ JSON.stringify($json.template_components) }}\n'
                     "  }\n"
                     "}")

    log_out_sql = """INSERT INTO wa.message_log
  (contact_id, wa_id, business_phone_id, direction, message_type, template_name,
   template_category, wamid, body_preview, gate_code, gate_reason, window_open,
   window_remaining_s)
VALUES
  ({{ $('Compliance Gate').item.json.contact_id || 'NULL' }},
   '{{ $('Compliance Gate').item.json.wa_id }}',
   '{{ $('Compliance Gate').item.json.business_phone_id }}',
   'outbound',
   '{{ $('Compliance Gate').item.json.message_type }}',
   {{ $('Compliance Gate').item.json.template_name ? "'" + $('Compliance Gate').item.json.template_name + "'" : 'NULL' }},
   {{ $('Compliance Gate').item.json.template_category ? "'" + $('Compliance Gate').item.json.template_category + "'" : 'NULL' }},
   {{ $json.body && $json.body.messages && $json.body.messages[0] ? "'" + $json.body.messages[0].id + "'" : 'NULL' }},
   left({{ JSON.stringify($('Compliance Gate').item.json.body) }}, 160),
   '{{ $('Compliance Gate').item.json.gate_code }}',
   {{ JSON.stringify($('Compliance Gate').item.json.gate_reason) }},
   {{ $('Compliance Gate').item.json.window_open }},
   {{ $('Compliance Gate').item.json.window_remaining_s }});"""

    log_refusal_sql = """-- A refusal is a row. Never a silent drop.
INSERT INTO wa.message_log
  (contact_id, wa_id, business_phone_id, direction, message_type, template_name,
   template_category, wamid, body_preview, gate_code, gate_reason, window_open,
   window_remaining_s)
VALUES
  ({{ $json.contact_id || 'NULL' }}, '{{ $json.wa_id }}', '{{ $json.business_phone_id }}',
   'refused', 'none',
   {{ $json.template_name ? "'" + $json.template_name + "'" : 'NULL' }},
   {{ $json.template_category ? "'" + $json.template_category + "'" : 'NULL' }},
   NULL,
   left({{ JSON.stringify($json.body) }}, 160),
   '{{ $json.gate_code }}', {{ JSON.stringify($json.gate_reason) }},
   {{ $json.window_open }}, {{ $json.window_remaining_s }});"""

    raise_js = """// Make the refusal impossible to ignore.
// The n8n execution is marked as failed so it shows red in the executions list
// and any error workflow you have configured fires. If you would rather the
// caller handle refusals as data, set STOP_HARD to false - but keep the log
// node above it either way.
const STOP_HARD = true;
const item = $input.first().json;
const message = 'WhatsApp send REFUSED [' + item.gate_code + '] to ' + item.wa_id + ': ' + item.gate_reason;
if (STOP_HARD) {
  throw new Error(message);
}
return [{ json: Object.assign({ refused: true, message: message }, item) }];
"""

    nodes = [
        sticky("mr-note", [-500, -260],
               "## message-router - THE GATE\\n\\n"
               "Every outbound message goes through here. Nothing calls the Cloud API directly.\\n\\n"
               "Window open  -> free-form service message allowed.\\n"
               "Window closed -> approved template only, otherwise REFUSE and log.\\n\\n"
               "Verified: 'When the window closes, you can only send pre-approved template messages.'\\n"
               "https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages",
               560, 320),
        node("mr-in", "Send Request In", "n8n-nodes-base.executeWorkflowTrigger", 1.1, [140, 60], {
            "inputSource": "workflowInputs",
            "workflowInputs": {"values": [
                {"name": "wa_id", "type": "string"},
                {"name": "business_phone_id", "type": "string"},
                {"name": "message_type", "type": "string"},
                {"name": "body", "type": "string"},
                {"name": "template_name", "type": "string"},
                {"name": "template_category", "type": "string"},
                {"name": "template_language", "type": "string"},
                {"name": "template_status", "type": "string"},
                {"name": "daily_cap", "type": "number"},
                {"name": "burst_cap", "type": "number"},
            ]},
        }),
        pg_node("mr-load", "Load Gate State", [360, 60], load_sql),
        code_node("mr-gate", "Compliance Gate", [580, 60], gate_js),
        if_node("mr-allow", "Allowed?", [800, 60], "={{ $json.allow }}", "true"),
        if_node("mr-istpl", "Template Send?", [1020, -60],
                "={{ $json.action }}", "equals", "send_template", "string"),
        http_node("mr-send-tpl", "Send Template Message", [1240, -160],
                  GRAPH + "/{{ $json.business_phone_id }}/messages", template_body),
        http_node("mr-send-free", "Send Free-form Message", [1240, 40],
                  GRAPH + "/{{ $json.business_phone_id }}/messages", freeform_body),
        pg_node("mr-log-out", "Log Outbound", [1480, -60], log_out_sql),
        noop("mr-sent", "Sent", [1700, -60]),
        pg_node("mr-log-ref", "Log Refusal", [1020, 240], log_refusal_sql),
        code_node("mr-raise", "Refusal Is Loud", [1240, 240], raise_js),
    ]

    connections = conn([
        ("Send Request In", 0, "Load Gate State", "main"),
        ("Load Gate State", 0, "Compliance Gate", "main"),
        ("Compliance Gate", 0, "Allowed?", "main"),
        ("Allowed?", 0, "Template Send?", "main"),
        ("Allowed?", 1, "Log Refusal", "main"),
        ("Template Send?", 0, "Send Template Message", "main"),
        ("Template Send?", 1, "Send Free-form Message", "main"),
        ("Send Template Message", 0, "Log Outbound", "main"),
        ("Send Free-form Message", 0, "Log Outbound", "main"),
        ("Log Outbound", 0, "Sent", "main"),
        ("Log Refusal", 0, "Refusal Is Loud", "main"),
    ])
    return workflow("message-router", nodes, connections,
                    [{"name": "whatsapp-compliance"}])


# ---------------------------------------------------------------------------
# 3. optin-optout
# ---------------------------------------------------------------------------
def build_optin_optout():
    normalize_js = """// Normalize an opt-in captured anywhere (web form, IVR, paper, CRM import)
// into one ledger event. Meta requires the opt-in to name the business and to
// state that the person is opting in to receive messages from it.
// https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in
const results = [];
for (const item of $input.all()) {
  const b = item.json.body || item.json;
  const waId = String(b.wa_id || '').replace(/[^0-9]/g, '');
  if (!waId) { throw new Error('opt-in event has no wa_id'); }
  const source = String(b.source || '').trim();
  if (!source) { throw new Error('opt-in event for ' + waId + ' has no source - consent without a source is not proof'); }
  const businessName = String(b.business_name || '').trim();
  if (!businessName) { throw new Error('opt-in event for ' + waId + ' does not name the business'); }
  const event = b.event === 'opt_out' ? 'opt_out' : 'opt_in';
  const scope = ['all', 'marketing', 'utility', 'authentication'].indexOf(b.scope) >= 0 ? b.scope : 'all';
  results.push({
    json: {
      wa_id: waId,
      business_phone_id: String(b.business_phone_id || ''),
      event: event,
      scope: scope,
      source: source,
      source_detail: JSON.stringify(b.source_detail || {}),
      consent_text: b.consent_text || null,
      business_name: businessName,
      occurred_at: b.occurred_at || new Date().toISOString(),
      marketing: event === 'opt_in' && (scope === 'all' || scope === 'marketing')
    }
  });
}
return results;
"""

    ledger_sql = """-- Append-only. The database trigger rejects UPDATE and DELETE here.
WITH c AS (
  INSERT INTO wa.contacts (wa_id, business_phone_id)
  VALUES ('{{ $json.wa_id }}', '{{ $json.business_phone_id }}')
  ON CONFLICT (business_phone_id, wa_id) DO UPDATE SET updated_at = now()
  RETURNING id
)
INSERT INTO wa.opt_in_ledger
  (contact_id, wa_id, event, scope, source, source_detail, consent_text, business_name, occurred_at)
SELECT c.id, '{{ $json.wa_id }}', '{{ $json.event }}', '{{ $json.scope }}',
       '{{ $json.source }}', '{{ $json.source_detail }}'::jsonb,
       {{ $json.consent_text ? JSON.stringify($json.consent_text) : 'NULL' }},
       '{{ $json.business_name }}', '{{ $json.occurred_at }}'::timestamptz
FROM c
RETURNING id AS ledger_id, contact_id, wa_id, event, scope, occurred_at;"""

    sync_sql = """-- Cache the ledger's verdict on wa.contacts.
-- Opt-out wins and is irreversible until a NEW opt_in row is appended.
UPDATE wa.contacts SET
  opted_in           = CASE WHEN '{{ $json.event }}' = 'opt_in'  THEN TRUE  ELSE opted_in END,
  opted_in_at        = CASE WHEN '{{ $json.event }}' = 'opt_in'  THEN '{{ $json.occurred_at }}'::timestamptz ELSE opted_in_at END,
  marketing_opted_in = CASE
                         WHEN '{{ $json.event }}' = 'opt_out' THEN FALSE
                         WHEN '{{ $json.event }}' = 'opt_in' AND '{{ $json.scope }}' IN ('all','marketing') THEN TRUE
                         ELSE marketing_opted_in END,
  opted_out          = CASE WHEN '{{ $json.event }}' = 'opt_out' THEN TRUE ELSE FALSE END,
  opted_out_at       = CASE WHEN '{{ $json.event }}' = 'opt_out' THEN '{{ $json.occurred_at }}'::timestamptz ELSE NULL END,
  updated_at         = now()
WHERE id = {{ $json.contact_id }}
RETURNING id AS contact_id, wa_id, opted_in, marketing_opted_in, opted_out, opted_out_at;"""

    keyword_js = (
        "// Detect an opt-out (or an explicit re-opt-in) in an inbound message.\n"
        "// Generated from logic/gate.js core - see tools/build_workflows.py.\n"
        + CORE +
        """
// ------------------------- n8n adapter -------------------------
// Whole-message match only, so "stop by the shop tomorrow" is not an opt-out.
// UNVERIFIED: WhatsApp does not publish a mandated keyword list. This is our
// convention for "provide clear instructions for how people can opt out, and
// honor these requests".
// https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in
const REOPT_IN = ['start', 'unstop', 'subscribe', 'yes'];
const results = [];
for (const item of $input.all()) {
  const row = item.json || {};
  const text = row.text || '';
  const normalized = normalizeText(text);
  let event = null;
  if (isOptOutMessage(text)) {
    event = 'opt_out';
  } else if (REOPT_IN.indexOf(normalized) >= 0) {
    event = 'opt_in';
  }
  results.push({
    json: {
      wa_id: row.wa_id,
      business_phone_id: row.business_phone_id,
      text: text,
      consent_event: event,
      is_consent_event: event !== null,
      event: event || 'none',
      scope: 'all',
      source: 'whatsapp_keyword',
      source_detail: JSON.stringify({ wamid: row.wamid || null, matched_text: normalized }),
      consent_text: text,
      business_name: row.business_name || 'REPLACE_WITH_YOUR_BUSINESS_NAME',
      occurred_at: row.inbound_at || new Date().toISOString()
    }
  });
}
return results;
""")

    nodes = [
        sticky("oo-note", [-520, -220],
               "## optin-optout\\n\\n"
               "Top lane: consent captured off-platform (web form, IVR, paper) -> ledger.\\n"
               "Bottom lane: STOP / UNSUBSCRIBE typed into WhatsApp -> immediate opt-out.\\n\\n"
               "The ledger is append-only and is your proof of consent.\\n"
               "Opt-in requirements verified against\\n"
               "https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in\\n\\n"
               "STOP keyword list is this kit's own convention - Meta publishes no mandated list.",
               560, 340),
        node("oo-webhook", "Consent Capture Webhook", "n8n-nodes-base.webhook", 2, [140, -40], {
            "httpMethod": "POST",
            "path": "wa/consent",
            "responseMode": "lastNode",
            "options": {},
        }, webhookId="22222222-2222-4222-8222-222222222222"),
        code_node("oo-normalize", "Normalize Consent Event", [360, -40], normalize_js),
        pg_node("oo-ledger", "Append To Opt-In Ledger", [580, -40], ledger_sql),
        pg_node("oo-sync", "Sync Contact Consent State", [800, -40], sync_sql),
        noop("oo-done", "Consent Recorded", [1020, -40]),
        node("oo-sub", "Inbound Message In", "n8n-nodes-base.executeWorkflowTrigger", 1.1,
             [140, 240], {"inputSource": "workflowInputs",
                          "workflowInputs": {"values": [
                              {"name": "wa_id", "type": "string"},
                              {"name": "business_phone_id", "type": "string"},
                              {"name": "text", "type": "string"},
                              {"name": "wamid", "type": "string"},
                              {"name": "inbound_at", "type": "string"}]}}),
        code_node("oo-keyword", "Detect Consent Keyword", [360, 240], keyword_js),
        if_node("oo-is", "Consent Keyword?", [580, 240], "={{ $json.is_consent_event }}", "true"),
        pg_node("oo-ledger2", "Append Keyword Event To Ledger", [800, 160], ledger_sql),
        pg_node("oo-sync2", "Apply Consent Change Now", [1020, 160], sync_sql),
        noop("oo-applied", "Consent Change Applied", [1240, 160]),
        noop("oo-nochange", "No Consent Change", [800, 340]),
    ]

    connections = conn([
        ("Consent Capture Webhook", 0, "Normalize Consent Event", "main"),
        ("Normalize Consent Event", 0, "Append To Opt-In Ledger", "main"),
        ("Append To Opt-In Ledger", 0, "Sync Contact Consent State", "main"),
        ("Sync Contact Consent State", 0, "Consent Recorded", "main"),
        ("Inbound Message In", 0, "Detect Consent Keyword", "main"),
        ("Detect Consent Keyword", 0, "Consent Keyword?", "main"),
        ("Consent Keyword?", 0, "Append Keyword Event To Ledger", "main"),
        ("Consent Keyword?", 1, "No Consent Change", "main"),
        ("Append Keyword Event To Ledger", 0, "Apply Consent Change Now", "main"),
        ("Apply Consent Change Now", 0, "Consent Change Applied", "main"),
    ])
    return workflow("optin-optout", nodes, connections, [{"name": "whatsapp-compliance"}])


# ---------------------------------------------------------------------------
# 4. rate-limiter
# ---------------------------------------------------------------------------
def build_rate_limiter():
    counters_sql = """SELECT
  '{{ $json.business_phone_id }}'::text AS business_phone_id,
  COUNT(*) FILTER (WHERE created_at > now() - INTERVAL '24 hours')                       AS sent_24h,
  COUNT(DISTINCT wa_id) FILTER (WHERE created_at > now() - INTERVAL '24 hours')          AS unique_recipients_24h,
  COUNT(*) FILTER (WHERE created_at > now() - INTERVAL '60 seconds')                     AS sent_burst_window,
  COUNT(*) FILTER (WHERE wa_id = '{{ $json.wa_id }}'
                     AND created_at > now() - INTERVAL '15 seconds')                     AS sent_to_contact_recent
FROM wa.message_log
WHERE direction = 'outbound'
  AND business_phone_id = '{{ $json.business_phone_id }}';"""

    caps_js = """// Per-number send caps.
//
// VERIFIED: Meta's messaging limits are tiers of UNIQUE customers you may start
// a conversation with per rolling 24 hours: 250 (new portfolios), 2K, 10K,
// 100K, unlimited. Replies inside an open customer service window are not what
// the tier counts, but they still shape how spammy your number looks.
// https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits
//
// UNVERIFIED: the burst numbers below. Meta documents throughput but this kit
// does not encode a published per-second figure. Treat them as self-imposed.
const TIERS = { 'TIER_250': 250, 'TIER_1K': 1000, 'TIER_2K': 2000, 'TIER_10K': 10000, 'TIER_100K': 100000, 'UNLIMITED': Number.MAX_SAFE_INTEGER };

const cfg = $('Rate Check In').first().json;
const counts = $input.first().json || {};

const tierName = String(cfg.tier || 'TIER_250').toUpperCase();
const tierCap = TIERS[tierName] !== undefined ? TIERS[tierName] : 250;
const selfUniqueCap = Number(cfg.self_imposed_daily_unique_recipients || 0) || tierCap;
const uniqueCap = Math.min(tierCap, selfUniqueCap);
const totalCap = Number(cfg.self_imposed_daily_total_messages || 0);
const burstCap = Number(cfg.burst_max_messages_per_window || 20);
const perContactGap = Number(cfg.min_seconds_between_messages_per_contact || 15);

const uniqueUsed = Number(counts.unique_recipients_24h || 0);
const totalUsed = Number(counts.sent_24h || 0);
const burstUsed = Number(counts.sent_burst_window || 0);
const contactRecent = Number(counts.sent_to_contact_recent || 0);

const breaches = [];
if (uniqueUsed >= uniqueCap) {
  breaches.push({ code: 'TIER_UNIQUE_CAP', detail: uniqueUsed + '/' + uniqueCap + ' unique recipients in 24h' });
}
if (totalCap > 0 && totalUsed >= totalCap) {
  breaches.push({ code: 'DAILY_CAP_REACHED', detail: totalUsed + '/' + totalCap + ' messages in 24h' });
}
if (burstCap > 0 && burstUsed >= burstCap) {
  breaches.push({ code: 'BURST_CAP_REACHED', detail: burstUsed + '/' + burstCap + ' in the burst window' });
}
if (perContactGap > 0 && contactRecent > 0) {
  breaches.push({ code: 'PER_CONTACT_COOLDOWN', detail: 'sent to this contact within the last ' + perContactGap + 's' });
}

return [{
  json: {
    business_phone_id: cfg.business_phone_id,
    wa_id: cfg.wa_id,
    allow: breaches.length === 0,
    gate_code: breaches.length === 0 ? 'OK_RATE' : breaches[0].code,
    gate_reason: breaches.length === 0
      ? 'Within caps (' + uniqueUsed + '/' + uniqueCap + ' unique, ' + burstUsed + '/' + burstCap + ' burst).'
      : breaches.map(function (b) { return b.code + ': ' + b.detail; }).join('; '),
    tier: tierName,
    tier_cap: tierCap,
    unique_cap: uniqueCap,
    unique_used: uniqueUsed,
    unique_headroom: Math.max(0, uniqueCap - uniqueUsed),
    total_used: totalUsed,
    burst_used: burstUsed,
    breaches: breaches
  }
}];
"""

    headroom_sql = """SELECT business_phone_id,
       COUNT(DISTINCT wa_id) FILTER (WHERE created_at > now() - INTERVAL '24 hours') AS unique_recipients_24h,
       COUNT(*) FILTER (WHERE created_at > now() - INTERVAL '24 hours')              AS sent_24h,
       COUNT(*) FILTER (WHERE direction = 'refused'
                          AND created_at > now() - INTERVAL '24 hours')              AS refusals_24h
FROM wa.message_log
GROUP BY business_phone_id;"""

    trend_js = """// Daily audit: how close is each number to its cap, and how many sends did
// the gates refuse? A refusal spike usually means someone is trying to blast.
const REPORT_UNIQUE_CAP = 250; // set to your granted tier
const WARN_AT = 0.8;
const out = [];
for (const item of $input.all()) {
  const r = item.json || {};
  const used = Number(r.unique_recipients_24h || 0);
  const ratio = REPORT_UNIQUE_CAP > 0 ? used / REPORT_UNIQUE_CAP : 0;
  out.push({
    json: {
      business_phone_id: r.business_phone_id,
      unique_recipients_24h: used,
      sent_24h: Number(r.sent_24h || 0),
      refusals_24h: Number(r.refusals_24h || 0),
      cap: REPORT_UNIQUE_CAP,
      utilization: Math.round(ratio * 100) / 100,
      needs_attention: ratio >= WARN_AT || Number(r.refusals_24h || 0) > 0
    }
  });
}
return out;
"""

    nodes = [
        sticky("rl-note", [-520, -200],
               "## rate-limiter\\n\\n"
               "Top lane: called before every send. Caps = min(Meta tier, your own cap).\\n"
               "Bottom lane: daily audit of headroom and refusal spikes.\\n\\n"
               "Tiers verified: 250 / 2K / 10K / 100K / unlimited unique customers per 24h.\\n"
               "https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits\\n\\n"
               "Burst numbers are self-imposed, NOT quoted from Meta.",
               560, 300),
        node("rl-in", "Rate Check In", "n8n-nodes-base.executeWorkflowTrigger", 1.1, [140, -20], {
            "inputSource": "workflowInputs",
            "workflowInputs": {"values": [
                {"name": "wa_id", "type": "string"},
                {"name": "business_phone_id", "type": "string"},
                {"name": "tier", "type": "string"},
                {"name": "self_imposed_daily_unique_recipients", "type": "number"},
                {"name": "self_imposed_daily_total_messages", "type": "number"},
                {"name": "burst_max_messages_per_window", "type": "number"},
                {"name": "min_seconds_between_messages_per_contact", "type": "number"}]},
        }),
        pg_node("rl-count", "Count Recent Sends", [360, -20], counters_sql),
        code_node("rl-caps", "Apply Caps", [580, -20], caps_js),
        if_node("rl-if", "Within Caps?", [800, -20], "={{ $json.allow }}", "true"),
        noop("rl-ok", "Rate OK", [1020, -120]),
        pg_node("rl-log", "Log Rate Refusal", [1020, 80], """INSERT INTO wa.message_log
  (wa_id, business_phone_id, direction, message_type, gate_code, gate_reason)
VALUES ('{{ $json.wa_id }}', '{{ $json.business_phone_id }}', 'refused', 'none',
        '{{ $json.gate_code }}', {{ JSON.stringify($json.gate_reason) }});"""),
        node("rl-cron", "Daily Headroom Audit", "n8n-nodes-base.scheduleTrigger", 1.2, [140, 300], {
            "rule": {"interval": [{"field": "hours", "hoursInterval": 24, "triggerAtHour": 9}]}
        }),
        pg_node("rl-headroom", "Read 24h Counters", [360, 300], headroom_sql),
        code_node("rl-trend", "Score Headroom", [580, 300], trend_js),
        if_node("rl-attn", "Needs Attention?", [800, 300], "={{ $json.needs_attention }}", "true"),
        noop("rl-alert", "Raise Headroom Alert", [1020, 300]),
    ]

    connections = conn([
        ("Rate Check In", 0, "Count Recent Sends", "main"),
        ("Count Recent Sends", 0, "Apply Caps", "main"),
        ("Apply Caps", 0, "Within Caps?", "main"),
        ("Within Caps?", 0, "Rate OK", "main"),
        ("Within Caps?", 1, "Log Rate Refusal", "main"),
        ("Daily Headroom Audit", 0, "Read 24h Counters", "main"),
        ("Read 24h Counters", 0, "Score Headroom", "main"),
        ("Score Headroom", 0, "Needs Attention?", "main"),
        ("Needs Attention?", 0, "Raise Headroom Alert", "main"),
    ])
    return workflow("rate-limiter", nodes, connections, [{"name": "whatsapp-compliance"}])


# ---------------------------------------------------------------------------
# 5. quality-monitor
# ---------------------------------------------------------------------------
def build_quality_monitor():
    classify_js = """// Ingest Meta's account and quality webhooks and decide whether to trip the
// circuit breaker.
//
// VERIFIED webhook fields (subscribe to these on your app):
//   phone_number_quality_update, message_template_quality_update,
//   message_template_status_update, account_update, account_alerts,
//   business_capability_update
//   https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview
//
// PARTLY UNVERIFIED: the developer docs show quality_score values GREEN, NA and
// UNKNOWN in examples and describe a "Flagged" status that lowers your limit.
// YELLOW and RED are widely reported but are NOT quoted from the developer docs
// cited here, so the mapping below is this kit's conservative convention.
const TRIP_SCORES = ['RED', 'YELLOW', 'FLAGGED'];
const TRIP_EVENTS = ['FLAGGED', 'DISABLED', 'RESTRICTED', 'ACCOUNT_VIOLATION', 'ACCOUNT_RESTRICTION', 'DOWNGRADE'];

const out = [];
for (const item of $input.all()) {
  const body = item.json.body || item.json;
  for (const entry of (body.entry || [])) {
    for (const change of (entry.changes || [])) {
      const field = change.field || 'unknown';
      const value = change.value || {};
      const score = String(
        (value.current_limit && value.event === 'FLAGGED' ? 'FLAGGED' : '') ||
        (value.quality_score && (value.quality_score.score || value.quality_score)) ||
        value.new_quality_score || ''
      ).toUpperCase();
      const event = String(value.event || value.decision || '').toUpperCase();
      const templateName = value.message_template_name || value.name || null;
      const templateStatus = String(value.new_template_status || value.event || '').toUpperCase();

      const reasons = [];
      if (score && TRIP_SCORES.indexOf(score) >= 0) {
        reasons.push('quality score ' + score + ' on ' + field);
      }
      if (event && TRIP_EVENTS.indexOf(event) >= 0) {
        reasons.push('account event ' + event + ' on ' + field);
      }
      if (field === 'message_template_status_update' && templateStatus === 'PAUSED') {
        reasons.push('template ' + (templateName || 'unknown') + ' PAUSED for negative feedback');
      }
      if (field === 'message_template_status_update' && templateStatus === 'REJECTED') {
        reasons.push('template ' + (templateName || 'unknown') + ' REJECTED');
      }

      out.push({
        json: {
          business_phone_id: String(value.display_phone_number_id || value.phone_number_id ||
                                    (value.metadata || {}).phone_number_id || entry.id || ''),
          field: field,
          event: event || null,
          current_limit: value.current_limit || null,
          quality_score: score || null,
          template_name: templateName,
          raw: JSON.stringify(value),
          should_trip: reasons.length > 0,
          trip_reason: reasons.join('; ') || null,
          occurred_at: new Date().toISOString()
        }
      });
    }
  }
}
if (out.length === 0) { return []; }
return out;
"""

    insert_sql = """INSERT INTO wa.quality_events
  (business_phone_id, field, event, current_limit, quality_score, template_name, raw, occurred_at)
VALUES
  ('{{ $json.business_phone_id }}', '{{ $json.field }}',
   {{ $json.event ? "'" + $json.event + "'" : 'NULL' }},
   {{ $json.current_limit ? "'" + $json.current_limit + "'" : 'NULL' }},
   {{ $json.quality_score ? "'" + $json.quality_score + "'" : 'NULL' }},
   {{ $json.template_name ? "'" + $json.template_name + "'" : 'NULL' }},
   '{{ $json.raw }}'::jsonb, '{{ $json.occurred_at }}'::timestamptz)
RETURNING id;"""

    trip_sql = """-- Halt ALL outbound for this number. Never auto-clears: a human sets
-- cleared_at / cleared_by after fixing the underlying cause.
INSERT INTO wa.circuit_breaker (business_phone_id, open, reason, opened_at, updated_at)
VALUES ('{{ $('Classify Quality Event').item.json.business_phone_id }}', TRUE,
        {{ JSON.stringify($('Classify Quality Event').item.json.trip_reason) }}, now(), now())
ON CONFLICT (business_phone_id) DO UPDATE
SET open = TRUE,
    reason = EXCLUDED.reason,
    opened_at = COALESCE(wa.circuit_breaker.opened_at, now()),
    cleared_at = NULL,
    cleared_by = NULL,
    updated_at = now()
RETURNING business_phone_id, open, reason, opened_at;"""

    sweep_sql = """-- Undelivered ratio: a spike is an early warning that your number is being
-- throttled or blocked, before Meta's own webhook says anything.
SELECT business_phone_id,
       COUNT(*)                                                   AS sent,
       COUNT(*) FILTER (WHERE wamid IS NULL)                      AS no_wamid,
       COUNT(*) FILTER (WHERE direction = 'refused')              AS refusals
FROM wa.message_log
WHERE created_at > now() - INTERVAL '6 hours'
  AND direction IN ('outbound', 'refused')
GROUP BY business_phone_id;"""

    sweep_js = """// Trend sweep. Trips the breaker on a bad undelivered ratio even when Meta has
// sent no quality webhook - webhook availability is not guaranteed and is the
// single biggest blind spot in this kit. See README Limitations.
const MIN_SAMPLE = 40;
const THRESHOLD = 0.25;
const out = [];
for (const item of $input.all()) {
  const r = item.json || {};
  const sent = Number(r.sent || 0);
  const bad = Number(r.no_wamid || 0);
  const ratio = sent > 0 ? bad / sent : 0;
  const trip = sent >= MIN_SAMPLE && ratio >= THRESHOLD;
  out.push({
    json: {
      business_phone_id: r.business_phone_id,
      sent: sent,
      undelivered: bad,
      refusals: Number(r.refusals || 0),
      undelivered_ratio: Math.round(ratio * 1000) / 1000,
      should_trip: trip,
      trip_reason: trip
        ? ('undelivered ratio ' + Math.round(ratio * 100) + '% over ' + sent + ' sends in 6h')
        : null
    }
  });
}
return out;
"""

    sweep_trip_sql = """INSERT INTO wa.circuit_breaker (business_phone_id, open, reason, opened_at, updated_at)
VALUES ('{{ $json.business_phone_id }}', TRUE,
        {{ JSON.stringify($json.trip_reason) }}, now(), now())
ON CONFLICT (business_phone_id) DO UPDATE
SET open = TRUE, reason = EXCLUDED.reason,
    opened_at = COALESCE(wa.circuit_breaker.opened_at, now()),
    cleared_at = NULL, cleared_by = NULL, updated_at = now()
RETURNING business_phone_id, open, reason;"""

    nodes = [
        sticky("qm-note", [-540, -220],
               "## quality-monitor\\n\\n"
               "Top lane: Meta account/quality webhooks -> quality_events -> trip breaker.\\n"
               "Bottom lane: 6-hourly undelivered-ratio sweep, in case no webhook arrives.\\n\\n"
               "Subscribe your app to: phone_number_quality_update,\\n"
               "message_template_quality_update, message_template_status_update,\\n"
               "account_update, account_alerts, business_capability_update.\\n"
               "https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview\\n\\n"
               "The breaker NEVER auto-clears. A human clears it.",
               580, 360),
        node("qm-webhook", "Meta Account Webhook", "n8n-nodes-base.webhook", 2, [140, -40], {
            "httpMethod": "POST",
            "path": "wa/quality",
            "responseMode": "lastNode",
            "options": {},
        }, webhookId="33333333-3333-4333-8333-333333333333"),
        code_node("qm-classify", "Classify Quality Event", [360, -40], classify_js),
        pg_node("qm-insert", "Record Quality Event", [580, -40], insert_sql),
        if_node("qm-if", "Trip Breaker?", [800, -40],
                "={{ $('Classify Quality Event').item.json.should_trip }}", "true"),
        pg_node("qm-trip", "Open Circuit Breaker", [1020, -140], trip_sql),
        noop("qm-halted", "Outbound Halted", [1240, -140]),
        noop("qm-ok", "Logged Only", [1020, 60]),
        node("qm-cron", "Undelivered Ratio Sweep", "n8n-nodes-base.scheduleTrigger", 1.2, [140, 300], {
            "rule": {"interval": [{"field": "hours", "hoursInterval": 6}]}
        }),
        pg_node("qm-sweep", "Read Delivery Stats", [360, 300], sweep_sql),
        code_node("qm-eval", "Evaluate Delivery Trend", [580, 300], sweep_js),
        if_node("qm-if2", "Trend Bad?", [800, 300], "={{ $json.should_trip }}", "true"),
        pg_node("qm-trip2", "Open Breaker On Trend", [1020, 300], sweep_trip_sql),
        noop("qm-clean", "Trend Clean", [1020, 440]),
    ]

    connections = conn([
        ("Meta Account Webhook", 0, "Classify Quality Event", "main"),
        ("Classify Quality Event", 0, "Record Quality Event", "main"),
        ("Record Quality Event", 0, "Trip Breaker?", "main"),
        ("Trip Breaker?", 0, "Open Circuit Breaker", "main"),
        ("Trip Breaker?", 1, "Logged Only", "main"),
        ("Open Circuit Breaker", 0, "Outbound Halted", "main"),
        ("Undelivered Ratio Sweep", 0, "Read Delivery Stats", "main"),
        ("Read Delivery Stats", 0, "Evaluate Delivery Trend", "main"),
        ("Evaluate Delivery Trend", 0, "Trend Bad?", "main"),
        ("Trend Bad?", 0, "Open Breaker On Trend", "main"),
        ("Trend Bad?", 1, "Trend Clean", "main"),
    ])
    return workflow("quality-monitor", nodes, connections, [{"name": "whatsapp-compliance"}])


# ---------------------------------------------------------------------------
# 6. example-agent
# ---------------------------------------------------------------------------
def build_example_agent():
    parse_js = """// Same parser as window-tracker, kept local so the example runs standalone.
const out = [];
for (const item of $input.all()) {
  const body = item.json.body || item.json;
  for (const entry of (body.entry || [])) {
    for (const change of (entry.changes || [])) {
      const value = (change || {}).value || {};
      const meta = value.metadata || {};
      for (const msg of (value.messages || [])) {
        if (msg.type !== 'text') { continue; }
        out.push({
          json: {
            wa_id: msg.from,
            business_phone_id: meta.phone_number_id || '',
            wamid: msg.id,
            text: (msg.text || {}).body || '',
            inbound_at: new Date(Number(msg.timestamp) * 1000).toISOString()
          }
        });
      }
    }
  }
}
return out;
"""

    prep_js = (
        "// Build the send request for message-router. This node deliberately does NOT\n"
        "// decide anything - it only shapes the request. The gate lives in\n"
        "// message-router, and the same core is embedded here so this workflow can\n"
        "// pre-flight the decision and tell the model whether it must pick a template.\n"
        "// Generated from logic/gate.js by tools/build_workflows.py.\n"
        + CORE +
        """
// ------------------------- n8n adapter -------------------------
const inbound = $('Parse Inbound').first().json;
const windowInfo = $('Get Window State').first().json || {};
const reply = String(($input.first().json || {}).output || '').trim();

// Pre-flight with the same logic the router will apply. If this says the window
// is closed, the agent must not send its free-form reply - it has to fall back
// to an approved template (or to nothing at all).
const preflight = evaluateSend({
  now: new Date().toISOString(),
  lastInboundAt: windowInfo.last_inbound_at || inbound.inbound_at,
  optedOut: windowInfo.opted_out === true,
  optInAt: windowInfo.opted_in === true ? (windowInfo.opted_in_at || null) : null,
  messageType: 'freeform',
  sent24h: 0,
  dailyCap: 0,
  sentBurstWindow: 0,
  burstCap: 0
});

const useTemplate = preflight.requiresTemplate === true;

return [{
  json: {
    wa_id: inbound.wa_id,
    business_phone_id: inbound.business_phone_id,
    message_type: useTemplate ? 'template' : 'freeform',
    body: reply,
    // Replace with a template you have actually had approved.
    template_name: useTemplate ? 'appointment_reminder_v3' : null,
    template_category: useTemplate ? 'UTILITY' : null,
    template_language: 'en_US',
    template_status: useTemplate ? 'APPROVED' : null,
    template_components: [],
    daily_cap: 800,
    burst_cap: 20,
    preflight_code: preflight.code,
    preflight_reason: preflight.reason
  }
}];
""")

    system_prompt = (
        "You are the support assistant for Example Widgets Ltd on WhatsApp.\n\n"
        "Hard rules:\n"
        "- You are replying to someone who messaged us first. You never start conversations.\n"
        "- Never promise a discount, refund or delivery date you were not given in context.\n"
        "- If the customer asks to stop hearing from us, acknowledge it in one line and "
        "say nothing else; the opt-out is already recorded by the system.\n"
        "- Keep replies under 600 characters. Plain text. No markdown.\n"
        "- If you do not know something, say so and offer to have a human follow up.\n\n"
        "You have no ability to send anything yourself. Your text goes through a compliance "
        "gate that may refuse it. That is expected and is not an error."
    )

    nodes = [
        sticky("ea-note", [-560, -260],
               "## example-agent\\n\\n"
               "A realistic responder wired through all five gates.\\n\\n"
               "1 window-tracker records the inbound and resets the 24h window\\n"
               "2 optin-optout checks for STOP / START before anything else\\n"
               "3 the AI drafts a reply (it can NEVER send)\\n"
               "4 rate-limiter checks caps\\n"
               "5 message-router applies the gate and is the only thing that calls Meta\\n\\n"
               "This is a REPLY bot. It has no bulk send path, on purpose.",
               580, 340),
        node("ea-webhook", "WhatsApp Inbound", "n8n-nodes-base.webhook", 2, [120, 60], {
            "httpMethod": "POST",
            "path": "wa/agent",
            "responseMode": "lastNode",
            "options": {},
        }, webhookId="44444444-4444-4444-8444-444444444444"),
        code_node("ea-parse", "Parse Inbound", [340, 60], parse_js),
        node("ea-window", "Record Inbound (window-tracker)", "n8n-nodes-base.executeWorkflow", 1.2,
             [560, 60], {
                 "workflowId": {"__rl": True, "value": "REPLACE_WITH_WINDOW_TRACKER_WORKFLOW_ID",
                                "mode": "id"},
                 "workflowInputs": {"mappingMode": "defineBelow", "value": {
                     "wa_id": "={{ $json.wa_id }}",
                     "business_phone_id": "={{ $json.business_phone_id }}"}},
                 "options": {"waitForSubWorkflow": True},
             }),
        node("ea-consent", "Consent Keyword Check (optin-optout)", "n8n-nodes-base.executeWorkflow",
             1.2, [780, 60], {
                 "workflowId": {"__rl": True, "value": "REPLACE_WITH_OPTIN_OPTOUT_WORKFLOW_ID",
                                "mode": "id"},
                 "workflowInputs": {"mappingMode": "defineBelow", "value": {
                     "wa_id": "={{ $('Parse Inbound').item.json.wa_id }}",
                     "business_phone_id": "={{ $('Parse Inbound').item.json.business_phone_id }}",
                     "text": "={{ $('Parse Inbound').item.json.text }}",
                     "wamid": "={{ $('Parse Inbound').item.json.wamid }}",
                     "inbound_at": "={{ $('Parse Inbound').item.json.inbound_at }}"}},
                 "options": {"waitForSubWorkflow": True},
             }),
        node("ea-getwindow", "Get Window State", "n8n-nodes-base.executeWorkflow", 1.2, [1000, 60], {
            "workflowId": {"__rl": True, "value": "REPLACE_WITH_WINDOW_TRACKER_WORKFLOW_ID",
                           "mode": "id"},
            "workflowInputs": {"mappingMode": "defineBelow", "value": {
                "wa_id": "={{ $('Parse Inbound').item.json.wa_id }}",
                "business_phone_id": "={{ $('Parse Inbound').item.json.business_phone_id }}"}},
            "options": {"waitForSubWorkflow": True},
        }),
        if_node("ea-optedout", "Opted Out Or Consent Event?", [1220, 60],
                "={{ $json.opted_out === true }}", "true"),
        noop("ea-stop", "Stop - Do Not Reply", [1440, -60]),
        node("ea-agent", "AI Support Agent", "@n8n/n8n-nodes-langchain.agent", 1.7, [1440, 200], {
            "promptType": "define",
            "text": "={{ $('Parse Inbound').item.json.text }}",
            "options": {"systemMessage": system_prompt},
        }),
        node("ea-model", "OpenAI Chat Model", "@n8n/n8n-nodes-langchain.lmChatOpenAi", 1.2,
             [1400, 400], {"model": {"__rl": True, "value": "gpt-4o-mini", "mode": "list"},
                           "options": {"temperature": 0.2}},
             credentials={"openAiApi": {"id": "REPLACE_WITH_YOUR_OPENAI_CRED_ID",
                                        "name": "OpenAI account"}}),
        node("ea-memory", "Conversation Memory", "@n8n/n8n-nodes-langchain.memoryBufferWindow", 1.3,
             [1560, 400], {"sessionIdType": "customKey",
                           "sessionKey": "={{ $('Parse Inbound').item.json.wa_id }}",
                           "contextWindowLength": 10}),
        code_node("ea-prep", "Prepare Send Request", [1660, 200], prep_js),
        node("ea-rate", "Rate Check (rate-limiter)", "n8n-nodes-base.executeWorkflow", 1.2,
             [1880, 200], {
                 "workflowId": {"__rl": True, "value": "REPLACE_WITH_RATE_LIMITER_WORKFLOW_ID",
                                "mode": "id"},
                 "workflowInputs": {"mappingMode": "defineBelow", "value": {
                     "wa_id": "={{ $json.wa_id }}",
                     "business_phone_id": "={{ $json.business_phone_id }}",
                     "tier": "TIER_250",
                     "self_imposed_daily_unique_recipients": "={{ 150 }}",
                     "self_imposed_daily_total_messages": "={{ 800 }}",
                     "burst_max_messages_per_window": "={{ 20 }}",
                     "min_seconds_between_messages_per_contact": "={{ 15 }}"}},
                 "options": {"waitForSubWorkflow": True},
             }),
        if_node("ea-rateok", "Rate OK?", [2100, 200], "={{ $json.allow }}", "true"),
        node("ea-router", "Send (message-router)", "n8n-nodes-base.executeWorkflow", 1.2,
             [2320, 120], {
                 "workflowId": {"__rl": True, "value": "REPLACE_WITH_MESSAGE_ROUTER_WORKFLOW_ID",
                                "mode": "id"},
                 "workflowInputs": {"mappingMode": "defineBelow", "value": {
                     "wa_id": "={{ $('Prepare Send Request').item.json.wa_id }}",
                     "business_phone_id": "={{ $('Prepare Send Request').item.json.business_phone_id }}",
                     "message_type": "={{ $('Prepare Send Request').item.json.message_type }}",
                     "body": "={{ $('Prepare Send Request').item.json.body }}",
                     "template_name": "={{ $('Prepare Send Request').item.json.template_name }}",
                     "template_category": "={{ $('Prepare Send Request').item.json.template_category }}",
                     "template_language": "={{ $('Prepare Send Request').item.json.template_language }}",
                     "template_status": "={{ $('Prepare Send Request').item.json.template_status }}",
                     "daily_cap": "={{ 800 }}",
                     "burst_cap": "={{ 20 }}"}},
                 "options": {"waitForSubWorkflow": True},
             }),
        noop("ea-sent", "Reply Delivered To Gate", [2540, 120]),
        noop("ea-ratestop", "Held By Rate Limiter", [2320, 320]),
    ]

    connections = conn([
        ("WhatsApp Inbound", 0, "Parse Inbound", "main"),
        ("Parse Inbound", 0, "Record Inbound (window-tracker)", "main"),
        ("Record Inbound (window-tracker)", 0, "Consent Keyword Check (optin-optout)", "main"),
        ("Consent Keyword Check (optin-optout)", 0, "Get Window State", "main"),
        ("Get Window State", 0, "Opted Out Or Consent Event?", "main"),
        ("Opted Out Or Consent Event?", 0, "Stop - Do Not Reply", "main"),
        ("Opted Out Or Consent Event?", 1, "AI Support Agent", "main"),
        ("OpenAI Chat Model", 0, "AI Support Agent", "ai_languageModel"),
        ("Conversation Memory", 0, "AI Support Agent", "ai_memory"),
        ("AI Support Agent", 0, "Prepare Send Request", "main"),
        ("Prepare Send Request", 0, "Rate Check (rate-limiter)", "main"),
        ("Rate Check (rate-limiter)", 0, "Rate OK?", "main"),
        ("Rate OK?", 0, "Send (message-router)", "main"),
        ("Rate OK?", 1, "Held By Rate Limiter", "main"),
        ("Send (message-router)", 0, "Reply Delivered To Gate", "main"),
    ])
    return workflow("example-agent", nodes, connections, [{"name": "whatsapp-compliance"}])


def main():
    if not os.path.isdir(WORKFLOW_DIR):
        os.makedirs(WORKFLOW_DIR)
    write("window-tracker.json", build_window_tracker())
    write("message-router.json", build_message_router())
    write("optin-optout.json", build_optin_optout())
    write("rate-limiter.json", build_rate_limiter())
    write("quality-monitor.json", build_quality_monitor())
    write("example-agent.json", build_example_agent())


if __name__ == "__main__":
    main()
