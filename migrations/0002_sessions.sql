CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  expires_at INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX sessions_expiry ON sessions(expires_at);
