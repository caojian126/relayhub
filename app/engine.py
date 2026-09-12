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


def upstream_headers(site):
    headers = {"Content-Type": "application/json"}
    if site.get("api_key"):
        headers["Authorization"] = "Bearer " + site["api_key"]
    return headers


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
        SELECT s.id AS site_id, s.name, s.base_url, s.api_key, s.note,
               s.priority        AS site_priority,
               s.fail_streak     AS site_fail_streak,
               s.circuit_until   AS site_circuit_until,
               s.quota_known, s.quota_remaining, s.daily_limit AS site_daily_limit,
               r.id AS route_id, r.model, r.upstream_model, r.daily_limit,
               r.priority        AS node_priority,
               r.fail_streak     AS node_fail_streak,
               r.circuit_until   AS node_circuit_until
        FROM routes r
        JOIN sites s ON s.id = r.site_id
        LEFT JOIN model_groups g ON g.name = r.model
        WHERE r.model = ? AND r.enabled = 1 AND s.enabled = 1
          AND COALESCE(g.enabled, 1) = 1
        """,
        (model,),
    )
    now = time.time()
    out = []
    for row in rows:
        c = db.rowdict(row)
        if c["site_circuit_until"] and c["site_circuit_until"] > now:
            continue
        if c["node_circuit_until"] and c["node_circuit_until"] > now:
            continue
        if not c["base_url"]:
            continue
        limit = c["daily_limit"] or 0
        used = get_today_count(c["site_id"], model)
        if limit and used >= limit:
            continue
        # 站点级「每天总共能用几次」：跨该站所有统一模型累加。0 = 不限。
        # 跟上面的 limit 是两回事：limit 管「某站×某模型」，这里管整站。
        site_limit = c["site_daily_limit"] or 0
        if site_limit:
            t = db.query_one(
                "SELECT COALESCE(SUM(count),0) AS c FROM daily_counters WHERE day=? AND site_id=?",
                (today(), c["site_id"]),
            )
            if (int(t["c"]) if t else 0) >= site_limit:
                continue
        c["today_count"] = used
        c["priority"] = c["node_priority"] if c["node_priority"] is not None else 100
        out.append(c)
    return out


def sort_candidates(cands, strategy):
    """节点排序。c["priority"] 是「站点×模型」节点的排序号（越小越优先）。"""
    if strategy == "priority":
        cands.sort(key=lambda c: (c["priority"], c["site_priority"], c["today_count"]))
    elif strategy == "round_robin":
        cands.sort(key=lambda c: (c["today_count"], c["priority"], c["site_priority"]))
    else:
        def key(c):
            known = 1 if (c["quota_known"] and c["quota_remaining"] is not None) else 0
            rem = c["quota_remaining"] if known else 0
            # 先排「额度已知」的（余额多优先），再排额度未知的（按节点顺序）
            return (0 if known else 1, -rem, c["priority"], c["site_priority"], c["today_count"])
        cands.sort(key=key)
    return cands


def available_models():
    rows = db.query("SELECT DISTINCT model FROM routes WHERE enabled=1")
    return sorted(r["model"] for r in rows)


def public_models():
    """对外 GET /v1/models 暴露的「统一模型」。

    只返回：启用 + 标记为公开 + 至少有一个可用节点。
    真实的站点模型名不会因为「配过路由」就自动暴露出去。
    """
    rows = db.query(
        """
        SELECT g.name
        FROM model_groups g
        WHERE g.enabled = 1 AND g.is_public = 1
          AND EXISTS (
              SELECT 1 FROM routes r
              JOIN sites s ON s.id = r.site_id
              WHERE r.model = g.name AND r.enabled = 1 AND s.enabled = 1
          )
        ORDER BY g.name
        """
    )
    return [r["name"] for r in rows]


def group_strategy(model):
    """统一模型自己的策略优先，留空则继承全局。"""
    row = db.query_one("SELECT strategy FROM model_groups WHERE name=?", (model,))
    if row is not None:
        s = (row["strategy"] or "").strip()
        if s in STRATEGIES:
            return s
    return db.get_setting("strategy", "balanced")


# ------------------------------------------------------------------ 错误分类

# 这些状态码一律当作「上游临时故障」，可以换下一个节点
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 507, 508, 509,
                520, 521, 522, 523, 524, 525, 527, 529, 530, 598, 599}

# 命中这些关键字 = 这个节点本身不提供这个模型 -> 换节点
_MODEL_HINTS = (
    "model not found", "model_not_found", "no such model", "model does not exist",
    "unsupported model", "unknown model", "invalid model", "not a valid model",
    "model is not available", "does not support model", "no available channel",
    "无可用渠道", "模型不存在", "不存在的模型", "无此模型", "模型未找到", "没有可用渠道",
)

# 命中这些关键字 = 这次请求本身有问题 -> 不要换节点，直接把错误还给客户端
_REQUEST_HINTS = (
    "context length", "context_length_exceeded", "maximum context",
    "too many tokens", "token limit", "exceeds the maximum", "content filter",
    "invalid_request_error", "invalid request", "missing required",
    "超出", "上下文长度", "内容过长", "参数错误", "请求格式",
)


def classify_error(status, text):
    """判断这个上游错误该不该换下一个节点。

    返回 (retryable, kind)。
    原则：网络 / 超时 / 429 / 5xx / 节点自身问题  -> 换节点
          请求本身写错了（上下文超长、参数非法）-> 不换节点，直接报错
    这样不会因为一句话写错就把所有站点全部试一遍。
    """
    t = (text or "").lower()
    if any(h in t for h in _MODEL_HINTS):
        return True, "model_unavailable"
    if any(h in t for h in _REQUEST_HINTS):
        return False, "bad_request"
    if status is None:
        return True, "network"
    if status in RETRY_STATUS:
        return True, "upstream_error"
    if status in (401, 403):
        return True, "auth"
    if status == 404:
        return True, "not_found"
    if 400 <= status < 500:
        return False, "client_error"
    return False, "unknown"


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


# ------------------------------------------------------------------ 节点级状态
#
# 一个「节点」= 一条 routes 记录 = 「站点 × 真实模型」。
# 站点级熔断管的是「整个站挂了」，节点级冷却管的是「这个站上的这个模型用不了」。

def record_node_failure(route_id, err):
    if not route_id:
        return
    threshold = int(db.get_setting("node_circuit_threshold", 3) or 0)
    cooldown = int(db.get_setting("node_cooldown", 60) or 60)
    db.execute(
        """UPDATE routes
           SET fail_streak = fail_streak + 1, fail_count = fail_count + 1,
               last_error = ?, last_fail_at = ?
           WHERE id = ?""",
        (str(err)[:500], time.time(), route_id),
    )
    row = db.query_one("SELECT fail_streak FROM routes WHERE id=?", (route_id,))
    if threshold > 0 and row and row["fail_streak"] >= threshold:
        db.execute(
            "UPDATE routes SET circuit_until = ? WHERE id = ?",
            (time.time() + cooldown, route_id),
        )


def record_node_success(route_id, latency_ms=0):
    if not route_id:
        return
    db.execute(
        """UPDATE routes
           SET fail_streak = 0, circuit_until = 0, last_error = '',
               ok_count = ok_count + 1, last_ok_at = ?, last_latency_ms = ?
           WHERE id = ?""",
        (time.time(), int(latency_ms or 0), route_id),
    )


def reset_node(route_id):
    db.execute(
        """UPDATE routes
           SET fail_streak = 0, circuit_until = 0, last_error = ''
           WHERE id = ?""",
        (route_id,),
    )


def node_state(c, now=None):
    """把一个节点渲染成面板要的中文状态。"""
    now = now or time.time()
    if c.get("node_circuit_until") and c["node_circuit_until"] > now:
        return "cooling", int(c["node_circuit_until"] - now)
    if c.get("circuit_until") and c["circuit_until"] > now:
        return "cooling", int(c["circuit_until"] - now)
    if not c.get("enabled", 1):
        return "disabled", 0
    if c.get("last_error"):
        return "error", 0
    return "ok", 0
