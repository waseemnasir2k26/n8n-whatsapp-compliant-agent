# n8n-whatsapp-compliant-agent

An n8n starter kit for a WhatsApp Business API AI agent where the compliance guardrails **are** the product: the 24-hour customer service window, opt-in and opt-out, send caps and a quality circuit breaker, all enforced in Postgres before a single message leaves your number.

## The ban problem

Almost every WhatsApp automation tutorial shows you how to send a message. Almost none of them stop you from sending the message that gets your number blocked.

The rules that actually cost people their accounts, from Meta's own docs:

- **Free-form messages outside the 24-hour window are not allowed.** "When a WhatsApp user messages you or calls you, a 24-hour timer called a customer service window starts... When the window closes, you can only send pre-approved template messages." — [Send messages, Meta for Developers](https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages)
- **Users reporting and blocking you is the thing that ends accounts.** "We might also limit or offboard your business from WhatsApp if your account receives excessive negative feedback from users." Enforcement escalates from a warning, to a "1 or 3 day block on sending marketing, utility, and authentication template messages", to a "5, 7, or 30 day block on sending any messages", to "an account lock, which is an indefinite block on sending any messages; can only be removed via an appeal", to being "permanently disabled from the WhatsApp Business Platform". — [Policy and spam enforcement](https://developers.facebook.com/documentation/business-messaging/whatsapp/policy-enforcement)
- **You need opt-in before you message anyone.** "Businesses must clearly state that a person is opting in to receive communication from the business" and "must clearly state the business's name that a person is opting in to receive messages from", and must "provide clear instructions for how people can opt out of receiving specific categories of messages, and honor these requests." — [Get opt-in for WhatsApp](https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in)
- **Your send volume is capped by tier, and a bad quality rating pulls the cap down.** Tiers are counted in unique customers per rolling 24 hours: 250 (default for new portfolios), 2,000, 10,000, 100,000, unlimited. — [Messaging limits](https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits)

This kit encodes those rules as five gates every outbound message has to pass, plus a worked AI agent that is wired through all of them.

![gates](https://img.shields.io/badge/gates-5-1f6feb) ![tests](https://img.shields.io/badge/tests-39%20passing-2ea043) ![workflows](https://img.shields.io/badge/n8n%20workflows-6-ff6d5a) ![license](https://img.shields.io/badge/license-MIT-555)

## Quick start

1. **Create the database.** `psql "$DATABASE_URL" -f sql/schema.sql` (Postgres 14+). This creates the `wa` schema: `contacts`, `opt_in_ledger`, `message_log`, `quality_events`, `circuit_breaker`.
2. **Configure limits.** `cp config/limits.example.yaml config/limits.yaml`, then set your `phone_number_id`, your granted messaging tier, and your approved template names. Every value in the example file is invented — replace all of them.
3. **Import the workflows.** In n8n, import all six files in `workflows/`. Attach a Postgres credential and an HTTP Header Auth credential (`Authorization: Bearer <your Cloud API token>`) where the nodes say `REPLACE_WITH_...`, and paste each sub-workflow's id into the `Execute Workflow` nodes in `example-agent.json`.
4. **Point Meta at the webhooks.** Subscribe your app to the `messages` field on `/wa/inbound` and to `phone_number_quality_update`, `message_template_quality_update`, `message_template_status_update`, `account_update`, `account_alerts` and `business_capability_update` on `/wa/quality`.
5. **Verify the gates before going live.** `npm test` (or `python tests/validate_workflows.py && node --test "tests/*.test.js"`), then send yourself a message, wait 24 hours, and confirm the router refuses a free-form reply.

## The five gates

| Gate              | What it prevents                                                                                                                                                                                                                        | What you configure                                                                                                        |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `window-tracker`  | Losing track of when the 24-hour customer service window opened, so you can't tell whether a free-form reply is legal. Resets on every inbound message **and call**.                                                                    | Nothing. It reads and writes `wa.contacts.last_inbound_at`. Point the Meta `messages` webhook at it.                      |
| `message-router`  | The suspension case: a free-form message sent after the window closed. Window open means free-form is fine; window closed means an approved template or a logged, loud refusal.                                                         | Your Cloud API credential and `phone_number_id`. Nothing else may call the Graph API.                                     |
| `optin-optout`    | Messaging someone who never consented, or who told you to stop. Append-only consent ledger with source and timestamp; STOP/UNSUBSCRIBE in several languages takes effect immediately and is irreversible without a new explicit opt-in. | Your business name (Meta requires the opt-in to name it), extra stop keywords for your markets, the confirmation message. |
| `rate-limiter`    | Blowing through your messaging tier or firing a burst that looks like a blast. Caps at the **lower** of Meta's tier and your own self-imposed cap.                                                                                      | Your granted tier, self-imposed daily unique-recipient and total caps, burst window, per-contact cooldown.                |
| `quality-monitor` | Sending straight through a quality collapse. Ingests Meta's quality/account webhooks, trends undelivered ratio as a backstop, and trips a circuit breaker that halts **all** outbound until a human clears it.                          | Which quality scores and account events trip the breaker, the undelivered-ratio threshold.                                |

`workflows/example-agent.json` is a realistic AI responder wired through all five, so you can see the pattern rather than reassemble it.

## How a message flows

```
             inbound from a WhatsApp user
                          |
                          v
              +-----------------------+
              |   window-tracker      |  last_inbound_at = now()   -> 24h window (re)opens
              +-----------------------+
                          |
                          v
              +-----------------------+
              |    optin-optout       |  "STOP"? -> ledger opt_out, contact blocked, stop here
              +-----------------------+
                          |
                          v
              +-----------------------+
              |      AI agent         |  drafts a reply. It can never send.
              +-----------------------+
                          |
                          v
              +-----------------------+
              |    rate-limiter       |  min(Meta tier, your cap) + burst + per-contact cooldown
              +-----------------------+
                          |
                          v
    +--------------------------------------------------+
    |                message-router                    |
    |                                                  |
    |  breaker open? ------------------> REFUSE  CIRCUIT_BREAKER_OPEN
    |  opted out? ---------------------> REFUSE  OPTED_OUT
    |  over cap? ----------------------> REFUSE  DAILY_CAP_REACHED / BURST_CAP_REACHED
    |                                                  |
    |  window OPEN  --> free-form  ----> SEND    OK_WINDOW_OPEN
    |  window CLOSED                                   |
    |     no opt-in ------------------> REFUSE  NO_OPT_IN
    |     free-form ------------------> REFUSE  TEMPLATE_REQUIRED
    |     template not APPROVED ------> REFUSE  TEMPLATE_NOT_APPROVED
    |     MARKETING w/o consent ------> REFUSE  NO_MARKETING_OPT_IN
    |     approved template ---------> SEND    OK_TEMPLATE_OUT_OF_WINDOW
    +--------------------------------------------------+
             |                              |
             v                              v
       Graph API /messages          wa.message_log (direction='refused')
                                    + the n8n execution fails, loudly
```

Every refusal is a row in `wa.message_log` with a code, a reason and the window state at the time. A gate that drops messages silently is a gate you cannot audit.

## The 24-hour window explained

Meta calls it the **customer service window**:

> "When a WhatsApp user messages you or calls you, a 24-hour timer called a customer service window starts. If the user messages or calls you again before the timer expires, the timer resets to 24 hours."
> — [Send messages](https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages)

While it is open you may send free-form service messages. When it closes:

> "When the window closes, you can only send pre-approved template messages."
> — [Send messages](https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages)

Three details this kit implements deliberately:

- **A call resets the window too**, not just a text message. `window-tracker` handles both.
- **Exactly 24 hours elapsed counts as CLOSED.** The boundary is treated conservatively so a second of clock skew between your server and Meta's cannot produce an out-of-window free-form send. There is a unit test for exactly this edge.
- **Templates must be APPROVED, and the categories are `AUTHENTICATION`, `MARKETING` and `UTILITY`.** "Service" is not a template category — service messages are the free-form messages you send while the window is open. Templates can also be **paused**: "The template has been paused due to recurring negative feedback from customers, or low read-rates." — [Template fundamentals](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview)

## Consent and opt-out

`wa.opt_in_ledger` is append-only — a database trigger rejects `UPDATE` and `DELETE` on it. Every row carries the event (`opt_in` / `opt_out`), the scope, the **source** (`website_form`, `whatsapp_keyword`, `ivr`, `paper`, `crm_import`), a JSONB `source_detail` for the form URL or message id, the exact `consent_text` the person agreed to, the business name they agreed to hear from, and when it happened. The boolean flags on `wa.contacts` are a cache of that ledger, not the truth.

Why the ledger rather than a flag: Meta requires that you obtained opt-in and that you honor opt-outs. A boolean column tells you the current state. It cannot tell you _when_ consent was given, _where_, or _what wording_ the person saw — which is exactly what you are asked for when an account gets flagged or a regulator asks. Consent state lives in Postgres and never in a prompt; an LLM must never be the system of record for whether you are allowed to message someone.

Opt-out behaviour:

- STOP-style keywords are matched **whole-message** and normalized, so "stop by tomorrow" is not an opt-out but "Stop!" is. The list covers English, Spanish, French, German, Portuguese, Indonesian and Italian.
- An opt-out takes effect on the same execution that detected it — before any reply is drafted.
- It is **irreversible** without a new explicit `opt_in` row appended to the ledger (`START`, `SUBSCRIBE`, or an off-platform re-consent posted to the consent webhook). There is no admin toggle that quietly un-opts-out someone.
- Opt-out beats everything, including an open window and an approved template.

**This kit is for replying to and following up with people who contacted you.** There is deliberately no bulk-send path, no contact-list loop, and no "blast to everyone who opted in" workflow — a test asserts that none of the shipped workflows contains one. If you want cold outreach, this is the wrong repo.

## Not affiliated — verify current policy yourself

**This project is not affiliated with, endorsed by, or connected to Meta Platforms, Inc. or WhatsApp.** "WhatsApp" and "Meta" are trademarks of their respective owners.

**WhatsApp platform policy changes frequently.** Every rule encoded here was verified against Meta's developer documentation on **2026-09-01**, and every rule is cited to the page it came from so you can check it yourself. Before you rely on this in production, re-read:

- https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
- https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview
- https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in
- https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits
- https://developers.facebook.com/documentation/business-messaging/whatsapp/policy-enforcement
- https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview

Things this repo could **not** verify from Meta's developer docs, marked as unverified in the code as well:

| Unverified                                                                                                                                                                                                                                      | Where it appears                                | What we did instead                                                                                                    |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| A mandated STOP keyword list. WhatsApp does not publish one the way SMS carriers do.                                                                                                                                                            | `logic/gate.js` `STOP_KEYWORDS`                 | Shipped our own deliberately broad multi-language list. A false opt-out is cheaper than an ignored one.                |
| Quality rating values `YELLOW` and `RED`. The developer docs show `GREEN`, `NA` and `UNKNOWN` in API examples and describe a `Flagged` status; YELLOW/RED are widely reported but come from the Business Help Center, not the pages cited here. | `config/limits.example.yaml`, `quality-monitor` | Trip the breaker on any of RED / YELLOW / FLAGGED, and store Meta's raw string so you can audit what actually arrived. |
| A published messages-per-second throughput number.                                                                                                                                                                                              | `config/limits.example.yaml` burst section      | Conservative self-imposed burst caps, clearly labelled as ours.                                                        |
| An explicit requirement for a _separate_ marketing consent flag. Meta requires opt-in and requires honoring opt-out "for specific categories of messages".                                                                                      | `logic/gate.js` `NO_MARKETING_OPT_IN`           | Enforced a distinct marketing consent flag as the safe reading.                                                        |
| Whether Meta requires you to _retain_ proof of consent, and for how long. The opt-in page states the requirements without a retention rule.                                                                                                     | `sql/schema.sql` `opt_in_ledger`                | Kept an append-only ledger anyway. Your local law probably requires it even if Meta does not.                          |

## Limitations

Read this part.

- **This reduces risk. It does not guarantee you will not be banned.** Meta enforces on signals you cannot see, including how many recipients block or report you. You can be perfectly inside the 24-hour window and still get flagged for sending things people do not want.
- **Policy changes.** Every rule here has a citation and a verification date. Both will go stale. Re-verify before you deploy and again when Meta ships a changelog entry.
- **Quality signals depend on webhook availability.** `quality-monitor` can only react to what Meta actually sends you, and webhook delivery is not guaranteed. That is why the workflow also runs a local undelivered-ratio sweep every 6 hours as a backstop — but a local heuristic is not the same as knowing your true quality rating.
- **The gates are only as good as the data.** If something writes to `wa.contacts` outside these workflows, or a second system sends on the same number without going through `message-router`, the counters and window state are wrong and the gates will happily approve something they should not. One sender, one database.
- **Rate limiting is per phone number in your own database.** It cannot see sends made by other tools on the same number, and it does not know your real current tier unless you set it in `config/limits.yaml`.
- **No legal advice.** GDPR, TCPA, LSSI-CE, PDPA and friends have their own consent rules that are stricter than Meta's in places. The ledger gives you the evidence; it does not tell you whether your consent flow is lawful where you operate.
- **The AI agent is an example, not a product.** It has no tools, no escalation path and no PII redaction. Treat `example-agent.json` as the wiring diagram, not the deployment.

## Contributing

Issues and PRs welcome, with one rule specific to this repo: **anything that touches a policy rule must cite Meta's documentation by URL in the PR description**, and anything that cannot be cited must be marked unverified in both the code comment and the README table above. A confidently wrong compliance rule is worse than no rule.

Before opening a PR:

```bash
python tools/build_workflows.py      # regenerate workflows if you changed logic/gate.js
python tests/validate_workflows.py   # structure, ASCII jsCode, no leaked secrets
node --test "tests/*.test.js"        # gate unit tests + embedded-copy parity
```

The decision logic lives in `logic/gate.js` and is copied verbatim into the n8n Code nodes by `tools/build_workflows.py`. Never hand-edit the copy inside a workflow JSON — `tests/parity.test.js` runs both versions across a 7,680-case matrix and fails if they disagree by even one field.

---

Built by **SkynetLabs (Waseem Nasir)** — we build compliant WhatsApp and multi-channel agents for clients.
https://skynetjoe.com · https://calendly.com/skynetlabs/schedule-a-free-consultation

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Waseem Nasir / SkynetLabs.
