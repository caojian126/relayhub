import json
import sqlite3
import threading
import time

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
  daily_limit       INTEGER NOT NULL DEFAULT 0,
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
  quota_note        TEXT    NOT NULL DEFAULT '',
  quota_checked_at  REAL    NOT NULL DEFAULT 0,
  created_at        REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS model_groups (
  name        TEXT PRIMARY KEY,
  display     TEXT    NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1,
  is_public   INTEGER NOT NULL DEFAULT 1,
  strategy    TEXT    NOT NULL DEFAULT '',
  note        TEXT    NOT NULL DEFAULT '',
  created_at  REAL    NOT NULL DEFAULT 0,
  auto        INTEGER NOT NULL DEFAULT 0,
  ordered     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS routes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id         INTEGER NOT NULL,
  model           TEXT    NOT NULL,
  upstream_model  TEXT    NOT NULL DEFAULT '',
  enabled         INTEGER NOT NULL DEFAULT 1,
  daily_limit     INTEGER NOT NULL DEFAULT 0,
  priority        INTEGER NOT NULL DEFAULT 100,
  fail_streak     INTEGER NOT NULL DEFAULT 0,
  ok_count        INTEGER NOT NULL DEFAULT 0,
  fail_count      INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT    NOT NULL DEFAULT '',
  last_ok_at      REAL    NOT NULL DEFAULT 0,
  last_fail_at    REAL    NOT NULL DEFAULT 0,
  last_latency_ms INTEGER NOT NULL DEFAULT 0,
  circuit_until   REAL    NOT NULL DEFAULT 0,
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
  cached            INTEGER NOT NULL DEFAULT 0,
  endpoint          TEXT    NOT NULL DEFAULT 'openai',
  status_code       INTEGER,
  latency_ms        INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens      INTEGER NOT NULL DEFAULT 0,
  stream            INTEGER NOT NULL DEFAULT 0,
  switches          INTEGER NOT NULL DEFAULT 0,
  stream_phase      TEXT    NOT NULL DEFAULT '',
  error             TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS cache_entries (
  key            TEXT PRIMARY KEY,
  model          TEXT NOT NULL DEFAULT '',
  request_body   TEXT NOT NULL DEFAULT '',
  response_json  TEXT NOT NULL DEFAULT '',
  stream         INTEGER NOT NULL DEFAULT 0,
  created_at     REAL NOT NULL,
  expires_at     REAL NOT NULL DEFAULT 0,
  hits           INTEGER NOT NULL DEFAULT 0,
  last_hit_at    REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_logs_ts    ON request_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_site  ON request_logs(site_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_routes_mdl ON routes(model);
CREATE INDEX IF NOT EXISTS idx_cache_exp  ON cache_entries(expires_at);
"""

DEFAULT_SETTINGS = {
    # 默认「按用户排的顺序」。理由：各站额度字段的单位和真伪都很不可靠
    # （见 quota.py：很多站把「不限」写成一个亿的占位值），拿余额当默认
    # 排序依据会把顺序搞乱。想按余额排的人去「设置」或「顺序」页自己切。
    "strategy": "priority",             # priority | balanced | round_robin
    "max_attempts": 3,                  # 单次请求最多尝试几个上游
    "circuit_threshold": 3,             # 连续失败几次触发熔断
    "circuit_cooldown": 600,            # 熔断冷却秒数
    "inject_stream_usage": True,        # 流式时自动加 include_usage，便于统计 token
    "quota_check_interval": 3600,       # 额度巡检间隔（秒）
    "auto_sync_models": False,          # 定时自动同步上游模型。默认关：
                                        # 不许不打招呼就把上游几百个模型全导进路由
    "auto_sync_interval": 86400,        # 自动同步间隔（秒）
    "cache_enabled": False,             # 响应缓存总开关
    "cache_endpoints": ["openai"],      # 允许读写缓存的入口
    "cache_only_deterministic": True,   # 仅缓存 temperature=0 的请求
    "cache_ttl": 3600,                  # 缓存有效期（秒），0 = 永不过期
    "cache_max_entries": 1000,          # 最大缓存条目数
    "node_circuit_threshold": 3,        # 单个「站点×模型」节点连续失败几次进入冷却
    "node_cooldown": 60,                # 节点冷却秒数（期间跳过该节点，到点自动恢复）
    "rewrite_model": True,              # 回包时把 model 字段改写成客户端请求的统一模型名
    "switch_before_output": True,       # 流式：开始输出前允许换节点；开始输出后绝不换
}

# 建表后需要补充的列（兼容旧数据库）
MIGRATIONS = {
    "sites": {
        # 「一个站一天总共能用几次」：跨该站所有统一模型累加。0 = 不限。
        "daily_limit": "INTEGER NOT NULL DEFAULT 0",
        # 额度查询的提示语（占位值 / 只返回了系统级上限 之类）
        "quota_note": "TEXT NOT NULL DEFAULT ''",
    },
    "request_logs": {
        "cached": "INTEGER NOT NULL DEFAULT 0",
        "endpoint": "TEXT NOT NULL DEFAULT 'openai'",
        "switches": "INTEGER NOT NULL DEFAULT 0",
        "stream_phase": "TEXT NOT NULL DEFAULT ''",
    },
    "routes": {
        "priority": "INTEGER NOT NULL DEFAULT 100",
        "fail_streak": "INTEGER NOT NULL DEFAULT 0",
        "ok_count": "INTEGER NOT NULL DEFAULT 0",
        "fail_count": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "last_ok_at": "REAL NOT NULL DEFAULT 0",
        "last_fail_at": "REAL NOT NULL DEFAULT 0",
        "last_latency_ms": "INTEGER NOT NULL DEFAULT 0",
        "circuit_until": "REAL NOT NULL DEFAULT 0",
    },
    "model_groups": {
        "auto": "INTEGER NOT NULL DEFAULT 0",
        # 是否在「统一模型」页里手工拖过节点顺序。
        # 拖过 = 这个模型听自己的 routes.priority；没拖过 = 跟随「顺序」页的全局站序。
        "ordered": "INTEGER NOT NULL DEFAULT 0",
    },
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


def _migrate(conn):
    for table, cols in MIGRATIONS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        have = {r["name"] for r in rows}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    conn = _conn()
    with _write_lock:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    for k, v in DEFAULT_SETTINGS.items():
        if query_one("SELECT 1 FROM settings WHERE k=?", (k,)) is None:
            set_setting(k, v)
    _migrate_settings()


def _migrate_settings():
    """一次性的设置项迁移。每项只做一次，用哨兵值记住做过没有。"""
    # v0.5.1：auto_sync_models 的默认值从「开」改成「关」。
    # 老库里存的是 True，启动 45 秒后会把上游所有模型统统导进路由，
    # 跟「用户自己挑模型」的设计直接冲突 —— 所以这里强制关一次。
    # 真想用的人去「设置」页自己打开。
    if get_setting("_mig_auto_sync_off") is None:
        set_setting("auto_sync_models", False)
        set_setting("_mig_auto_sync_off", True)
    sync_groups()
    backfill_group_origin()


def ensure_group(name, auto=1, is_public=0):
    """确保存在一条统一模型记录；已经有了就完全不动（绝不覆盖用户设置）。"""
    name = (name or "").strip()
    if not name:
        return
    if query_one("SELECT 1 FROM model_groups WHERE name=?", (name,)) is None:
        execute(
            """INSERT INTO model_groups
               (name, display, enabled, is_public, strategy, note, created_at, auto)
               VALUES (?, '', 1, ?, '', '', ?, ?)""",
            (name, int(bool(is_public)), time.time(), int(bool(auto))),
        )


def sync_groups():
    """给每个 routes.model 补一条统一模型记录（auto=1，默认不对外公开）。

    auto=1 表示「跟着路由自动生成的，不是用户手工建的」。
    这类分组默认 is_public=0 —— 不会出现在 /v1/models 里，
    免得把上游几百个模型名一股脑怼给客户端。
    想公开的话，在「统一模型」页一键切换即可。
    """
    for row in query("SELECT DISTINCT model FROM routes"):
        name = (row["model"] or "").strip()
        if not name:
            continue
        nodes = query("SELECT upstream_model FROM routes WHERE model=?", (name,))
        # 只要有一个节点绑了真实模型名，就说明这是用户手工建的跨站统一模型
        manual = any((r["upstream_model"] or "").strip() for r in nodes)
        ensure_group(name, auto=0 if manual else 1, is_public=1 if manual else 0)


def purge_empty_auto_groups():
    """清掉「自动生成、且已经没有任何节点」的分组。

    删完路由后不留空壳，否则统一模型页会攒一堆点进去是空的条目。
    """
    cur = execute(
        """DELETE FROM model_groups
           WHERE auto=1 AND name NOT IN (SELECT DISTINCT model FROM routes)"""
    )
    return cur.rowcount


def backfill_group_origin():
    """把老库里「自动生成的分组」认出来并标记（只做一次）。

    判断依据：该分组下所有节点的 upstream_model 都是空的 —— 说明它只是
    「导入某个模型」的副产品，不是用户手工绑的跨站统一模型。
    这类分组改成 auto=1 + is_public=0，从「统一模型」页默认视图和 /v1/models 里挪走。
    绑过真实模型名的分组一律原样保留，所以你手工建的 auto 不会被误伤。
    """
    if get_setting("_mig_group_origin") is not None:
        return
    for row in query("SELECT name FROM model_groups"):
        name = row["name"]
        nodes = query("SELECT upstream_model FROM routes WHERE model=?", (name,))
        if not nodes:
            continue  # 空分组，可能是刚建的，不动
        if any((r["upstream_model"] or "").strip() for r in nodes):
            # 绑过真实模型名 -> 用户手工建的跨站统一模型，保持原样（公开）
            execute("UPDATE model_groups SET auto=0, is_public=1 WHERE name=?", (name,))
            continue
        execute("UPDATE model_groups SET auto=1, is_public=0 WHERE name=?", (name,))
    set_setting("_mig_group_origin", True)
