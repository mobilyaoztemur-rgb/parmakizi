"""Remember acknowledged attendance hashes, without storing personnel records."""
import hashlib
import json
import sqlite3
from pathlib import Path


class AttendanceCache:
    def __init__(self, path=':memory:'):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS uploaded(device_id TEXT, record_hash TEXT, PRIMARY KEY(device_id,record_hash))')

    @staticmethod
    def digest(record):
        keys = ('userId', 'recordedAt', 'status', 'punch')
        return hashlib.sha256(json.dumps([record[k] for k in keys], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

    def unseen(self, device_id, records):
        result = []
        in_batch = set()
        for record in records:
            digest = self.digest(record)
            if digest not in in_batch and not self.db.execute('SELECT 1 FROM uploaded WHERE device_id=? AND record_hash=?', (device_id, digest)).fetchone():
                result.append(record)
                in_batch.add(digest)
        return result

    def acknowledge(self, device_id, records):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO uploaded VALUES(?,?)', [(device_id, self.digest(r)) for r in records])

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
