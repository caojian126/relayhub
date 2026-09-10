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

from . import cache, chat, db, engine, formats as fm, models_sync, quota, security
from .config import CONNECT_TIMEOUT, REQUEST_TIMEOUT
from .db import DEFAULT_SETTINGS

STATIC_DIR = Path(__file__).parent / "static"
VERSION = "0.4.1"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    security.seed_admin()
    cache.purge_expired()
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        follow_redirects=True,
    )
    tasks = [
        asyncio.create_task(quota.background_loop()),
        asyncio.create_task(models_sync.background_loop(app.state.client)),
        asyncio.create_task(_cache_janitor()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await app.state.client.aclose()


async def _cache_janitor():
    while True:
        await asyncio.sleep(600)
        try:
            cache.purge_expired()
        except Exception:
            pass


app = FastAPI(title="RelayHub", version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------------------------------------------------------------- 鉴权

def _bearer(request: Request):
    auth = request.headers.get("Authorization", "") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    key = request.headers.get("x-api-key", "") or ""
    if key:
        return key.strip()
    return request.query_params.get("key", "") or ""


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


# ================================================================ 统一入口

async def _read_json(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    return body


def _error_payload(endpoint, message):
    if endpoint == "anthropic":
        return fm.sse("error", {"type": "error",
                                "error": {"type": "api_error", "message": message}})
    if endpoint == "gemini":
        return fm.sse_data({"error": {"code": 502, "message": message,
                                        "status": "UNAVAILABLE"}})
    return fm.sse_data({"error": {"message": message, "type": "relayhub_error"}})


async def handle(request: Request, endpoint, model_action=None):
    body = await _read_json(request)
    key_info = auth_api_key(request)

    forced_stream = None
    if endpoint == "gemini":
        name, _, action = (model_action or "").partition(":")
        if name:
            body["model"] = name
        forced_stream = action == "streamGenerateContent"

    model = body.get("model")
    if not model:
        raise HTTPException(400, "缺少 model 字段")

    want_stream = bool(forced_stream) or bool(body.get("stream"))

    if endpoint == "gemini":
        unified = fm.gemini_to_openai(body, model)
    else:
        try:
            unified = fm.CONVERTERS[endpoint](body)
        except Exception as e:
            raise HTTPException(400, f"请求转换失败：{e}")

    if not unified.get("model"):
        unified["model"] = model
    if not unified.get("messages"):
        raise HTTPException(400, "messages 为空")

    incr_key_counter(key_info["id"] if key_info else None)
    client = request.app.state.client
    key = chat.cache_key_for(endpoint, unified)

    if key:
        hit = chat.lookup(key)
        if hit:
            unified_resp = hit["response_json"]
            chat.write_log(model=model, usage=chat._usage_of(unified_resp), ok=1, cached=1,
                           endpoint=endpoint, latency_ms=0, key_info=key_info,
                           stream=1 if want_stream else 0)
            if want_stream:
                return StreamingResponse(
                    _render_cached_stream(endpoint, unified_resp, model),
                    media_type="text/event-stream", headers=SSE_HEADERS,
                )
            return JSONResponse(fm.RENDERERS[endpoint](unified_resp, model))

    if not want_stream:
        try:
            unified_resp, _ = await chat.complete(client, unified, key_info, endpoint)
        except chat.NoUpstream as e:
            avail = "、".join(e.available) if e.available else "（空）"
            raise HTTPException(503, f"没有可用上游支持模型 {model}。已配置模型：{avail}")
        except chat.UpstreamFailed as e:
            raise HTTPException(502, str(e))
        return JSONResponse(fm.RENDERERS[endpoint](unified_resp, model))

    return StreamingResponse(
        _render_stream(endpoint, client, unified, key_info, model, key),
        media_type="text/event-stream", headers=SSE_HEADERS,
    )


async def _render_stream(endpoint, client, unified, key_info, model, key):
    holder = {}
    finish_reason = "stop"

    if endpoint == "openai":
        try:
            async for ev in chat.stream(client, unified, key_info, endpoint, holder):
                yield ev.raw
        except (chat.UpstreamFailed, chat.NoUpstream) as e:
            yield _error_payload(endpoint, str(e))
        else:
            chat.store(key, unified, holder.get("final"), stream=True)
        return

    parser = chat.SSEParser()

    if endpoint == "anthropic":
        yield fm.sse("message_start", {"type": "message_start", "message": {
            "id": "msg_relayhub", "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})
        yield fm.sse("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}})
        try:
            async for ev in chat.stream(client, unified, key_info, endpoint, holder):
                for obj in parser.feed(ev.raw):
                    for ch in obj.get("choices") or []:
                        d = ch.get("delta") or {}
                        if ch.get("finish_reason"):
                            finish_reason = ch["finish_reason"]
                        text = d.get("content")
                        if isinstance(text, str) and text:
                            yield fm.sse("content_block_delta", {
                                "type": "content_block_delta", "index": 0,
                                "delta": {"type": "text_delta", "text": text}})
        except (chat.UpstreamFailed, chat.NoUpstream) as e:
            yield fm.sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            yield _error_payload(endpoint, str(e))
            return
        yield fm.sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        final = holder.get("final") or {}
        usage = final.get("usage") or {}
        stop = {"stop": "end_turn", "length": "max_tokens",
                "tool_calls": "tool_use"}.get(finish_reason, "end_turn")
        yield fm.sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": int(usage.get("completion_tokens") or 0)}})
        yield fm.sse("message_stop", {"type": "message_stop"})
        chat.store(key, unified, holder.get("final"), stream=True)
        return

    if endpoint == "gemini":
        try:
            async for ev in chat.stream(client, unified, key_info, endpoint, holder):
                for obj in parser.feed(ev.raw):
                    for ch in obj.get("choices") or []:
                        d = ch.get("delta") or {}
                        text = d.get("content")
                        if isinstance(text, str) and text:
                            yield fm.sse_data({"candidates": [{
                                "content": {"parts": [{"text": text}], "role": "model"},
                                "index": 0}]})
        except (chat.UpstreamFailed, chat.NoUpstream) as e:
            yield _error_payload(endpoint, str(e))
            return
        final = holder.get("final")
        if isinstance(final, dict):
            yield fm.sse_data(fm.openai_to_gemini(final, model))
            chat.store(key, unified, final, stream=True)
        return

    # ---- Responses API ----
    rid = "resp_relayhub"
    created_at = int(time.time())
    base_resp = {"id": rid, "object": "response", "created_at": created_at,
                 "status": "in_progress", "model": model, "output": []}
    yield fm.sse("response.created", {"type": "response.created", "response": base_resp})
    yield fm.sse("response.in_progress", {"type": "response.in_progress", "response": base_resp})
    started = False
    try:
        async for ev in chat.stream(client, unified, key_info, endpoint, holder):
            for obj in parser.feed(ev.raw):
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    text = d.get("content")
                    if not isinstance(text, str) or not text:
                        continue
                    if not started:
                        started = True
                        yield fm.sse("response.output_item.added", {
                            "type": "response.output_item.added", "output_index": 0,
                            "item": {"id": "msg_relayhub", "type": "message",
                                     "status": "in_progress", "role": "assistant",
                                     "content": []}})
                        yield fm.sse("response.content_part.added", {
                            "type": "response.content_part.added", "item_id": "msg_relayhub",
                            "output_index": 0, "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []}})
                    yield fm.sse("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": "msg_relayhub",
                        "output_index": 0, "content_index": 0, "delta": text})
    except (chat.UpstreamFailed, chat.NoUpstream) as e:
        yield fm.sse("response.failed", {"type": "response.failed", "response": {
            "id": rid, "object": "response", "created_at": created_at,
            "status": "failed", "model": model, "output": [],
            "error": {"code": "upstream_error", "message": str(e)}}})
        return

    final = holder.get("final") or {}
    usage = final.get("usage") or {}
    text_part = ""
    if isinstance(final.get("choices"), list) and final["choices"]:
        text_part = (final["choices"][0].get("message") or {}).get("content") or ""

    if started:
        yield fm.sse("response.output_text.done", {
            "type": "response.output_text.done", "item_id": "msg_relayhub",
            "output_index": 0, "content_index": 0, "text": text_part})
        yield fm.sse("response.content_part.done", {
            "type": "response.content_part.done", "item_id": "msg_relayhub",
            "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": text_part, "annotations": []}})
        yield fm.sse("response.output_item.done", {
            "type": "response.output_item.done", "output_index": 0,
            "item": {"id": "msg_relayhub", "type": "message", "status": "completed",
                     "role": "assistant",
                     "content": [{"type": "output_text", "text": text_part,
                                  "annotations": []}]}})

    complete = fm.openai_to_responses(final, model)
    complete["id"] = rid
    complete["usage"] = {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    yield fm.sse("response.completed", {"type": "response.completed", "response": complete})
    chat.store(key, unified, final, stream=True)


async def _render_cached_stream(endpoint, unified_resp, model):
    """缓存命中时的流式回放（从完整响应合成）。"""
    if endpoint == "openai":
        text, tool_calls = fm.chunk_text(unified_resp)
        base = {
            "id": unified_resp.get("id") or "chatcmpl-relayhub",
            "object": "chat.completion.chunk",
            "created": unified_resp.get("created") or int(time.time()),
            "model": unified_resp.get("model") or model,
        }
        yield fm.sse_data({**base, "choices": [{
            "index": 0, "delta": {"role": "assistant", "content": ""},
            "finish_reason": None}]})
        for piece in fm.split_pieces(text):
            yield fm.sse_data({**base, "choices": [{
                "index": 0, "delta": {"content": piece}, "finish_reason": None}]})
        for i, tc in enumerate(tool_calls or []):
            fn = tc.get("function") or {}
            yield fm.sse_data({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": i, "id": tc.get("id"), "type": "function",
                "function": {"name": fn.get("name"),
                             "arguments": fn.get("arguments")}}]},
                "finish_reason": None}]})
        choice = fm._first_choice(unified_resp)
        yield fm.sse_data({**base, "choices": [{
            "index": 0, "delta": {},
            "finish_reason": choice.get("finish_reason") or "stop"}]})
        if unified_resp.get("usage"):
            yield fm.sse_data({**base, "choices": [], "usage": unified_resp["usage"]})
        yield b"data: [DONE]\n\n"
        return

    if endpoint == "anthropic":
        res = fm.openai_to_anthropic(unified_resp, model)
        yield fm.sse("message_start", {"type": "message_start", "message": {
            **res, "content": [], "stop_reason": None}})
        yield fm.sse("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}})
        text = "".join(b.get("text", "") for b in res["content"]
                       if b.get("type") == "text")
        for piece in fm.split_pieces(text):
            yield fm.sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": piece}})
        yield fm.sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield fm.sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": res["stop_reason"], "stop_sequence": None},
            "usage": {"output_tokens": res["usage"]["output_tokens"]}})
        yield fm.sse("message_stop", {"type": "message_stop"})
        return

    if endpoint == "gemini":
        text, _ = fm.chunk_text(unified_resp)
        for piece in fm.split_pieces(text):
            yield fm.sse_data({"candidates": [{
                "content": {"parts": [{"text": piece}], "role": "model"}, "index": 0}]})
        yield fm.sse_data(fm.openai_to_gemini(unified_resp, model))
        return

    rid = "resp_relayhub"
    full = fm.openai_to_responses(unified_resp, model)
    full["id"] = rid
    yield fm.sse("response.created", {"type": "response.created", "response": full})
    yield fm.sse("response.output_item.added", {
        "type": "response.output_item.added", "output_index": 0,
        "item": {"id": "msg_relayhub", "type": "message", "status": "in_progress",
                 "role": "assistant", "content": []}})
    yield fm.sse("response.content_part.added", {
        "type": "response.content_part.added", "item_id": "msg_relayhub",
        "output_index": 0, "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []}})
    for piece in fm.split_pieces(full.get("output_text") or ""):
        yield fm.sse("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": "msg_relayhub",
            "output_index": 0, "content_index": 0, "delta": piece})
    yield fm.sse("response.output_text.done", {
        "type": "response.output_text.done", "item_id": "msg_relayhub",
        "output_index": 0, "content_index": 0,
        "text": full.get("output_text") or ""})
    yield fm.sse("response.completed", {"type": "response.completed", "response": full})


# ================================================================ 四个入口

@app.post("/v1/chat/completions")
async def ep_openai(request: Request):
    return await handle(request, "openai")


@app.post("/v1/responses")
async def ep_responses(request: Request):
    return await handle(request, "responses")


@app.post("/v1/messages")
async def ep_anthropic(request: Request):
    return await handle(request, "anthropic")


@app.post("/v1beta/models/{model_action}")
async def ep_gemini_v1beta(model_action: str, request: Request):
    return await handle(request, "gemini", model_action)


@app.post("/v1/models/{model_action}")
async def ep_gemini_v1(model_action: str, request: Request):
    return await handle(request, "gemini", model_action)


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
            raise HTTPException(400, "面板密码由环境变量 ADMIN_PASSWORD 管理，请修改该环境变量后重启服务")
        if len(new_pw) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        if not security.verify_password(old_pw, row["password_hash"]):
            raise HTTPException(400, "原密码不正确")
        db.execute("UPDATE admins SET password_hash=? WHERE id=?",
                   (security.hash_password(new_pw), row["id"]))

    if new_username and new_username != row["username"]:
        if security.username_from_env():
            raise HTTPException(400, "面板账号由环境变量 ADMIN_USERNAME 管理，请修改该环境变量后重启服务")
        try:
            db.execute("UPDATE admins SET username=? WHERE id=?", (new_username, row["id"]))
        except Exception as e:
            raise HTTPException(400, f"修改失败：{e}")

    return {"ok": True}


# -------- 缓存管理

@app.get("/admin/api/cache")
async def cache_stats(request: Request):
    require_admin(request)
    return cache.stats()


@app.post("/admin/api/cache/clear")
async def cache_clear(request: Request):
    require_admin(request)
    cache.clear()
    return {"ok": True}


@app.post("/admin/api/cache/purge")
async def cache_purge(request: Request):
    require_admin(request)
    return {"ok": True, "removed": cache.purge_expired()}


# -------- 配置备份

@app.get("/admin/api/export")
async def export_config(request: Request):
    require_admin(request)
    sites = []
    for row in db.query("SELECT * FROM sites ORDER BY id"):
        d = db.rowdict(row)
        sites.append({
            "name": d["name"], "base_url": d["base_url"], "api_key": d["api_key"],
            "enabled": d["enabled"], "priority": d["priority"], "note": d["note"],
        })
    routes = [db.rowdict(r) for r in db.query(
        "SELECT site_id, model, upstream_model, enabled, daily_limit FROM routes")]
    keys = [db.rowdict(r) for r in db.query(
        "SELECT key, name, enabled, daily_limit, note FROM api_keys")]
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
                "UPDATE sites SET base_url=?, api_key=?, enabled=?, priority=?, note=? WHERE id=?",
                (base_url, api_key, enabled, priority, note, row["id"]))
            name_to_id[name] = row["id"]
        else:
            cur = db.execute(
                """INSERT INTO sites(name, base_url, api_key, enabled, priority, note, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (name, base_url, api_key, enabled, priority, note, time.time()))
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
        db.execute(
            """INSERT INTO routes(site_id, model, upstream_model, enabled, daily_limit)
               VALUES (?,?,?,?,?)
               ON CONFLICT(site_id, model) DO UPDATE SET
                 upstream_model=excluded.upstream_model,
                 daily_limit=excluded.daily_limit, enabled=excluded.enabled""",
            (name_to_id[site_name], model, (r.get("upstream_model") or "").strip(),
             1 if r.get("enabled", True) else 0, int(r.get("daily_limit") or 0)))
        added_routes += 1

    added_keys = 0
    for k in data.get("keys") or []:
        key = (k.get("key") or "").strip()
        if not key or db.query_one("SELECT 1 FROM api_keys WHERE key=?", (key,)):
            continue
        db.execute(
            """INSERT INTO api_keys(key, name, enabled, daily_limit, note, created_at)
               VALUES (?,?,?,?,?,?)""",
            (key, k.get("name") or "未命名", 1 if k.get("enabled", True) else 0,
             int(k.get("daily_limit") or 0), k.get("note") or "", time.time()))
        added_keys += 1

    for k, v in (data.get("settings") or {}).items():
        if k in DEFAULT_SETTINGS:
            db.set_setting(k, v)

    return {"ok": True, "sites": len(data.get("sites") or []),
            "routes": added_routes, "keys": added_keys}


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
            (engine.today(), d["id"]))
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
             int(payload.get("priority") or 100), payload.get("note") or "", time.time()))
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
    return await quota.check_site(
        request.app.state.client, site_id, site["base_url"], site["api_key"])


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


# -------- 路由

@app.get("/admin/api/routes")
async def list_routes(request: Request):
    require_admin(request)
    rows = db.query(
        """SELECT r.*, s.name AS site_name, s.enabled AS site_enabled,
                  s.priority AS site_priority
           FROM routes r JOIN sites s ON s.id = r.site_id
           ORDER BY r.model, s.priority, s.id""")
    out = []
    for row in rows:
        d = db.rowdict(row)
        c = db.query_one(
            "SELECT count FROM daily_counters WHERE day=? AND site_id=? AND model=?",
            (engine.today(), d["site_id"], d["model"]))
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
    try:
        cur = db.execute(
            """INSERT INTO routes(site_id, model, upstream_model, enabled, daily_limit)
               VALUES (?,?,?,1,?)
               ON CONFLICT(site_id, model) DO UPDATE
               SET upstream_model=excluded.upstream_model,
                   daily_limit=excluded.daily_limit, enabled=1""",
            (site_id, model, (payload.get("upstream_model") or "").strip(),
             int(payload.get("daily_limit") or 0)))
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
    out = []
    for row in db.query("SELECT * FROM api_keys ORDER BY id DESC"):
        d = db.rowdict(row)
        c = db.query_one(
            "SELECT count FROM key_counters WHERE day=? AND key_id=?",
            (engine.today(), d["id"]))
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
        (key, name, int(payload.get("daily_limit") or 0),
         payload.get("note") or "", time.time()))
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
            (site_id, limit, offset))
    else:
        rows = db.query(
            "SELECT * FROM request_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
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
        """SELECT COUNT(*) AS req, COALESCE(SUM(ok),0) AS ok,
                  COALESCE(SUM(total_tokens),0) AS tok, COALESCE(SUM(cached),0) AS ch
           FROM request_logs WHERE ts >= ?""", (since,))
    today_row = db.query_one(
        "SELECT COUNT(*) AS c, COALESCE(SUM(cached),0) AS ch FROM request_logs WHERE ts >= ?",
        (time.time() - 86400,))
    daily_rows = db.query(
        """SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d,
                  COUNT(*) AS req, COALESCE(SUM(ok),0) AS ok,
                  COALESCE(SUM(total_tokens),0) AS tok, COALESCE(SUM(cached),0) AS ch
           FROM request_logs WHERE ts >= ? GROUP BY d""", (since,))
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
            "cached": int(r["ch"]) if r else 0,
        })

    sites = []
    for row in db.query("SELECT * FROM sites ORDER BY priority, id"):
        d = db.rowdict(row)
        c = db.query_one(
            "SELECT COALESCE(SUM(count),0) AS c FROM daily_counters WHERE day=? AND site_id=?",
            (engine.today(), d["id"]))
        req = db.query_one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(ok),0) AS ok FROM request_logs
               WHERE site_id=? AND ts >= ?""", (d["id"], since))
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
            "cached": int(totals["ch"]) if totals else 0,
            "today_requests": int(today_row["c"]) if today_row else 0,
            "today_cached": int(today_row["ch"]) if today_row else 0,
            "sites": len(sites),
            "models": len(engine.available_models()),
        },
        "daily": daily,
        "sites": sites,
        "cache": cache.stats(),
    }


# -------- 设置

SETTING_KEYS = (
    "strategy", "max_attempts", "circuit_threshold", "circuit_cooldown",
    "inject_stream_usage", "quota_check_interval",
    "auto_sync_models", "auto_sync_interval",
    "cache_enabled", "cache_endpoints", "cache_only_deterministic",
    "cache_ttl", "cache_max_entries",
)


@app.get("/admin/api/settings")
async def get_settings(request: Request):
    require_admin(request)
    return {k: db.get_setting(k, DEFAULT_SETTINGS.get(k)) for k in SETTING_KEYS}


@app.put("/admin/api/settings")
async def put_settings(request: Request, payload: dict = Body(...)):
    require_admin(request)
    for k, v in payload.items():
        if k not in SETTING_KEYS:
            continue
        if k == "cache_endpoints":
            if not isinstance(v, list):
                v = [v]
            v = [x for x in v if x in fm.ENDPOINTS]
        db.set_setting(k, v)
    return await get_settings(request)


# ---------------------------------------------------------------- 页面

@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": VERSION, "time": time.time()}


@app.get("/")
async def root():
    return RedirectResponse("/admin")


@app.get("/admin")
async def admin_page():
    return FileResponse(STATIC_DIR / "index.html")
