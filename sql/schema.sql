CREATE TABLE IF NOT EXISTS conversations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    conversation_id VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL,
    content MEDIUMTEXT NOT NULL,
    intent VARCHAR(20) NULL,
    escalate INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT chk_conversations_role CHECK (role IN ('user', 'assistant', 'system')),
    CONSTRAINT chk_conversations_intent CHECK (intent IS NULL OR intent IN ('inquiry', 'lead', 'escalate')),
    INDEX idx_conversations_conversation_id (conversation_id, created_at)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS collected_data (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(255) NOT NULL,
    `key` VARCHAR(255) NOT NULL,
    `value` TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_collected_data_session_id (session_id)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS user_auth_codes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    code VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_user_auth_codes_email (email)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS session_summaries (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(255) NOT NULL UNIQUE,
    summary MEDIUMTEXT NOT NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS token_usage (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(255) NULL,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    model_name VARCHAR(255) NULL,
    duration_seconds DECIMAL(14, 4) DEFAULT 0.0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_token_usage_model_name (model_name)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS global_settings (
    id INT AUTO_INCREMENT PRIMARY KEY,
    setting_key VARCHAR(191) NOT NULL UNIQUE,
    setting_value TEXT NOT NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) CHARACTER SET utf8mb4;

-- ===== billing / usage (added 2026-11-25) =====
-- One row per model call (or per Live turn). Token columns are the raw counts, cost_usd is a snapshot taken at write time.
CREATE TABLE IF NOT EXISTS usage_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    session_id VARCHAR(255) NULL,
    call_id VARCHAR(64) NULL,
    channel VARCHAR(16) NOT NULL,
    purpose VARCHAR(32) NOT NULL,
    model VARCHAR(100) NOT NULL,
    input_text INT NOT NULL DEFAULT 0,
    input_audio INT NOT NULL DEFAULT 0,
    input_image INT NOT NULL DEFAULT 0,
    input_cached INT NOT NULL DEFAULT 0,
    output_text INT NOT NULL DEFAULT 0,
    output_audio INT NOT NULL DEFAULT 0,
    estimated TINYINT NOT NULL DEFAULT 0,
    cost_usd DECIMAL(14,8) NULL,
    price_note VARCHAR(255) NULL,
    is_test TINYINT NOT NULL DEFAULT 0,
    source VARCHAR(12) NOT NULL DEFAULT 'live',
    raw_usage TEXT NULL,
    INDEX idx_usage_created (created_at),
    INDEX idx_usage_call (call_id),
    INDEX idx_usage_session (session_id),
    INDEX idx_usage_channel (channel, purpose)
) CHARACTER SET utf8mb4;

-- One row per phone call (also failed / unanswered ones). talk_seconds = our measure (pickup -> hang-up),
-- cdr_billsec = Asterisk's own record (reconciliation). summary/todos are filled by the post-call analysis.
CREATE TABLE IF NOT EXISTS calls (
    call_id VARCHAR(64) PRIMARY KEY,
    conversation_id VARCHAR(255) NOT NULL,
    direction VARCHAR(12) NOT NULL DEFAULT 'outbound',
    phone_number VARCHAR(32) NOT NULL,
    contact_name VARCHAR(100) NULL,
    client_id VARCHAR(64) NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    requested_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    answered_at DATETIME(6) NULL,
    ended_at DATETIME(6) NULL,
    talk_seconds DECIMAL(10,2) NOT NULL DEFAULT 0,
    cdr_duration INT NULL,
    cdr_billsec INT NULL,
    cdr_disposition VARCHAR(20) NULL,
    end_reason VARCHAR(100) NULL,
    outcome VARCHAR(40) NULL,
    fail_detail VARCHAR(255) NULL,
    is_test TINYINT NOT NULL DEFAULT 0,
    source VARCHAR(12) NOT NULL DEFAULT 'live',
    summary MEDIUMTEXT NULL,
    todos MEDIUMTEXT NULL,
    analysis_json MEDIUMTEXT NULL,
    INDEX idx_calls_requested (requested_at),
    INDEX idx_calls_number (phone_number),
    INDEX idx_calls_conv (conversation_id)
) CHARACTER SET utf8mb4;

-- Effective-dated list prices (USD per 1M tokens). A price change = a new row with a new valid_from.
CREATE TABLE IF NOT EXISTS price_rates (
    id INT AUTO_INCREMENT PRIMARY KEY,
    model VARCHAR(100) NOT NULL,
    modality VARCHAR(12) NOT NULL,
    direction VARCHAR(4) NOT NULL,
    usd_per_million DECIMAL(12,6) NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE NULL,
    source VARCHAR(255) NULL,
    UNIQUE KEY uq_price (model, modality, direction, valid_from)
) CHARACTER SET utf8mb4;

INSERT IGNORE INTO price_rates (model, modality, direction, usd_per_million, valid_from, valid_to, source) VALUES
 ('gemini-3.1-flash-live-preview', 'text',  'in',  0.750000, '2000-01-01', NULL, 'aistudio.google.com/docs/models/gemini-3.1-flash-live-preview'),
 ('gemini-3.1-flash-live-preview', 'audio', 'in',  3.000000, '2000-01-01', NULL, 'aistudio.google.com/docs/models/gemini-3.1-flash-live-preview'),
 ('gemini-3.1-flash-live-preview', 'image', 'in',  1.000000, '2000-01-01', NULL, 'aistudio.google.com/docs/models/gemini-3.1-flash-live-preview'),
 ('gemini-3.1-flash-live-preview', 'text',  'out', 4.500000, '2000-01-01', NULL, 'output incl. thinking tokens'),
 ('gemini-3.1-flash-live-preview', 'audio', 'out', 12.000000, '2000-01-01', NULL, 'output incl. thinking tokens'),
 ('gemini-3.8-flash', 'text',   'in',  0.750000, '2000-01-01', '2026-12-31', 'aistudio.google.com/docs/models/gemini-3.8-flash (intro price)'),
 ('gemini-3.8-flash', 'text',   'out', 3.750000, '2000-01-01', '2026-12-31', 'output incl. thinking tokens (intro price)'),
 ('gemini-3.8-flash', 'cached', 'in',  0.075000, '2000-01-01', '2026-12-31', 'context caching read (intro price)'),
 ('gemini-3.8-flash', 'text',   'in',  1.500000, '2027-01-01', NULL, 'standard price from 2027-01-01'),
 ('gemini-3.8-flash', 'text',   'out', 7.500000, '2027-01-01', NULL, 'standard price from 2027-01-01'),
 ('gemini-3.8-flash', 'cached', 'in',  0.150000, '2027-01-01', NULL, 'standard price from 2027-01-01'),
 ('gemini-embedding-001', 'text', 'in', 0.150000, '2000-01-01', NULL, 'aistudio.google.com/docs/pricing (Gemini Embedding)');

INSERT IGNORE INTO global_settings (setting_key, setting_value) VALUES
 ('usd_to_bdt', '123.0'),
 ('test_numbers', '01797046851'),
 ('test_session_prefixes', 'test-,upaytest');
