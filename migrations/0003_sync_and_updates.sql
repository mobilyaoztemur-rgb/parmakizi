ALTER TABLE devices ADD COLUMN sync_enabled INTEGER NOT NULL DEFAULT 0 CHECK(sync_enabled IN (0,1));
ALTER TABLE devices ADD COLUMN sync_interval_seconds INTEGER NOT NULL DEFAULT 60 CHECK(sync_interval_seconds BETWEEN 30 AND 86400);
ALTER TABLE devices ADD COLUMN next_sync_at TEXT;
ALTER TABLE devices ADD COLUMN last_sync_at TEXT;
ALTER TABLE devices ADD COLUMN sync_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE devices ADD COLUMN settings TEXT NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual';
CREATE INDEX devices_sync_due ON devices(gateway_id,sync_enabled,next_sync_at);

CREATE TABLE releases (
  version TEXT PRIMARY KEY,
  version_code INTEGER NOT NULL UNIQUE,
  payload TEXT NOT NULL,
  signature TEXT NOT NULL,
  published_at TEXT NOT NULL
);
