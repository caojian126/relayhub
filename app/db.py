import json
import sqlite3
import threading

from .config import DATA_DIR, DB_PATH

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT    NOT NULL UNIQUE,
  base_url          TEXT    NOT NULL,
  api_key           TEXT    NOT NULL DEFAULT '',
  enabled           INTEGER NOT NULL DEFAULT 1,
  priority          INTEGER NOT NULL DEFAULT 100,
  note              TEXT    NOT NULL DEFAULT '',
  fail_streak       INTEGER NOT NULL DEFAULT 0,
  circuit_until     REAL    NOT NULL DEFAULT 0,
  last_error        TEXT    NOT NULL DEFAULT '',
  last_used_at      REAL    NOT NULL DEFAULT 0,
  quota_known       INTEGER NOT NULL DEFAULT 0,
  quota_remaining   REAL,
  quota_used        REAL,
  quota_limit       REAL,
  quota_raw         TEXT    NOT NULL DEFAULT '',
  quota_error       TEXT    NOT NULL DEFAULT '',
  quota_checked_at  REAL    NOT NULL DEFAULT 0,
  created_at        REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id         INTEGER NOT NULL,
  model           TEXT    NOT NULL,
  upstream_model  TEXT    NOT NULL DEFAULT '',
  enabled         INTEGER NOT NULL DEFAULT 1,
  daily_limit     INTEGER NOT NULL DEFAULT 0,
  UNIQUE(site_id, model)
);

CREATE TABLE IF NOT EXISTS daily_counters (
  day      TEXT    NOT NULL,
  site_id  INTEGER NOT NULL,
  model    TEXT    NOT NULL,
  count    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, site_id, model)
);

CREATE TABLE IF NOT EXISTS key_counters (
  day     TEXT    NOT NULL,
  key_id  INTEGER NOT NULL,
  count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, key_id)
);

CREATE TABLE IF NOT EXISTS api_keys (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  key           TEXT    NOT NULL UNIQUE,
  name          TEXT    NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  daily_limit   INTEGER NOT NULL DEFAULT 0,
  note          TEXT    NOT NULL DEFAULT '',
  created_at    REAL    NOT NULL,
  last_used_at  REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admins (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  username       TEXT    NOT NULL UNIQUE,
  password_hash  TEXT    NOT NULL,
  created_at     REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  k  TEXT PRIMARY KEY,
  v  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_logs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                REAL    NOT NULL,
  site_id           INTEGER,
  site_name         TEXT    NOT NULL DEFAULT '',
  model             TEXT    NOT NULL DEFAULT '',
  upstream_model    TEXT    NOT NULL DEFAULT '',
  key_id            INTEGER,
  key_name          TEXT    NOT NULL DEFAULT '',
  attempt           INTEGER NOT NULL DEFAULT 1,
  ok                INTEGER NOT NULL DEFAULT 0,
  status_code       INTEGER,
  latency_ms        INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens      INTEGER NOT NULL DEFAULT 0,
  stream            INTEGER NOT NULL DEFAULT 0,
  error             TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_logs_ts    ON request_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_site  ON request_logs(site_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_routes_mdl ON routes(model);
"""

DEFAULT_SETTINGS = {
    "strategy": "balanced",            # balanced | priority | round_robin
    "max_attempts": 3,                 # 单次请求最多尝试几个上游
    "circuit_threshold": 3,            # 连续失败几次触发熔断
    "circuit_cooldown": 600,           # 熔断冷却秒数
    "inject_stream_usage": True,       # 流式时自动加 include_usage，便于统计 token
    "quota_check_interval": 3600,      # 额度巡检间隔（秒）
    "auto_sync_models": True,          # 定时自动同步上游模型
    "auto_sync_interval": 86400,       # 自动同步间隔（秒）
}


def _conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def execute(sql, params=()):
    with _write_lock:
        conn = _conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def query(sql, params=()):
    return _conn().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return _conn().execute(sql, params).fetchone()


def rowdict(row):
    return {k: row[k] for k in row.keys()} if row is not None else None


def get_setting(key, default=None):
    row = query_one("SELECT v FROM settings WHERE k=?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return row["v"]


def set_setting(key, value):
    execute(
        "INSERT INTO settings(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def init_db():
    conn = _conn()
    with _write_lock:
        conn.executescript(SCHEMA)
        conn.commit()
    for k, v in DEFAULT_SETTINGS.items():
        if query_one("SELECT 1 FROM settings WHERE k=?", (k,)) is None:
            set_setting(k, v)
