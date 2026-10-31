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
