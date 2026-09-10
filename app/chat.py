"""统一的请求执行器。

所有入口先把请求翻译成「统一 OpenAI 请求体」，再交给这里：
  * 缓存查询 / 回写
  * 候选筛选与排序、失败自动切换、熔断
  * 用量与日志落库
"""

import json
import time

from . import cache, db, engine


class NoUpstream(Exception):
    def __init__(self, message, available=None):
        super().__init__(message)
        self.available = available or []


class UpstreamFailed(Exception):
    def __init__(self, message, attempts=None):
        super().__init__(message)
        self.attempts = attempts or []


# ------------------------------------------------------------------ SSE 解析

class SSEParser:
    """从字节流里切出完整的 SSE data 行。"""

    def __init__(self):
        self.buf = b""

    def feed(self, chunk):
        self.buf += chunk
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                out.append(json.loads(data))
            except Exception:
                continue
        if len(self.buf) > 2_000_000:
            self.buf = self.buf[-10000:]
        return out


class Accumulator:
    """把流式增量拼回一个完整的 chat.completion 对象。"""

    def __init__(self, model):
        self.model = model
        self.id = None
        self.created = None
        self.content = []
        self.tools = {}
        self.finish = None
        self.usage = None
        self.role = "assistant"

    def feed(self, obj):
        if not isinstance(obj, dict):
            return
        if obj.get("id"):
            self.id = obj["id"]
        if obj.get("created"):
            self.created = obj["created"]
        if obj.get("model"):
            self.model = obj["model"]
        if isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]
        choices = obj.get("choices")
        if not isinstance(choices, list):
            return
        for ch in choices:
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta") or {}
            if delta.get("role"):
                self.role = delta["role"]
            text = delta.get("content")
            if isinstance(text, str) and text:
                self.content.append(text)
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index")
                if not isinstance(idx, int):
                    idx = 0
                slot = self.tools.setdefault(idx, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                self.finish = ch["finish_reason"]

    def result(self):
        message = {"role": self.role, "content": "".join(self.content)}
        if self.tools:
            message["tool_calls"] = [self.tools[k] for k in sorted(self.tools)]
            if not message["content"]:
                message["content"] = None
        return {
            "id": self.id or "chatcmpl-relayhub",
            "object": "chat.completion",
            "created": self.created or int(time.time()),
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self.finish or "stop",
            }],
            "usage": self.usage or {},
        }


class StreamEvent:
    __slots__ = ("raw", "objs")

    def __init__(self, raw, objs):
        self.raw = raw
        self.objs = objs


# ------------------------------------------------------------------ 日志

def write_log(*, site=None, model="", upstream_model="", key_info=None, attempt=1, ok=0,
              status_code=None, latency_ms=0, usage=None, stream=0, error="",
              cached=0, endpoint="openai"):
    u = usage or {}
    db.execute(
        """INSERT INTO request_logs
           (ts, site_id, site_name, model, upstream_model, key_id, key_name, attempt, ok,
            cached, endpoint, status_code, latency_ms,
            prompt_tokens, completion_tokens, total_tokens, stream, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            time.time(),
            site["site_id"] if site else None,
            site["name"] if site else "",
            model,
            upstream_model,
            key_info.get("id") if key_info else None,
            key_info.get("name", "") if key_info else "",
            attempt, ok, cached, endpoint, status_code, latency_ms,
            int(u.get("prompt_tokens") or 0),
            int(u.get("completion_tokens") or 0),
            int(u.get("total_tokens") or 0),
            stream, str(error)[:1000],
        ),
    )


# ------------------------------------------------------------------ 候选

def _candidates(model):
    strategy = db.get_setting("strategy", "balanced")
    max_attempts = int(db.get_setting("max_attempts", 3) or 3)
    cands = engine.get_candidates(model)
    if not cands:
        raise NoUpstream(
            f"没有可用上游支持模型 {model}",
            available=engine.available_models(),
        )
    engine.sort_candidates(cands, strategy)
    return cands, max_attempts


def _upstream_payload(body, site):
    payload = dict(body)
    if site.get("upstream_model"):
        payload["model"] = site["upstream_model"]
    return payload


# ------------------------------------------------------------------ 缓存

def cache_key_for(endpoint, body):
    """返回缓存 key；None 表示这个请求不走缓存。"""
    if not cache.allowed(endpoint):
        return None
    if not cache.is_cacheable(body):
        return None
    return cache.make_key(body)


def lookup(key):
    return cache.get(key) if key else None


def store(key, body, unified_json, stream=False):
    if not key:
        return False
    return cache.put(key, body.get("model"), body, unified_json, stream=stream)


def _usage_of(unified):
    if isinstance(unified, dict):
        u = unified.get("usage")
        if isinstance(u, dict):
            return u
    return None


# ================================================================== 非流式

async def complete(client, body, key_info, endpoint="openai"):
    """执行一次非流式请求，返回 (unified_json, from_cache)。"""
    model = body.get("model")

    key = cache_key_for(endpoint, body)
    hit = lookup(key)
    if hit:
        write_log(model=model, usage=_usage_of(hit["response_json"]), ok=1, cached=1,
                  endpoint=endpoint, latency_ms=0, key_info=key_info, stream=0)
        return hit["response_json"], True

    cands, max_attempts = _candidates(model)
    last_err = "未知错误"
    attempts = []

    for attempt, site in enumerate(cands[:max_attempts], start=1):
        payload = _upstream_payload(body, site)
        payload.pop("stream", None)
        payload.pop("stream_options", None)
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
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                      latency_ms=int((time.time() - t0) * 1000), error=f"连接失败: {e}")
            last_err = f"{site['name']}: {e}"
            attempts.append(last_err)
            continue

        latency = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception as e:
                engine.record_failure(site["site_id"], f"返回非 JSON: {e}")
                write_log(site=site, model=model, upstream_model=payload.get("model"),
                          key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                          status_code=200, latency_ms=latency, error="返回非 JSON")
                last_err = f"{site['name']}: 返回非 JSON"
                attempts.append(last_err)
                continue
            engine.record_success(site["site_id"])
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=1, endpoint=endpoint,
                      status_code=200, latency_ms=latency, usage=data.get("usage"))
            store(key, body, data, stream=False)
            return data, False

        err_text = (r.text or "")[:500]
        engine.record_failure(site["site_id"], f"{r.status_code} {err_text}")
        write_log(site=site, model=model, upstream_model=payload.get("model"),
                  key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                  status_code=r.status_code, latency_ms=latency, error=err_text)
        last_err = f"{site['name']}: HTTP {r.status_code} {err_text}"
        attempts.append(last_err)

    raise UpstreamFailed(f"所有上游均失败。最后错误：{last_err}", attempts)


# ================================================================== 流式

async def stream(client, body, key_info, endpoint="openai", holder=None):
    """向上游发起流式请求，逐段吐出 StreamEvent。

    成功结束后会把完整响应写入 holder["final"]，供上层回写缓存。
    注意：一旦开始往客户端吐字节，就不能再切换上游。
    """
    holder = holder if holder is not None else {}
    holder["ok"] = False
    holder["final"] = None
    holder["site"] = None

    model = body.get("model")
    cands, max_attempts = _candidates(model)
    inject_usage = bool(db.get_setting("inject_stream_usage", True))
    last_err = "未知错误"
    attempts = []

    for site in cands:
        if len(attempts) >= max_attempts:
            break
        attempt = len(attempts) + 1

        payload = _upstream_payload(body, site)
        payload["stream"] = True
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
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                      stream=1, latency_ms=int((time.time() - t0) * 1000),
                      error=f"连接失败: {e}")
            last_err = f"{site['name']}: {e}"
            attempts.append(last_err)
            continue

        if resp.status_code != 200:
            try:
                txt = (await resp.aread()).decode("utf-8", "ignore")[:500]
            except Exception:
                txt = ""
            await resp.aclose()
            engine.record_failure(site["site_id"], f"{resp.status_code} {txt}")
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                      stream=1, status_code=resp.status_code,
                      latency_ms=int((time.time() - t0) * 1000), error=txt)
            last_err = f"{site['name']}: HTTP {resp.status_code} {txt}"
            attempts.append(last_err)
            continue

        # ---- 拿到 200，开始转发（此后不可再切换）----
        engine.record_success(site["site_id"])
        parser = SSEParser()
        acc = Accumulator(model)
        broken = False
        try:
            async for chunk in resp.aiter_bytes():
                for obj in parser.feed(chunk):
                    acc.feed(obj)
                yield StreamEvent(chunk, None)
        except Exception as e:
            broken = True
            last_err = f"{site['name']} 传输中断: {e}"
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                      stream=1, status_code=200,
                      latency_ms=int((time.time() - t0) * 1000),
                      error=f"传输中断: {e}")
        finally:
            await resp.aclose()

        if not broken:
            final = acc.result()
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=1, endpoint=endpoint,
                      stream=1, status_code=200,
                      latency_ms=int((time.time() - t0) * 1000),
                      usage=final.get("usage"))
            holder["final"] = final
            holder["ok"] = True
            holder["site"] = site["name"]
        return

    raise UpstreamFailed(f"所有上游均失败。最后错误：{last_err}", attempts)
