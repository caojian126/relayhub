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


class UpstreamRejected(UpstreamFailed):
    """上游明确拒绝了这次请求，但这不是节点的故障。

    例如上下文超长、参数非法、内容审核。这种情况**不换节点**，直接把上游的
    原始错误和状态码还给客户端，免得拿同一个错误把别的站点全试一遍、白白
    消耗它们的额度。

    继承自 UpstreamFailed，这样各入口原有的 `except UpstreamFailed` 依然兜得住；
    需要精确处理的调用方把 `except UpstreamRejected` 写在前面即可。
    """

    def __init__(self, status, body, site_name="", kind="bad_request"):
        super().__init__(f"{site_name}: HTTP {status} {body}")
        self.status = int(status or 400)
        self.body = body or ""
        self.site_name = site_name
        self.kind = kind


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
              cached=0, endpoint="openai", switches=0, stream_phase=""):
    """写一条请求日志。

    stream_phase 用来区分流式请求的三种收尾方式：
      ""      非流式
      "ok"    流式正常结束
      "pre"   开始输出之前就失败，已经切换到别的节点
      "post"  已经把内容发给客户端之后中途断流，按设计不重试、不换节点
    """
    u = usage or {}
    db.execute(
        """INSERT INTO request_logs
           (ts, site_id, site_name, model, upstream_model, key_id, key_name, attempt, ok,
            cached, endpoint, status_code, latency_ms,
            prompt_tokens, completion_tokens, total_tokens, stream, switches, stream_phase, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            stream, int(switches or 0), str(stream_phase or ""), str(error)[:1000],
        ),
    )


# ------------------------------------------------------------------ 候选

def _candidates(model):
    """取出这个统一模型下可以尝试的节点，并按策略排好序。"""
    max_attempts = int(db.get_setting("max_attempts", 3) or 3)
    cands = engine.get_candidates(model)
    if not cands:
        raise NoUpstream(
            f"没有可用上游支持模型 {model}",
            available=engine.public_models() or engine.available_models(),
        )
    engine.sort_candidates(cands, engine.group_strategy(model))
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
    switches = 0

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
            latency = int((time.time() - t0) * 1000)
            engine.record_failure(site["site_id"], e)
            engine.record_node_failure(site.get("route_id"), f"连接失败: {e}")
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                      latency_ms=latency, switches=switches, error=f"连接失败: {e}")
            last_err = f"{site['name']}: {e}"
            attempts.append(last_err)
            switches += 1
            continue

        latency = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception as e:
                engine.record_failure(site["site_id"], f"返回非 JSON: {e}")
                engine.record_node_failure(site.get("route_id"), "返回非 JSON")
                write_log(site=site, model=model, upstream_model=payload.get("model"),
                          key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                          status_code=200, latency_ms=latency, switches=switches,
                          error=f"返回非 JSON: {e}")
                last_err = f"{site['name']}: 返回非 JSON"
                attempts.append(last_err)
                switches += 1
                continue
            engine.record_success(site["site_id"])
            engine.record_node_success(site.get("route_id"), latency)
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=1, endpoint=endpoint,
                      status_code=200, latency_ms=latency, usage=data.get("usage"),
                      switches=switches)
            store(key, body, data, stream=False)
            return data, False

        err_text = (r.text or "")[:500]
        retryable, kind = engine.classify_error(r.status_code, err_text)

        if not retryable:
            # 这次请求本身的问题（上下文超长 / 参数非法 / 内容审核），
            # 换哪个站都是同样的结果，不浪费其它站点的额度，直接还给客户端。
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                      status_code=r.status_code, latency_ms=latency, switches=switches,
                      error=err_text or f"HTTP {r.status_code}")
            raise UpstreamRejected(r.status_code, err_text, site["name"], kind)

        engine.record_failure(site["site_id"], f"{r.status_code} {err_text}")
        engine.record_node_failure(site.get("route_id"), f"{r.status_code} {err_text}")
        write_log(site=site, model=model, upstream_model=payload.get("model"),
                  key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                  status_code=r.status_code, latency_ms=latency, switches=switches,
                  error=err_text or f"HTTP {r.status_code}")
        last_err = f"{site['name']}: HTTP {r.status_code} {err_text}"
        attempts.append(last_err)
        switches += 1

    raise UpstreamFailed(f"所有上游均失败。最后错误：{last_err}", attempts)


# ================================================================== 流式

def _sse_bytes(obj):
    """把一个 JSON 对象序列化成一段标准 SSE data 行。"""
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


async def stream(client, body, key_info, endpoint="openai", holder=None, rewrite=False):
    """向上游发起流式请求，逐段吐出 StreamEvent。

    分两个阶段，分界线是「有没有往客户端吐过字节」：

      阶段一（还没开吐）：连接失败 / 超时 / HTTP 错误 / 还没输出就断了
          -> 允许换下一个节点，日志记 stream_phase="pre"

      阶段二（只要吐过一个字节）：立刻锁定当前节点
          -> 中途断流不重试、不换节点，正常结束连接，日志记 stream_phase="post"
             这是设计而不是 bug：换节点会产生重复内容或两个模型的答案拼接。

    rewrite=True 时会把上游回包里的 model 字段改写成客户端请求的统一模型名
    （让客户端始终只看到 auto 这类统一模型名），并负责补发 [DONE]。
    """
    holder = holder if holder is not None else {}
    holder.update(ok=False, final=None, site=None, phase="", switches=0, breaker="")

    model = body.get("model")
    cands, max_attempts = _candidates(model)
    inject_usage = bool(db.get_setting("inject_stream_usage", True))
    do_rewrite = bool(rewrite) and bool(db.get_setting("rewrite_model", True))
    allow_switch = bool(db.get_setting("switch_before_output", True))

    attempts = []
    last_err = "未知错误"
    switches = 0
    tried = 0

    for site in cands:
        if tried >= max_attempts:
            break
        tried += 1
        attempt = tried

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

        # ============== 阶段一：还没向客户端输出过任何字节 ==============
        try:
            req = client.build_request(
                "POST",
                engine.norm_base(site["base_url"]) + "/chat/completions",
                json=payload,
                headers=engine.upstream_headers(site),
            )
            resp = await client.send(req, stream=True)
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            engine.record_failure(site["site_id"], e)
            engine.record_node_failure(site.get("route_id"), f"连接失败: {e}")
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                      stream=1, stream_phase="pre", latency_ms=latency,
                      switches=switches, error=f"连接失败: {e}")
            last_err = f"{site['name']}: {e}"
            attempts.append(last_err)
            switches += 1
            continue

        if resp.status_code != 200:
            try:
                txt = (await resp.aread()).decode("utf-8", "ignore")[:500]
            except Exception:
                txt = ""
            await resp.aclose()
            latency = int((time.time() - t0) * 1000)
            retryable, kind = engine.classify_error(resp.status_code, txt)

            if not retryable:
                # 请求本身的问题，换节点也是同样结果，直接还给客户端
                write_log(site=site, model=model, upstream_model=payload.get("model"),
                          key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                          stream=1, stream_phase="pre", status_code=resp.status_code,
                          latency_ms=latency, switches=switches,
                          error=txt or f"HTTP {resp.status_code}")
                raise UpstreamRejected(resp.status_code, txt, site["name"], kind)

            engine.record_failure(site["site_id"], f"{resp.status_code} {txt}")
            engine.record_node_failure(site.get("route_id"), f"{resp.status_code} {txt}")
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                      stream=1, stream_phase="pre", status_code=resp.status_code,
                      latency_ms=latency, switches=switches,
                      error=txt or f"HTTP {resp.status_code}")
            last_err = f"{site['name']}: HTTP {resp.status_code} {txt}"
            attempts.append(last_err)
            switches += 1
            continue

        # ============== 阶段二：拿到 200，开始转发 ==============
        # 从这里开始，只要往客户端吐过一个字节，就锁定这个节点，绝不再换。
        engine.record_success(site["site_id"])
        parser = SSEParser()
        acc = Accumulator(model)
        started = False      # 有没有已经吐给客户端字节
        broken = False
        break_err = ""

        try:
            async for chunk in resp.aiter_bytes():
                objs = parser.feed(chunk)
                for obj in objs:
                    acc.feed(obj)

                if not do_rewrite:
                    if chunk:
                        started = True
                        yield StreamEvent(chunk, None)
                    continue

                # 改写 model：按对象重新序列化，仍是标准 SSE，客户端无感
                for obj in objs:
                    if isinstance(obj, dict) and obj.get("model"):
                        obj["model"] = model
                    started = True
                    yield StreamEvent(_sse_bytes(obj), None)
        except Exception as e:
            broken = True
            break_err = f"传输中断: {e}"
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass

        latency = int((time.time() - t0) * 1000)

        if broken:
            if started:
                # 已经开始输出 -> 按设计不重试、不换节点，只记录「流式输出后断流」
                engine.record_failure(site["site_id"], break_err)
                engine.record_node_failure(site.get("route_id"), break_err)
                write_log(site=site, model=model, upstream_model=payload.get("model"),
                          key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                          stream=1, stream_phase="post", status_code=200,
                          latency_ms=latency, switches=switches, error=break_err)
                holder["phase"] = "post"
                holder["switches"] = switches
                holder["breaker"] = break_err
                return
            # 还没吐过任何字节 -> 这次断流等价于连接失败，可以换下一个节点
            engine.record_failure(site["site_id"], break_err)
            engine.record_node_failure(site.get("route_id"), break_err)
            write_log(site=site, model=model, upstream_model=payload.get("model"),
                      key_info=key_info, attempt=attempt, ok=0, endpoint=endpoint,
                      stream=1, stream_phase="pre", status_code=200,
                      latency_ms=latency, switches=switches, error=break_err)
            last_err = f"{site['name']}: {break_err}"
            attempts.append(last_err)
            switches += 1
            if not allow_switch:
                break
            continue

        # ---- 正常结束 ----
        if do_rewrite:
            yield StreamEvent(b"data: [DONE]\n\n", None)
        final = acc.result()
        engine.record_node_success(site.get("route_id"), latency)
        write_log(site=site, model=model, upstream_model=payload.get("model"),
                  key_info=key_info, attempt=attempt, ok=1, endpoint=endpoint,
                  stream=1, stream_phase="ok", status_code=200,
                  latency_ms=latency, usage=final.get("usage"), switches=switches)
        holder["final"] = final
        holder["ok"] = True
        holder["site"] = site["name"]
        holder["phase"] = "ok"
        holder["switches"] = switches
        return

    raise UpstreamFailed(f"所有上游均失败。最后错误：{last_err}", attempts)
