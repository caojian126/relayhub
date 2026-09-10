import time

from . import db

STRATEGIES = ("balanced", "priority", "round_robin")


def norm_base(url):
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.endswith("/v1"):
        u += "/v1"
    return u


def today():
    return time.strftime("%Y-%m-%d", time.localtime())


def get_today_count(site_id, model):
    row = db.query_one(
        "SELECT count FROM daily_counters WHERE day=? AND site_id=? AND model=?",
        (today(), site_id, model),
    )
    return int(row["count"]) if row else 0


def incr_today_count(site_id, model):
    db.execute(
        """INSERT INTO daily_counters(day, site_id, model, count) VALUES(?, ?, ?, 1)
           ON CONFLICT(day, site_id, model) DO UPDATE SET count = count + 1""",
        (today(), site_id, model),
    )


def get_candidates(model):
    rows = db.query(
        """
        SELECT s.id AS site_id, s.name, s.base_url, s.api_key, s.priority, s.note,
               s.fail_streak, s.circuit_until, s.quota_known, s.quota_remaining,
               r.id AS route_id, r.model, r.upstream_model, r.daily_limit
        FROM routes r
        JOIN sites s ON s.id = r.site_id
        WHERE r.model = ? AND r.enabled = 1 AND s.enabled = 1
        """,
        (model,),
    )
    now = time.time()
    out = []
    for row in rows:
        c = db.rowdict(row)
        if c["circuit_until"] and c["circuit_until"] > now:
            continue
        if not c["base_url"]:
            continue
        limit = c["daily_limit"] or 0
        used = get_today_count(c["site_id"], model)
        if limit and used >= limit:
            continue
        c["today_count"] = used
        out.append(c)
    return out


def sort_candidates(cands, strategy):
    if strategy == "priority":
        cands.sort(key=lambda c: (c["priority"], c["today_count"]))
    elif strategy == "round_robin":
        cands.sort(key=lambda c: (c["today_count"], c["priority"]))
    else:
        def key(c):
            known = 1 if (c["quota_known"] and c["quota_remaining"] is not None) else 0
            rem = c["quota_remaining"] if known else 0
            # 先排「额度已知」的（余额多优先），再排额度未知的（按优先级）
            return (0 if known else 1, -rem, c["priority"], c["today_count"])
        cands.sort(key=key)
    return cands


def available_models():
    rows = db.query("SELECT DISTINCT model FROM routes WHERE enabled=1")
    return sorted(r["model"] for r in rows)


def record_failure(site_id, err):
    threshold = int(db.get_setting("circuit_threshold", 3) or 0)
    cooldown = int(db.get_setting("circuit_cooldown", 600) or 600)
    db.execute(
        "UPDATE sites SET fail_streak = fail_streak + 1, last_error = ? WHERE id = ?",
        (str(err)[:500], site_id),
    )
    row = db.query_one("SELECT fail_streak FROM sites WHERE id=?", (site_id,))
    if threshold > 0 and row and row["fail_streak"] >= threshold:
        db.execute(
            "UPDATE sites SET circuit_until = ? WHERE id = ?",
            (time.time() + cooldown, site_id),
        )


def record_success(site_id):
    db.execute(
        "UPDATE sites SET fail_streak = 0, circuit_until = 0, last_error = '', last_used_at = ? WHERE id = ?",
        (time.time(), site_id),
    )


def reset_circuit(site_id):
    db.execute(
        "UPDATE sites SET fail_streak = 0, circuit_until = 0, last_error = '' WHERE id = ?",
        (site_id,),
    )
