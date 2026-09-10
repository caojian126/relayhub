"""旧库升级冒烟测试

模拟一个 v0.4.1 的旧数据库（没有 model_groups、routes 没有节点级字段、
request_logs 没有 switches/stream_phase），然后用当前代码执行 init_db()，
断言：

  1. 表结构自动补齐（ALTER TABLE，不删库）
  2. 站点 / API Key / 路由 / 上游模型名 一条不丢
  3. 旧的 routes.model 自动补齐成统一模型记录

用法： python3 tests/smoke_db.py
"""
import os
import pathlib
import sqlite3
import sys
import tempfile

tmp = pathlib.Path(tempfile.mkdtemp(prefix="rh-olddb-"))
os.environ["DATA_DIR"] = str(tmp)
os.environ["DB_PATH"] = str(tmp / "relayhub.db")

OLD_SCHEMA = """
CREATE TABLE sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 100,
  note TEXT NOT NULL DEFAULT '', fail_streak INTEGER NOT NULL DEFAULT 0,
  circuit_until REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
  last_used_at REAL NOT NULL DEFAULT 0, quota_known INTEGER NOT NULL DEFAULT 0,
  quota_remaining REAL, quota_used REAL, quota_limit REAL,
  quota_raw TEXT NOT NULL DEFAULT '', quota_error TEXT NOT NULL DEFAULT '',
  quota_checked_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL,
  model TEXT NOT NULL, upstream_model TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, daily_limit INTEGER NOT NULL DEFAULT 0,
  UNIQUE(site_id, model));
CREATE TABLE request_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, site_id INTEGER,
  site_name TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
  upstream_model TEXT NOT NULL DEFAULT '', key_id INTEGER,
  key_name TEXT NOT NULL DEFAULT '', attempt INTEGER NOT NULL DEFAULT 1,
  ok INTEGER NOT NULL DEFAULT 0, status_code INTEGER,
  latency_ms INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
  stream INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
"""

old = sqlite3.connect(str(tmp / "relayhub.db"))
old.executescript(OLD_SCHEMA)
old.execute("INSERT INTO sites(name, base_url, api_key, created_at) "
            "VALUES('A站', 'https://a.example.com/v1', 'sk-aaaa-secret', 1.0)")
old.execute("INSERT INTO routes(site_id, model, upstream_model) "
            "VALUES(1, 'auto', 'claude-sonnet-4-5')")
old.execute("INSERT INTO routes(site_id, model) VALUES(1, 'gpt-5')")
old.commit()
old.close()

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app import db  # noqa: E402

db.init_db()

# 1. 结构补齐
rcols = {r["name"] for r in db.query("PRAGMA table_info(routes)")}
need = {"priority", "fail_streak", "ok_count", "fail_count", "last_error",
        "last_ok_at", "last_fail_at", "last_latency_ms", "circuit_until"}
assert need <= rcols, f"routes 缺列: {sorted(need - rcols)}"

lcols = {r["name"] for r in db.query("PRAGMA table_info(request_logs)")}
assert {"cached", "endpoint", "switches", "stream_phase"} <= lcols, \
    f"request_logs 缺列: {sorted(lcols)}"

# 2. 数据不丢
sites = db.query("SELECT * FROM sites")
assert len(sites) == 1 and sites[0]["api_key"] == "sk-aaaa-secret", "站点或 Key 丢了"

routes = db.query("SELECT * FROM routes ORDER BY id")
assert len(routes) == 2, f"路由条数变成 {len(routes)}"
assert routes[0]["upstream_model"] == "claude-sonnet-4-5", "上游模型名丢了"
assert routes[0]["priority"] == 100, "新列默认值不对"

# 3. 统一模型自动补齐
groups = {r["name"] for r in db.query("SELECT name FROM model_groups")}
assert groups == {"auto", "gpt-5"}, f"统一模型没补齐: {sorted(groups)}"

# 4. 再跑一次必须幂等
db.init_db()
assert len(db.query("SELECT * FROM routes")) == 2
assert len(db.query("SELECT * FROM model_groups")) == 2, "重复初始化产生了重复记录"

# 5. 公开模型列表
from app import engine  # noqa: E402
pub = engine.public_models()
assert pub == ["auto", "gpt-5"], f"public_models 不对: {pub}"

print(f"OK 旧库升级：站点 {len(sites)} / 路由 {len(routes)} / "
      f"统一模型 {sorted(groups)} / 对外模型 {pub}")
