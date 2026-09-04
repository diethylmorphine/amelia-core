CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'unknown',
    user_id    TEXT    NOT NULL DEFAULT 'local',
    tags       TEXT    NOT NULL DEFAULT '',
    embedding  BLOB,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_user   ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source);

-- Полнотекстовый поиск как запасной путь, когда эмбеддингов нет.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    text, content='memories', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

-- ---------------------------------------------------------------- сессии
-- Диалоговый контекст. Переживает рестарт: разговор продолжается с того же места.
CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    role       TEXT    NOT NULL,          -- user | assistant
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, id);

-- Сжатая выжимка старых реплик, когда окно переполнилось.
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id  TEXT PRIMARY KEY,
    summary     TEXT NOT NULL,
    upto_turn   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------- планировщик
-- Задачи во времени: напоминания и повторяющиеся действия.
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL DEFAULT 'local',
    source      TEXT    NOT NULL DEFAULT 'cli',
    kind        TEXT    NOT NULL DEFAULT 'remind',
    payload     TEXT    NOT NULL,
    run_at      TEXT    NOT NULL,          -- ISO UTC
    every       INTEGER,                   -- секунды между повторами, NULL = разовая
    status      TEXT    NOT NULL DEFAULT 'pending',   -- pending | done | cancelled
    meta        TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL,
    fired_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);

-- ---------------------------------------------------------------- журнал действий
-- Что агент сделал по своей инициативе — и как это откатить.
CREATE TABLE IF NOT EXISTS tool_journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL DEFAULT 'local',
    tool        TEXT    NOT NULL,
    args        TEXT    NOT NULL DEFAULT '{}',
    result      TEXT    NOT NULL DEFAULT '',
    undo_token  TEXT,
    undone      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_journal_user ON tool_journal(user_id, id DESC);
