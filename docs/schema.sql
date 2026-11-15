CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    title TEXT,
    create_time REAL,
    create_time_iso TEXT,
    update_time REAL,
    update_time_iso TEXT,
    source_file TEXT,
    default_model_slug TEXT,
    is_archived TEXT,
    is_starred TEXT,
    message_count INTEGER,
    user_message_count INTEGER,
    assistant_message_count INTEGER,
    char_count INTEGER,
    tags TEXT,
    importance_score INTEGER,
    metadata_json TEXT,
    memory_space TEXT DEFAULT 'general',
    user_status TEXT DEFAULT 'unreviewed',
    user_note TEXT,
    project_name TEXT
);

CREATE TABLE messages (
    message_id TEXT,
    node_id TEXT,
    conversation_id TEXT,
    conversation_title TEXT,
    ordinal INTEGER,
    parent_id TEXT,
    children_json TEXT,
    role TEXT,
    author_name TEXT,
    create_time REAL,
    create_time_iso TEXT,
    update_time REAL,
    update_time_iso TEXT,
    content_type TEXT,
    text TEXT,
    status TEXT,
    weight REAL,
    metadata_json TEXT,
    memory_space TEXT DEFAULT 'general'
);

CREATE TABLE assets (
    asset_file TEXT PRIMARY KEY,
    original_name TEXT,
    size_bytes INTEGER
);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    conversation_title,
    role,
    text,
    content='messages',
    content_rowid='rowid'
);

CREATE TABLE memory_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_space TEXT NOT NULL,
    conversation_id TEXT,
    summary_type TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    keywords_json TEXT,
    source_message_count INTEGER DEFAULT 0,
    source_conversation_count INTEGER DEFAULT 0,
    method TEXT DEFAULT 'extractive_v1',
    model_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    metadata_json TEXT,
    CHECK (summary_type IN ('conversation_summary', 'memory_space_overview')),
    CHECK (summary_type != 'conversation_summary' OR conversation_id IS NOT NULL)
);

CREATE TABLE memory_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    memory_space TEXT NOT NULL DEFAULT 'general',
    source_platform TEXT,
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    embedding_protocol TEXT NOT NULL,
    embedding_normalized INTEGER NOT NULL,
    embedding_dim INTEGER NOT NULL,
    embedding_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_preview TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT,
    UNIQUE(
        source_table, source_id, embedding_provider, embedding_model, model_revision,
        model_fingerprint, embedding_protocol, embedding_normalized, content_hash
    )
);
