import hashlib
import json
import time

from . import db

ENDPOINTS = ("openai", "responses", "anthropic", "gemini")


def make_key(unified_body):
    """基于「统一 OpenAI 请求体」生成缓存 key。

    stream / stream_options 不参与计算，因此流式与非流式共享同一条缓存；
    四种客户端格式翻译后若等价，也共享同一条缓存。
    """
    body = dict(unified_body or {})
    body.pop("stream", None)
    body.pop("stream_options", None)
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def allowed(endpoint):
    if not db.get_setting("cache_enabled", False):
        return False
    eps = db.get_setting("cache_endpoints", ["openai"])
    if isinstance(eps, str):
        eps = [x.strip() for x in eps.split(",") if x.strip()]
    if not isinstance(eps, list) or not eps:
        return False
    return endpoint in eps


def is_cacheable(body):
    """判断这次请求是否值得缓存。"""
    if not isinstance(body, dict):
        return False
    if db.get_setting("cache_only_deterministic", True):
        t = body.get("temperature")
        try:
            if t is None or float(t) > 0.01:
                return False
        except (TypeError, ValueError):
            return False
    return True


def get(key):
    if not key:
        return None
    row = db.query_one("SELECT * FROM cache_entries WHERE key=?", (key,))
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] < time.time():
        db.execute("DELETE FROM cache_entries WHERE key=?", (key,))
        return None
    db.execute(
        "UPDATE cache_entries SET hits = hits + 1, last_hit_at = ? WHERE key=?",
        (time.time(), key),
    )
    d = db.rowdict(row)
    try:
        d["response_json"] = json.loads(d["response_json"])
    except Exception:
        return None
    return d


def put(key, model, request_body, response_json, stream=False):
    if not key or not isinstance(response_json, dict):
        return False
    ttl = int(db.get_setting("cache_ttl", 3600) or 0)
    now = time.time()
    expires = now + ttl if ttl > 0 else 0
    try:
        req = json.dumps(request_body, ensure_ascii=False)[:8000]
        resp = json.dumps(response_json, ensure_ascii=False)
    except Exception:
        return False
    db.execute(
        """INSERT INTO cache_entries
           (key, model, request_body, response_json, stream, created_at, expires_at, hits, last_hit_at)
           VALUES (?,?,?,?,?,?,?,0,0)
           ON CONFLICT(key) DO UPDATE SET
             response_json=excluded.response_json,
             request_body=excluded.request_body,
             created_at=excluded.created_at,
             expires_at=excluded.expires_at""",
        (key, model or "", req, resp, 1 if stream else 0, now, expires),
    )
    trim()
    return True


def trim():
    maxn = int(db.get_setting("cache_max_entries", 1000) or 1000)
    if maxn <= 0:
        return
    row = db.query_one("SELECT COUNT(*) AS c FROM cache_entries")
    if not row or row["c"] <= maxn:
        return
    excess = row["c"] - maxn
    db.execute(
        """DELETE FROM cache_entries WHERE key IN (
             SELECT key FROM cache_entries
             ORDER BY COALESCE(last_hit_at, 0) ASC, created_at ASC LIMIT ?)""",
        (excess,),
    )


def purge_expired():
    cur = db.execute(
        "DELETE FROM cache_entries WHERE expires_at > 0 AND expires_at < ?", (time.time(),)
    )
    return cur.rowcount


def clear():
    db.execute("DELETE FROM cache_entries")


def delete(key):
    db.execute("DELETE FROM cache_entries WHERE key=?", (key,))


def stats():
    row = db.query_one(
        """SELECT COUNT(*) AS n,
                  COALESCE(SUM(hits), 0) AS h,
                  COALESCE(SUM(LENGTH(response_json)), 0) AS sz
           FROM cache_entries"""
    )
    return {
        "entries": int(row["n"]) if row else 0,
        "hits": int(row["h"]) if row else 0,
        "bytes": int(row["sz"]) if row else 0,
    }
