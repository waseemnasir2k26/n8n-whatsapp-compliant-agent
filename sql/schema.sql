-- n8n-whatsapp-compliant-agent :: Postgres schema
--
-- Consent and window state live HERE, never in a prompt and never in workflow
-- static data. An LLM must never be the system of record for whether you are
-- allowed to message someone.
--
-- Tested against PostgreSQL 14+.
--   psql "$DATABASE_URL" -f sql/schema.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS wa;

-- ---------------------------------------------------------------------------
-- contacts : one row per WhatsApp user you have a relationship with.
-- last_inbound_at drives the 24h customer service window.
-- https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.contacts (
    id                  BIGSERIAL PRIMARY KEY,
    wa_id               TEXT        NOT NULL,   -- E.164 without '+', as Meta sends it
    business_phone_id   TEXT        NOT NULL,   -- your phone_number_id (one row per pair)
    display_name        TEXT,
    locale              TEXT,
    last_inbound_at     TIMESTAMPTZ,            -- NULL = they have never contacted you
    last_outbound_at    TIMESTAMPTZ,
    opted_in            BOOLEAN     NOT NULL DEFAULT FALSE,
    opted_in_at         TIMESTAMPTZ,
    marketing_opted_in  BOOLEAN     NOT NULL DEFAULT FALSE,
    opted_out           BOOLEAN     NOT NULL DEFAULT FALSE,
    opted_out_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT contacts_unique_pair UNIQUE (business_phone_id, wa_id),
    CONSTRAINT contacts_optout_has_time CHECK (opted_out = FALSE OR opted_out_at IS NOT NULL),
    CONSTRAINT contacts_optin_has_time  CHECK (opted_in  = FALSE OR opted_in_at  IS NOT NULL),
    -- Marketing consent cannot exist without base consent.
    CONSTRAINT contacts_marketing_needs_optin CHECK (marketing_opted_in = FALSE OR opted_in = TRUE)
);

CREATE INDEX IF NOT EXISTS contacts_wa_id_idx            ON wa.contacts (wa_id);
CREATE INDEX IF NOT EXISTS contacts_window_idx           ON wa.contacts (business_phone_id, last_inbound_at DESC);
CREATE INDEX IF NOT EXISTS contacts_sendable_idx         ON wa.contacts (business_phone_id) WHERE opted_out = FALSE;

-- ---------------------------------------------------------------------------
-- opt_in_ledger : append-only proof of consent. NEVER update or delete a row.
-- State on wa.contacts is a cache; this table is the evidence you show when
-- Meta or a regulator asks why you messaged someone.
-- https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.opt_in_ledger (
    id            BIGSERIAL PRIMARY KEY,
    contact_id    BIGINT      NOT NULL REFERENCES wa.contacts(id) ON DELETE RESTRICT,
    wa_id         TEXT        NOT NULL,
    event         TEXT        NOT NULL CHECK (event IN ('opt_in', 'opt_out')),
    scope         TEXT        NOT NULL DEFAULT 'all' CHECK (scope IN ('all', 'marketing', 'utility', 'authentication')),
    source        TEXT        NOT NULL,   -- 'website_form' | 'whatsapp_keyword' | 'ivr' | 'paper' | 'crm_import' | ...
    source_detail JSONB       NOT NULL DEFAULT '{}'::jsonb, -- form url, IP, agent id, message id, screenshot ref
    consent_text  TEXT,                    -- the exact wording the person agreed to
    business_name TEXT,                    -- Meta requires the opt-in to name the business
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS opt_in_ledger_contact_idx ON wa.opt_in_ledger (contact_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS opt_in_ledger_wa_id_idx   ON wa.opt_in_ledger (wa_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS opt_in_ledger_event_idx   ON wa.opt_in_ledger (event, occurred_at DESC);

-- Belt and braces: block UPDATE/DELETE on the ledger at the database level.
CREATE OR REPLACE FUNCTION wa.ledger_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'wa.opt_in_ledger is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS opt_in_ledger_no_mutate ON wa.opt_in_ledger;
CREATE TRIGGER opt_in_ledger_no_mutate
    BEFORE UPDATE OR DELETE ON wa.opt_in_ledger
    FOR EACH ROW EXECUTE FUNCTION wa.ledger_is_append_only();

-- ---------------------------------------------------------------------------
-- message_log : every inbound, every outbound, and every REFUSAL.
-- Refusals are rows too. A gate that blocks silently is a gate you cannot audit.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.message_log (
    id                BIGSERIAL PRIMARY KEY,
    contact_id        BIGINT      REFERENCES wa.contacts(id) ON DELETE SET NULL,
    wa_id             TEXT        NOT NULL,
    business_phone_id TEXT        NOT NULL,
    direction         TEXT        NOT NULL CHECK (direction IN ('inbound', 'outbound', 'refused')),
    message_type      TEXT        NOT NULL DEFAULT 'freeform' CHECK (message_type IN ('freeform', 'template', 'none')),
    template_name     TEXT,
    template_category TEXT        CHECK (template_category IS NULL OR template_category IN ('AUTHENTICATION', 'MARKETING', 'UTILITY')),
    wamid             TEXT,       -- Meta message id, NULL for refusals
    body_preview      TEXT,       -- truncate before storing; do not warehouse full PII
    gate_code         TEXT,       -- OK_WINDOW_OPEN | TEMPLATE_REQUIRED | OPTED_OUT | ...
    gate_reason       TEXT,
    window_open       BOOLEAN,
    window_remaining_s INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS message_log_contact_idx  ON wa.message_log (contact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS message_log_rate_idx     ON wa.message_log (business_phone_id, created_at DESC) WHERE direction = 'outbound';
CREATE INDEX IF NOT EXISTS message_log_refusals_idx ON wa.message_log (gate_code, created_at DESC) WHERE direction = 'refused';
CREATE UNIQUE INDEX IF NOT EXISTS message_log_wamid_idx ON wa.message_log (wamid) WHERE wamid IS NOT NULL;

-- ---------------------------------------------------------------------------
-- quality_events : what Meta tells you about how your sending is landing.
-- Fed by the phone_number_quality_update, message_template_quality_update,
-- message_template_status_update, account_update and account_alerts webhooks.
-- https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.quality_events (
    id                BIGSERIAL PRIMARY KEY,
    business_phone_id TEXT        NOT NULL,
    field             TEXT        NOT NULL,  -- the webhook field name, verbatim
    event             TEXT,                  -- e.g. FLAGGED, ONBOARDING, DOWNGRADE
    current_limit     TEXT,                  -- e.g. TIER_1K, TIER_10K (Meta's string, kept raw)
    quality_score     TEXT,                  -- GREEN | YELLOW | RED | NA | UNKNOWN (see README: partly unverified)
    template_name     TEXT,
    raw               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS quality_events_phone_idx ON wa.quality_events (business_phone_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS quality_events_field_idx ON wa.quality_events (field, occurred_at DESC);

-- ---------------------------------------------------------------------------
-- circuit_breaker : one row per business phone number. When open = TRUE the
-- router blocks every outbound message, no exceptions, until a human clears it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wa.circuit_breaker (
    business_phone_id TEXT        PRIMARY KEY,
    open              BOOLEAN     NOT NULL DEFAULT FALSE,
    reason            TEXT,
    opened_at         TIMESTAMPTZ,
    cleared_at        TIMESTAMPTZ,
    cleared_by        TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Helper views used by the workflows.
-- ---------------------------------------------------------------------------

-- Window + consent state in one read, so the router does one query.
CREATE OR REPLACE VIEW wa.contact_gate_state AS
SELECT
    c.id                AS contact_id,
    c.wa_id,
    c.business_phone_id,
    c.last_inbound_at,
    (c.last_inbound_at IS NOT NULL
        AND now() - c.last_inbound_at < INTERVAL '24 hours')          AS window_open,
    GREATEST(0, EXTRACT(EPOCH FROM (c.last_inbound_at + INTERVAL '24 hours' - now()))::INTEGER)
                                                                      AS window_remaining_s,
    c.opted_in,
    c.opted_in_at,
    c.marketing_opted_in,
    c.opted_out,
    c.opted_out_at
FROM wa.contacts c;

-- Rolling 24h and 60s outbound counts per business phone number.
CREATE OR REPLACE VIEW wa.send_counters AS
SELECT
    business_phone_id,
    COUNT(*) FILTER (WHERE created_at > now() - INTERVAL '24 hours')  AS sent_24h,
    COUNT(DISTINCT wa_id) FILTER (WHERE created_at > now() - INTERVAL '24 hours') AS unique_recipients_24h,
    COUNT(*) FILTER (WHERE created_at > now() - INTERVAL '60 seconds') AS sent_burst_window
FROM wa.message_log
WHERE direction = 'outbound'
GROUP BY business_phone_id;

COMMIT;
