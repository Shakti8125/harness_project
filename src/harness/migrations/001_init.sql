-- src/harness/migrations/001_init.sql   (applied in order, tracked in schema_version)
-- Transcribed from PLAN.md Phase 3 "Schema". One addition, recorded in
-- docs/progress/phase-3/dispatch.md decision 4: approval.context_json.
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS failure_signature (
  signature_id      TEXT PRIMARY KEY,       -- sha256(scope_key)[:32]
  scope             TEXT NOT NULL,          -- caller-defined partition, e.g. one repository
  subject_key       TEXT NOT NULL,          -- integration-defined
  fingerprint       TEXT NOT NULL,          -- normalized error fingerprint
  first_seen_at     TEXT NOT NULL,
  last_seen_at      TEXT NOT NULL,
  occurrences       INTEGER NOT NULL DEFAULT 0,
  verdict_counts    TEXT NOT NULL DEFAULT '{}',   -- JSON {verdict: n}
  last_verdict      TEXT,
  last_run_id       TEXT
);
CREATE INDEX IF NOT EXISTS ix_sig_scope_seen ON failure_signature(scope, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS observation (
  observation_id TEXT PRIMARY KEY,
  signature_id   TEXT NOT NULL REFERENCES failure_signature(signature_id),
  run_id         TEXT NOT NULL,
  occurred_at    TEXT NOT NULL,
  verdict        TEXT NOT NULL,
  confidence     REAL NOT NULL,
  action_taken   TEXT,
  action_outcome TEXT,             -- "passed_on_retry" | "failed_again" | "pending" | NULL
  commit_sha     TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_sig_time ON observation(signature_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS run (
  run_id          TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  integration     TEXT NOT NULL,
  status          TEXT NOT NULL,
  attempt         INTEGER NOT NULL DEFAULT 1,
  superseded_run_id TEXT,
  created_at      TEXT NOT NULL,
  heartbeat_at    TEXT,
  completed_at    TEXT,
  outcome_json    TEXT
);

CREATE TABLE IF NOT EXISTS trace_span (
  span_id TEXT PRIMARY KEY, parent_span_id TEXT, run_id TEXT NOT NULL,
  name TEXT NOT NULL, component TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, duration_ms INTEGER,
  attributes_json TEXT NOT NULL DEFAULT '{}', error_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_span_run ON trace_span(run_id, started_at);

CREATE TABLE IF NOT EXISTS approval (
  approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL,
  plan_json TEXT NOT NULL, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  decided_at TEXT, decided_by TEXT, decision_note TEXT,
  context_json TEXT NOT NULL DEFAULT '{}'   -- what the deciding request needs to rebuild the gateway
);

CREATE TABLE IF NOT EXISTS escalation (
  escalation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, reason TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
  channel TEXT NOT NULL, delivered_at TEXT, delivery_error TEXT
);
