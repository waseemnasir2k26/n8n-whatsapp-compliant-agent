# Changelog

All notable changes to this project are documented in this file.

## [2026.10] - 2026-10-12

- October 2026 review of n8n-whatsapp-compliant-agent, a public MIT n8n starter kit for a WhatsApp Business API AI agent with five Postgres-backed compliance gates (24-hour window, opt-in/out, rate limits, quality circuit breaker) and a worked example agent.
- Status: v1 from September 2026; package version bumped 1.1.0 to 1.2.0, documentation and metadata only. No workflow JSON, gate logic, SQL or tests changed.
- Reviewed this month: all six Meta developer documentation links the README cites, plus Calendly and skynetjoe.com, still resolve. The cited policy rules were not re-read against Meta's current text in this pass, so the stated verification date stands. A Work with SkynetLabs section was added.
- Known gaps (from the README Limitations): no guarantee against bans; cited rules will go stale; quality monitoring depends on webhook delivery; gates assume a single sender and database; example-agent has no tools, escalation or PII redaction. No CI workflow, so tests run only by hand.

## [2026.09] - 2026-09-16

- Maintenance review of n8n-whatsapp-compliant-agent — an n8n starter kit for a WhatsApp Business API AI agent in which the compliance guardrails are the product: the 24-hour customer service window, opt-in/opt-out, send caps and a quality circuit breaker, all enforced in Postgres before a message leaves the number.
- Contents: five gate workflows plus a worked responder in `workflows/` (`window-tracker`, `message-router`, `optin-optout`, `rate-limiter`, `quality-monitor`, `example-agent`), shared gate logic in `logic/gate.js`, `sql/schema.sql` (Postgres 14+, `wa` schema: contacts, opt_in_ledger, message_log, quality_events, circuit_breaker), `config/limits.example.yaml`, a workflow builder in `tools/`, and tests wired to `npm test` (node --test plus a Python workflow validator and a parity test). MIT licensed.
- Status: v1, committed September 2026 in a single commit. Every policy rule in the README is quoted and linked to Meta's own developer documentation, with a stated verification date.
- Reviewed September 2026: docs refreshed, CHANGELOG started, package version bumped, versioned as v2026.09. No workflow JSON, gate logic, SQL or tests changed.
- Known gaps (from the README's own Limitations section): the kit reduces ban risk but guarantees nothing, since Meta enforces on signals you cannot see; the cited policy rules will go stale and need re-verifying; quality monitoring depends on webhook delivery, with a local undelivered-ratio sweep only as a backstop; the gates assume one sender and one database, and break if anything else writes to `wa.contacts` or sends on the same number; `example-agent.json` has no tools, no escalation path and no PII redaction. Repo-level: no CI workflow, so the tests run only by hand, and no CHANGELOG before this release.
