-- BankVoiceAI — Database Schema (PostgreSQL 16)
-- Run on startup via docker-entrypoint-initdb.d

-- Sessions table
CREATE TABLE IF NOT EXISTS call_sessions (
    id              SERIAL PRIMARY KEY,
    session_id      VARCHAR(128) UNIQUE NOT NULL,
    caller_phone    VARCHAR(32),
    channel         VARCHAR(16) DEFAULT 'voice',
    bank_id         VARCHAR(128),
    current_agent   VARCHAR(64) DEFAULT 'customer_service',
    status          VARCHAR(32) DEFAULT 'active',
    start_time      TIMESTAMPTZ DEFAULT NOW(),
    end_time        TIMESTAMPTZ,
    duration_seconds INTEGER,
    end_reason      VARCHAR(64),
    escalated       BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Conversation turns (audit log — CFPB requires 7yr retention)
CREATE TABLE IF NOT EXISTS conversation_turns (
    id          BIGSERIAL PRIMARY KEY,
    session_id  VARCHAR(128) REFERENCES call_sessions(session_id),
    turn_number INTEGER NOT NULL,
    role        VARCHAR(16) NOT NULL,  -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    agent_name  VARCHAR(64),
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Compliance events log
CREATE TABLE IF NOT EXISTS compliance_events (
    id          BIGSERIAL PRIMARY KEY,
    session_id  VARCHAR(128),
    event_type  VARCHAR(64) NOT NULL,  -- 'mini_miranda', 'cease_desist', 'debt_dispute', etc.
    details     JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Metrics (aggregated daily)
CREATE TABLE IF NOT EXISTS daily_metrics (
    id                      SERIAL PRIMARY KEY,
    date                    DATE UNIQUE NOT NULL DEFAULT CURRENT_DATE,
    total_calls             INTEGER DEFAULT 0,
    voice_calls             INTEGER DEFAULT 0,
    whatsapp_messages       INTEGER DEFAULT 0,
    escalations             INTEGER DEFAULT 0,
    avg_handle_time_seconds FLOAT DEFAULT 0,
    first_call_resolution   FLOAT DEFAULT 0,
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_sessions_status ON call_sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON call_sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id);
CREATE INDEX IF NOT EXISTS idx_compliance_session ON compliance_events(session_id);

-- Row-level security for multi-tenant (future)
ALTER TABLE call_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_turns ENABLE ROW LEVEL SECURITY;
