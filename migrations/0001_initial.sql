PRAGMA foreign_keys = ON;

CREATE TABLE gateways (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  last_seen TEXT,
  revoked_at TEXT
);
CREATE TABLE devices (
  id TEXT PRIMARY KEY,
  gateway_id TEXT NOT NULL REFERENCES gateways(id),
  name TEXT NOT NULL,
  brand TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL,
  port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
  driver TEXT NOT NULL CHECK(driver IN ('zkteco', 'simulator')),
  created_at TEXT NOT NULL,
  last_contact TEXT,
  last_error TEXT,
  info TEXT NOT NULL DEFAULT '{}',
  UNIQUE(gateway_id, ip, port)
);
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  gateway_id TEXT NOT NULL REFERENCES gateways(id),
  device_id TEXT REFERENCES devices(id),
  type TEXT NOT NULL,
  params TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','uncertain')),
  created_at TEXT NOT NULL,
  completed_at TEXT,
  lease_until TEXT,
  claim_token TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  request_id TEXT NOT NULL UNIQUE,
  request_hash TEXT NOT NULL
);
CREATE INDEX jobs_poll ON jobs(gateway_id, status, created_at);
CREATE TABLE device_users (
  device_id TEXT NOT NULL REFERENCES devices(id),
  uid INTEGER NOT NULL,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  card INTEGER NOT NULL DEFAULT 0,
  privilege INTEGER NOT NULL DEFAULT 0,
  synced_at TEXT NOT NULL,
  PRIMARY KEY(device_id, uid)
);
CREATE TABLE attendance (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id),
  user_id TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  device_local_time TEXT NOT NULL,
  timezone TEXT NOT NULL,
  status INTEGER NOT NULL,
  punch INTEGER NOT NULL,
  synced_at TEXT NOT NULL
);
CREATE INDEX attendance_device_time ON attendance(device_id, recorded_at DESC, id);
CREATE TABLE audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL
);
CREATE TABLE login_limits (
  key TEXT PRIMARY KEY,
  window INTEGER NOT NULL,
  attempts INTEGER NOT NULL
);
