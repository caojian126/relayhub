import asyncio
import datetime
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse

from . import db, engine, models_sync, quota, security
from .config import CONNECT_TIMEOUT, REQUEST_TIMEOUT
from .db import DEFAULT_SETTINGS

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    security.seed_admin()
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        follow_redirects=True,
    )
    tasks = [
        asyncio.create_task(quota.background_loop()),
        asyncio.create_task(models_sync.background_loop(app.state.client)),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await app.state.client.aclose()


app = FastAPI(title="RelayHub", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------------------------------------------------------------- 通用工具

def _bearer(request: Request):
    auth = request.headers.get("Authorization", "") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def require_admin(request: Request):
    user = security.verify_token(_bearer(request))
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    return user


def auth_api_key(request: Request):
    """返回 key 行 dict；若系统里还没有任何 key 则返回 None（开放模式）。"""
    if db.query_one("SELECT 1 FROM api_keys LIMIT 1") is None:
        return None
    token = _bearer(request)
    if not token:
        raise HTTPException(401, "缺少 API Key")
    row = db.query_one("SELECT * FROM api_keys WHERE key=? AND enabled=1", (token,))
    if row is None:
        raise HTTPException(401, "API Key 无效或已禁用")
    limit = row["daily_limit"] or 0
    if limit:
        c = db.query_one(
            "SELECT count FROM key_counters WHERE day=? AND key_id=?",
            (engine.today(), row["id"]),
        )
        used = int(c["count"]) if c else 0
        if used >= limit:
            raise HTTPException(429, f"该 API Key 今日调用次数已用完（{limit} 次）")
    return db.rowdict(row)


def incr_key_counter(key_id):
    if not key_id:
        return
    db.execute(
        """INSERT INTO key_counters(day, key_id, count) VALUES(?, ?, 1)
           ON CONFLICT(day, key_id) DO UPDATE SET count = count + 1""",
        (engine.today(), key_id),
    )
    db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time(), key_id))


def write_log(*, site=None, model="", upstream_model="", key_info=None, attempt=1, ok=0,
              status_code=None, latency_ms=0, usage=None, stream=0, error=""):
    u = usage or {}
    db.execute(
        """INSERT INTO request_logs
           (ts, site_id, site_name, model, upstream_model, key_id, key_name, attempt, ok,
            status_code, latency_ms, prompt_tokens, completion_tokens, total_tokens, stream, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            time.time(),
            site["site_id"] if site else None,
            site["name"] if site else "",
            model,
            upstream_model,
            key_info.get("id") if key_info else None,
            key_info.get("name", "") if key_info else "",
            attempt, ok, status_code, latency_ms,
            int(u.get("prompt_tokens") or 0),
            int(u.get("completion_tokens") or 0),
            int(u.get("total_tokens") or 0),
            stream, str(error)[:1000],
        ),
    )


class SSEUsage:
    """从 SSE 流里拾 usage 字段（用于统计 token）。"""

    def __init__(self):
        self.buf = b""
        self.usage = None

    def feed(self, chunk: bytes):
        if self.usage is not None:
            return
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                self.usage = obj["usage"]
        if len(self.buf) > 2_000_000:
            self.buf = self.buf[-10000:]


def sse_error(message):
    payload = {"error": {"message": message, "type": "relayhub_error", "code": "no_upstream"}}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


# ---------------------------------------------------------------- OpenAI 兼容层

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    if not isinstance(body, dict) or not body.get("model"):
        raise HTTPException(400, "缺少 model 字段")

    key_info = auth_api_key(request)
    model = body["model"]
    is_stream = bool(body.get("stream"))

    strategy = db.get_setting("strategy", "balanced")
    max_attempts = int(db.get_setting("max_attempts", 3) or 3)
    inject_usage = bool(db.get_setting("inject_stream_usage", True))

    cands = engine.get_candidates(model)
    if not cands:
        avail = engine.available_models()
        raise HTTPException(
            503,
            f"没有可用上游支持模型 {model}。已配置模型：{', '.join(avail) if avail else '（空）'}",
        )
    engine.sort_candidates(cands, strategy)
    incr_key_counter(key_info["id"] if key_info else None)

    client = request.app.state.client

    # ---------------- 非流式 ----------------
    if not is_stream:
        last_err = "未知错误"
        for attempt, site in enumerate(cands[:max_attempts], start=1):
            payload = dict(body)
            if site["upstream_model"]:
                payload["model"] = site["upstream_model"]
            engine.incr_today_count(site["site_id"], model)
            t0 = time.time()
            try:
                r = await client.post(
                    engine.norm_base(site["base_url"]) + "/chat/completions",
                    json=payload,
                    headers=engine.upstream_headers(site),
                )
            except Exception as e:
                engine.record_failure(site["site_id"], e)
                write_log(site=site, model=model, upstream_model=payload["model"],
                          key_info=key_info, attempt=attempt, ok=0,
                          latency_ms=int((time.time() - t0) * 1000), error=f"连接失败: {e}")
                last_err = f"{site['name']}: {e}"
                continue

            latency = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception as e:
                    engine.record_failure(site["site_id"], f"返回非 JSON: {e}")
                    write_log(site=site, model=model, upstream_model=payload["model"],
                              key_info=key_info, attempt=attempt, ok=0, status_code=200,
                              latency_ms=latency, error="返回非 JSON")
                    last_err = f"{site['name']}: 返回非 JSON"
                    continue
                engine.record_success(site["site_id"])
                write_log(site=site, model=model, upstream_model=payload["model"],
                          key_info=key_info, attempt=attempt, ok=1, status_code=200,
                          latency_ms=latency, usage=data.get("usage"))
                return JSONResponse(data)

            err_text = (r.text or "")[:500]
            engine.record_failure(site["site_id"], f"{r.status_code} {err_text}")
            write_log(site=site, model=model, upstream_model=payload["model"],
                      key_info=key_info, attempt=attempt, ok=0, status_code=r.status_code,
                      latency_ms=latency, error=err_text)
            last_err = f"{site['name']}: HTTP {r.status_code} {err_text}"

        raise HTTPException(502, f"所有上游均失败。最后错误：{last_err}")

    # ---------------- 流式 ----------------
    async def gen():
        attempts = 0
        last_err = "未知错误"
        for site in cands:
            if attempts >= max_attempts:
                break
            attempts += 1

            payload = dict(body)
            if site["upstream_model"]:
                payload["model"] = site["upstream_model"]
            if inject_usage:
                so = payload.get("stream_options")
                if not isinstance(so, dict):
                    so = {}
                so["include_usage"] = True
                payload["stream_options"] = so

            engine.incr_today_count(site["site_id"], model)
            t0 = time.time()
            try:
                req = client.build_request(
                    "POST",
                    engine.norm_base(site["base_url"]) + "/chat/completions",
                    json=payload,
                    headers=engine.upstream_headers(site),
                )
                resp = await client.send(req, stream=True)
            except Exception as e:
                engine.record_failure(site["site_id"], e)
                write_log(site=site, model=model, upstream_model=payload["model"],
                          key_info=key_info, attempt=attempts, ok=0, stream=1,
                          latency_ms=int((time.time() - t0) * 1000), error=f"连接失败: {e}")
                last_err = f"{site['name']}: {e}"
                continue

            if resp.status_code != 200:
                try:
                    txt = (await resp.aread()).decode("utf-8", "ignore")[:500]
                except Exception:
                    txt = ""
                await resp.aclose()
                engine.record_failure(site["site_id"], f"{resp.status_code} {txt}")
                write_log(site=site, model=model, upstream_model=payload["model"],
                          key_info=key_info, attempt=attempts, ok=0, stream=1,
                          status_code=resp.status_code,
                          latency_ms=int((time.time() - t0) * 1000), error=txt)
                last_err = f"{site['name']}: HTTP {resp.status_code} {txt}"
                continue

            # 拿到 200，开始往外吐字节 —— 从此不能再切换上游
            engine.record_success(site["site_id"])
            tracker = SSEUsage()
            failed = False
            try:
                async for chunk in resp.aiter_bytes():
                    tracker.feed(chunk)
                    yield chunk
            except Exception as e:
                failed = True
                last_err = f"{site['name']} 传输中断: {e}"
                write_log(site=site, model=model, upstream_model=payload["model"],
                          key_info=key_info, attempt=attempts, ok=0, stream=1,
                          status_code=200, latency_ms=int((time.time() - t0) * 1000),
                          error=f"传输中断: {e}")
            finally:
                await resp.aclose()

            if not failed:
                write_log(site=site, model=model, upstream_model=payload["model"],
                          key_info=key_info, attempt=attempts, ok=1, stream=1,
                          status_code=200, latency_ms=int((time.time() - t0) * 1000),
                          usage=tracker.usage)
            return

        yield sse_error(f"所有上游均失败。最后错误：{last_err}")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@app.get("/v1/models")
async def list_models(request: Request):
    auth_api_key(request)
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "relayhub"}
            for m in engine.available_models()
        ],
    }


# ---------------------------------------------------------------- 管理 API

@app.post("/admin/api/login")
async def admin_login(payload: dict = Body(...)):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not security.check_login(username, password):
        raise HTTPException(401, "用户名或密码错误")
    return {"token": security.make_token(username), "username": username}


@app.get("/admin/api/me")
async def admin_me(request: Request):
    return {"username": require_admin(request)}


@app.get("/admin/api/account")
async def get_account(request: Request):
    require_admin(request)
    row = db.query_one("SELECT username FROM admins ORDER BY id LIMIT 1")
    return {
        "username": row["username"] if row else "",
        "password_from_env": security.password_from_env(),
        "username_from_env": security.username_from_env(),
    }


@app.put("/admin/api/account")
async def put_account(request: Request, payload: dict = Body(...)):
    require_admin(request)
    row = db.query_one("SELECT * FROM admins ORDER BY id LIMIT 1")
    if row is None:
        raise HTTPException(400, "账号不存在")

    new_username = (payload.get("username") or "").strip()
    old_pw = payload.get("old_password") or ""
    new_pw = payload.get("new_password") or ""

    if new_pw:
        if security.password_from_env():
            raise HTTPException(
                400,
                "面板密码由环境变量 ADMIN_PASSWORD 管理，请修改该环境变量后重启服务",
            )
        if len(new_pw) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        if not security.verify_password(old_pw, row["password_hash"]):
            raise HTTPException(400, "原密码不正确")
        db.execute("UPDATE admins SET password_hash=? WHERE id=?",
                   (security.hash_password(new_pw), row["id"]))

    if new_username and new_username != row["username"]:
        if security.username_from_env():
            raise HTTPException(
                400, "面板账号由环境变量 ADMIN_USERNAME 管理，请修改该环境变量后重启服务"
            )
        try:
            db.execute("UPDATE admins SET username=? WHERE id=?", (new_username, row["id"]))
        except Exception as e:
            raise HTTPException(400, f"修改失败：{e}")

    return {"ok": True}


# -------- 配置备份

@app.get("/admin/api/export")
async def export_config(request: Request):
    require_admin(request)
    sites = []
    for row in db.query("SELECT * FROM sites ORDER BY id"):
        d = db.rowdict(row)
        sites.append({
            "name": d["name"],
            "base_url": d["base_url"],
            "api_key": d["api_key"],
            "enabled": d["enabled"],
            "priority": d["priority"],
            "note": d["note"],
        })
    routes = [
        db.rowdict(r) for r in db.query(
            "SELECT site_id, model, upstream_model, enabled, daily_limit FROM routes"
        )
    ]
    keys = [
        db.rowdict(r) for r in db.query(
            "SELECT key, name, enabled, daily_limit, note FROM api_keys"
        )
    ]
    return {
        "version": 1,
        "app": "relayhub",
        "exported_at": time.time(),
        "settings": {k: db.get_setting(k) for k in DEFAULT_SETTINGS},
        "sites": sites,
        "routes": routes,
        "keys": keys,
    }


@app.post("/admin/api/import")
async def import_config(request: Request, payload: dict = Body(...)):
    require_admin(request)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    name_to_id = {}
    for s in data.get("sites") or []:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        base_url = (s.get("base_url") or "").strip()
        api_key = s.get("api_key") or ""
        enabled = 1 if s.get("enabled", True) else 0
        priority = int(s.get("priority") or 100)
        note = s.get("note") or ""
        row = db.query_one("SELECT id FROM sites WHERE name=?", (name,))
        if row:
            db.execute(
                """UPDATE sites SET base_url=?, api_key=?, enabled=?, priority=?, note=?
                   WHERE id=?""",
                (base_url, api_key, enabled, priority, note, row["id"]),
            )
            name_to_id[name] = row["id"]
        else:
            cur = db.execute(
                """INSERT INTO sites(name, base_url, api_key, enabled, priority, note, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (name, base_url, api_key, enabled, priority, note, time.time()),
            )
            name_to_id[name] = cur.lastrowid

    site_by_id = {}
    for row in db.query("SELECT id, name FROM sites"):
        site_by_id[row["id"]] = row["name"]

    added_routes = 0
    for r in data.get("routes") or []:
        model = (r.get("model") or "").strip()
        if not model:
            continue
        site_name = r.get("site_name") or site_by_id.get(r.get("site_id"))
        if not site_name or site_name not in name_to_id:
            continue
        site_id = name_to_id[site_name]
        upstream_model = (r.get("upstream_model") or "").strip()
        daily_limit = int(r.get("daily_limit") or 0)
        enabled = 1 if r.get("enabled", True) else 0
        db.execute(
            """INSERT INTO routes(site_id, model, upstream_model, enabled, daily_limit)
               VALUES (?,?,?,?,?)
               ON CONFLICT(site_id, model) DO UPDATE SET
                 upstream_model=excluded.upstream_model,
                 daily_limit=excluded.daily_limit,
                 enabled=excluded.enabled""",
            (site_id, model, upstream_model, enabled, daily_limit),
        )
        added_routes += 1

    added_keys = 0
    for k in data.get("keys") or []:
        key = (k.get("key") or "").strip()
        if not key:
            continue
        if db.query_one("SELECT 1 FROM api_keys WHERE key=?", (key,)):
            continue
        db.execute(
            """INSERT INTO api_keys(key, name, enabled, daily_limit, note, created_at)
               VALUES (?,?,?,?,?,?)""",
            (key, k.get("name") or "未命名", 1 if k.get("enabled", True) else 0,
             int(k.get("daily_limit") or 0), k.get("note") or "", time.time()),
        )
        added_keys += 1

    for k, v in (data.get("settings") or {}).items():
        if k in DEFAULT_SETTINGS:
            db.set_setting(k, v)

    return {
        "ok": True,
        "sites": len(data.get("sites") or []),
        "routes": added_routes,
        "keys": added_keys,
    }


# -------- 站点

@app.get("/admin/api/sites")
async def list_sites(request: Request):
    require_admin(request)
    now = time.time()
    out = []
    for row in db.query("SELECT * FROM sites ORDER BY priority, id"):
        d = db.rowdict(row)
        d["api_key_masked"] = security.mask_key(d.pop("api_key", ""))
        c = db.query_one(
            "SELECT COALESCE(SUM(count),0) AS c FROM daily_counters WHERE day=? AND site_id=?",
            (engine.today(), d["id"]),
        )
        d["today_total"] = int(c["c"]) if c else 0
        d["circuit_active"] = bool(d["circuit_until"] and d["circuit_until"] > now)
        out.append(d)
    return out


@app.post("/admin/api/sites")
async def create_site(request: Request, payload: dict = Body(...)):
    require_admin(request)
    name = (payload.get("name") or "").strip()
    base_url = (payload.get("base_url") or "").strip()
    if not name or not base_url:
        raise HTTPException(400, "名称和 Base URL 必填")
    try:
        cur = db.execute(
            """INSERT INTO sites(name, base_url, api_key, enabled, priority, note, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (name, base_url, (payload.get("api_key") or "").strip(),
             1 if payload.get("enabled", True) else 0,
             int(payload.get("priority") or 100),
             payload.get("note") or "", time.time()),
        )
    except Exception as e:
        raise HTTPException(400, f"创建失败（名称可能重复）：{e}")
    return {"id": cur.lastrowid}


@app.put("/admin/api/sites/{site_id}")
async def update_site(site_id: int, request: Request, payload: dict = Body(...)):
    require_admin(request)
    if db.query_one("SELECT 1 FROM sites WHERE id=?", (site_id,)) is None:
        raise HTTPException(404, "站点不存在")
    fields, values = [], []
    for col in ("name", "base_url", "note"):
        if col in payload and payload[col] is not None:
            fields.append(f"{col}=?")
            values.append(str(payload[col]).strip())
    if payload.get("api_key"):
        fields.append("api_key=?")
        values.append(str(payload["api_key"]).strip())
    if "priority" in payload and payload["priority"] is not None:
        fields.append("priority=?")
        values.append(int(payload["priority"]))
    if "enabled" in payload:
        fields.append("enabled=?")
        values.append(1 if payload["enabled"] else 0)
    if not fields:
        return {"ok": True}
    values.append(site_id)
    db.execute(f"UPDATE sites SET {', '.join(fields)} WHERE id=?", values)
    return {"ok": True}


@app.delete("/admin/api/sites/{site_id}")
async def delete_site(site_id: int, request: Request):
    require_admin(request)
    db.execute("DELETE FROM routes WHERE site_id=?", (site_id,))
    db.execute("DELETE FROM sites WHERE id=?", (site_id,))
    return {"ok": True}


@app.post("/admin/api/sites/{site_id}/reset")
async def reset_site(site_id: int, request: Request):
    require_admin(request)
    engine.reset_circuit(site_id)
    return {"ok": True}


@app.post("/admin/api/sites/{site_id}/test")
async def test_site(site_id: int, request: Request):
    require_admin(request)
    row = db.query_one("SELECT * FROM sites WHERE id=?", (site_id,))
    if row is None:
        raise HTTPException(404, "站点不存在")
    result = await models_sync.scan_site(request.app.state.client, db.rowdict(row))
    if result["ok"]:
        return {"ok": True, "message": f"正常，发现 {len(result['models'])} 个模型",
                "models": result["models"]}
    return {"ok": False, "message": result["error"], "models": []}


@app.post("/admin/api/sites/{site_id}/quota")
async def site_quota(site_id: int, request: Request):
    require_admin(request)
    row = db.query_one("SELECT * FROM sites WHERE id=?", (site_id,))
    if row is None:
        raise HTTPException(404, "站点不存在")
    site = db.rowdict(row)
    info = await quota.check_site(
        request.app.state.client, site_id, site["base_url"], site["api_key"]
    )
    return info


# -------- 模型发现与同步

@app.post("/admin/api/models/scan")
async def scan_models(request: Request):
    require_admin(request)
    return await models_sync.scan_all(request.app.state.client)


@app.post("/admin/api/models/import")
async def import_models(request: Request, payload: dict = Body(...)):
    require_admin(request)
    items = payload.get("items") or []
    daily_limit = int(payload.get("daily_limit") or 0)
    added = models_sync.import_models(items, daily_limit)
    return {"ok": True, "added": added, "total": len(items)}


# -------- 路由（站点 × 模型）

@app.get("/admin/api/routes")
async def list_routes(request: Request):
    require_admin(request)
    rows = db.query(
        """SELECT r.*, s.name AS site_name, s.enabled AS site_enabled, s.priority AS site_priority
           FROM routes r JOIN sites s ON s.id = r.site_id
           ORDER BY r.model, s.priority, s.id"""
    )
    out = []
    for row in rows:
        d = db.rowdict(row)
        c = db.query_one(
            "SELECT count FROM daily_counters WHERE day=? AND site_id=? AND model=?",
            (engine.today(), d["site_id"], d["model"]),
        )
        d["today_count"] = int(c["count"]) if c else 0
        out.append(d)
    return out


@app.post("/admin/api/routes")
async def create_route(request: Request, payload: dict = Body(...)):
    require_admin(request)
    try:
        site_id = int(payload["site_id"])
    except Exception:
        raise HTTPException(400, "请选择站点")
    model = (payload.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "模型名必填")
    upstream_model = (payload.get("upstream_model") or "").strip()
    daily_limit = int(payload.get("daily_limit") or 0)
    try:
        cur = db.execute(
            """INSERT INTO routes(site_id, model, upstream_model, enabled, daily_limit)
               VALUES (?,?,?,1,?)
               ON CONFLICT(site_id, model) DO UPDATE
               SET upstream_model=excluded.upstream_model, daily_limit=excluded.daily_limit, enabled=1""",
            (site_id, model, upstream_model, daily_limit),
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"id": cur.lastrowid}


@app.put("/admin/api/routes/{route_id}")
async def update_route(route_id: int, request: Request, payload: dict = Body(...)):
    require_admin(request)
    fields, values = [], []
    if "upstream_model" in payload:
        fields.append("upstream_model=?")
        values.append((payload.get("upstream_model") or "").strip())
    if "daily_limit" in payload:
        fields.append("daily_limit=?")
        values.append(int(payload.get("daily_limit") or 0))
    if "enabled" in payload:
        fields.append("enabled=?")
        values.append(1 if payload["enabled"] else 0)
    if not fields:
        return {"ok": True}
    values.append(route_id)
    db.execute(f"UPDATE routes SET {', '.join(fields)} WHERE id=?", values)
    return {"ok": True}


@app.delete("/admin/api/routes/{route_id}")
async def delete_route(route_id: int, request: Request):
    require_admin(request)
    db.execute("DELETE FROM routes WHERE id=?", (route_id,))
    return {"ok": True}


# -------- API Key

@app.get("/admin/api/keys")
async def list_keys(request: Request):
    require_admin(request)
    rows = db.query("SELECT * FROM api_keys ORDER BY id DESC")
    out = []
    for row in rows:
        d = db.rowdict(row)
        c = db.query_one(
            "SELECT count FROM key_counters WHERE day=? AND key_id=?", (engine.today(), d["id"])
        )
        d["today_count"] = int(c["count"]) if c else 0
        out.append(d)
    return out


@app.post("/admin/api/keys")
async def create_key(request: Request, payload: dict = Body(...)):
    require_admin(request)
    name = (payload.get("name") or "").strip() or "未命名"
    key = security.gen_api_key()
    db.execute(
        """INSERT INTO api_keys(key, name, enabled, daily_limit, note, created_at)
           VALUES (?,?,1,?,?,?)""",
        (key, name, int(payload.get("daily_limit") or 0), payload.get("note") or "", time.time()),
    )
    return {"key": key, "name": name}


@app.put("/admin/api/keys/{key_id}")
async def update_key(key_id: int, request: Request, payload: dict = Body(...)):
    require_admin(request)
    fields, values = [], []
    if "name" in payload:
        fields.append("name=?")
        values.append(str(payload["name"]).strip() or "未命名")
    if "daily_limit" in payload:
        fields.append("daily_limit=?")
        values.append(int(payload.get("daily_limit") or 0))
    if "enabled" in payload:
        fields.append("enabled=?")
        values.append(1 if payload["enabled"] else 0)
    if not fields:
        return {"ok": True}
    values.append(key_id)
    db.execute(f"UPDATE api_keys SET {', '.join(fields)} WHERE id=?", values)
    return {"ok": True}


@app.delete("/admin/api/keys/{key_id}")
async def delete_key(key_id: int, request: Request):
    require_admin(request)
    db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    return {"ok": True}


# -------- 日志 / 统计

@app.get("/admin/api/logs")
async def list_logs(request: Request, site_id: int = 0, limit: int = 100, offset: int = 0):
    require_admin(request)
    limit = max(1, min(500, limit))
    if site_id:
        rows = db.query(
            "SELECT * FROM request_logs WHERE site_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (site_id, limit, offset),
        )
    else:
        rows = db.query(
            "SELECT * FROM request_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )
    return [db.rowdict(r) for r in rows]


@app.delete("/admin/api/logs")
async def clear_logs(request: Request):
    require_admin(request)
    db.execute("DELETE FROM request_logs")
    return {"ok": True}


@app.get("/admin/api/stats")
async def stats(request: Request, days: int = 7):
    require_admin(request)
    days = max(1, min(90, days))
    since = time.time() - days * 86400

    totals = db.query_one(
        """SELECT COUNT(*) AS req, COALESCE(SUM(ok),0) AS ok, COALESCE(SUM(total_tokens),0) AS tok
           FROM request_logs WHERE ts >= ?""",
        (since,),
    )
    today_row = db.query_one(
        "SELECT COUNT(*) AS c FROM request_logs WHERE ts >= ?", (time.time() - 86400,)
    )
    daily_rows = db.query(
        """SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d,
                  COUNT(*) AS req, COALESCE(SUM(ok),0) AS ok, COALESCE(SUM(total_tokens),0) AS tok
           FROM request_logs WHERE ts >= ? GROUP BY d""",
        (since,),
    )
    daily_map = {r["d"]: r for r in daily_rows}

    today = datetime.date.today()
    daily = []
    for i in range(days - 1, -1, -1):
        day = (today - datetime.timedelta(days=i)).isoformat()
        r = daily_map.get(day)
        daily.append({
            "day": day,
            "requests": int(r["req"]) if r else 0,
            "ok": int(r["ok"]) if r else 0,
            "tokens": int(r["tok"]) if r else 0,
        })

    sites = []
    for row in db.query("SELECT * FROM sites ORDER BY priority, id"):
        d = db.rowdict(row)
        c = db.query_one(
            "SELECT COALESCE(SUM(count),0) AS c FROM daily_counters WHERE day=? AND site_id=?",
            (engine.today(), d["id"]),
        )
        req = db.query_one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(ok),0) AS ok
               FROM request_logs WHERE site_id=? AND ts >= ?""",
            (d["id"], since),
        )
        d["today_total"] = int(c["c"]) if c else 0
        d["period_requests"] = int(req["n"]) if req else 0
        d["period_ok"] = int(req["ok"]) if req else 0
        d["circuit_active"] = bool(d["circuit_until"] and d["circuit_until"] > time.time())
        d.pop("api_key", None)
        sites.append(d)

    return {
        "totals": {
            "requests": int(totals["req"]) if totals else 0,
            "ok": int(totals["ok"]) if totals else 0,
            "tokens": int(totals["tok"]) if totals else 0,
            "today_requests": int(today_row["c"]) if today_row else 0,
            "sites": len(sites),
            "models": len(engine.available_models()),
        },
        "daily": daily,
        "sites": sites,
    }


# -------- 设置

SETTING_KEYS = (
    "strategy", "max_attempts", "circuit_threshold", "circuit_cooldown",
    "inject_stream_usage", "quota_check_interval",
    "auto_sync_models", "auto_sync_interval",
)


@app.get("/admin/api/settings")
async def get_settings(request: Request):
    require_admin(request)
    return {k: db.get_setting(k, DEFAULT_SETTINGS.get(k)) for k in SETTING_KEYS}


@app.put("/admin/api/settings")
async def put_settings(request: Request, payload: dict = Body(...)):
    require_admin(request)
    for k, v in payload.items():
        if k in SETTING_KEYS:
            db.set_setting(k, v)
    return await get_settings(request)


# ---------------------------------------------------------------- 页面

@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": time.time()}


@app.get("/")
async def root():
    return RedirectResponse("/admin")


@app.get("/admin")
async def admin_page():
    return FileResponse(STATIC_DIR / "index.html")
