"""RelayHub 端到端测试。

需要先跑起来：一个 RelayHub（默认 8099）+ 两个假中转站（8101 / 8102）。
用 tests/run_e2e.sh 一键起全套。

覆盖：站点 / 拉模型 / 统一模型 / 节点排序 / 网关 Key /
      普通请求 / 流式请求 / 故障切换 / 不可重试错误 / 流式断流 /
      /v1/models 只露统一模型 / 导出安全与完整 / 导入合并与覆盖
"""

import json
import os
import sys

import httpx

BASE = os.getenv("RH_BASE", "http://127.0.0.1:8099")
MOCK_A = os.getenv("MOCK_A", "http://127.0.0.1:8101")
MOCK_B = os.getenv("MOCK_B", "http://127.0.0.1:8102")
PW = os.getenv("RH_PASSWORD", "test123")

FAILED = []
TOKEN = ""
KEY = ""

c = httpx.Client(timeout=60)


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    line = f"  [{mark}] {name}"
    if extra:
        line += f"  -> {str(extra)[:300]}"
    print(line, flush=True)
    if not cond:
        FAILED.append(name)


def admin(path, method="GET", body=None):
    h = {}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    r = c.request(method, BASE + "/admin/api" + path, headers=h,
                  json=body if body is not None else None)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


def gw(path, method="POST", body=None, key=None):
    k = key if key is not None else KEY
    h = {}
    if k:
        h["Authorization"] = "Bearer " + k
    return c.request(method, BASE + "/v1" + path, headers=h,
                     json=body if body is not None else None)


def site_id_of(name):
    _, sites = admin("/sites")
    for s in sites:
        if s["name"] == name:
            return s["id"]
    return None


def make_group(gname, nodes, strategy="priority"):
    """nodes = [(site_name, upstream_model), ...] 按期望顺序。"""
    admin("/groups", "POST", {"name": gname, "strategy": strategy})
    ids = []
    for site_name, model in nodes:
        st, r = admin(f"/groups/{gname}/nodes", "POST",
                      {"site_id": site_id_of(site_name), "upstream_model": model})
        if st != 200:
            print(f"     节点添加失败 {site_name}/{model}: {r}", flush=True)
            return None
        ids.append(r["id"])
    admin(f"/groups/{gname}/order", "POST", {"ids": ids})
    return ids


# ======================================================================

def main():
    global TOKEN, KEY

    print("\n=== 0. 登录 / 体检 ===", flush=True)
    st, r = admin("/login", "POST", {"username": "admin", "password": PW})
    if st != 200:
        # 首次启动密码可能还没同步，重试一次
        st, r = admin("/login", "POST", {"username": "admin", "password": PW})
    check("管理员登录", st == 200 and "token" in (r if isinstance(r, dict) else {}), r)
    if st != 200:
        print("登录失败，后面的测试没法继续", flush=True)
        return 1
    TOKEN = r["token"]

    st, sysinfo = admin("/system")
    check("/admin/api/system 返回数据目录", st == 200 and "data_dir" in sysinfo, sysinfo)

    print("\n=== 1. 站点 ===", flush=True)
    for nm, url in (("A站", MOCK_A), ("B站", MOCK_B)):
        st, r = admin("/sites", "POST",
                      {"name": nm, "base_url": url, "api_key": "sk-mock-key-123456",
                       "priority": 100})
        check(f"新增站点 {nm}", st == 200, r)

    st, sites = admin("/sites")
    check("站点列表返回 2 个", st == 200 and len(sites) == 2, len(sites) if st == 200 else st)
    check("站点列表里的 Key 是打码的",
          all("api_key" not in s for s in sites)
          and any(s.get("api_key_masked") for s in sites),
          [s.get("api_key_masked") for s in sites])

    print("\n=== 2. 拉取模型 ===", flush=True)
    st, r = admin("/models/scan", "POST")
    ok_sites = [x for x in r if x["ok"]] if st == 200 else []
    check("扫描全部站点成功", st == 200 and len(ok_sites) == 2,
          [(x["site_name"], x["error"]) for x in r] if st == 200 else r)
    check("拉到 8 个模型", ok_sites and all(len(x["models"]) == 8 for x in ok_sites),
          [len(x["models"]) for x in ok_sites])

    # 只导入两个“正常”模型，验证多选导入
    aid = site_id_of("A站")
    st, r = admin("/models/import", "POST",
                  {"items": [{"site_id": aid, "model": "gpt-5"},
                             {"site_id": aid, "model": "claude-sonnet-4-5"}]})
    check("导入 2 个模型", st == 200 and r.get("added") == 2, r)
    st, r = admin("/models/import", "POST",
                  {"items": [{"site_id": aid, "model": "gpt-5"}]})
    check("重复导入不会重复添加", st == 200 and r.get("added") == 0, r)

    print("\n=== 3. 统一模型 + 节点排序 ===", flush=True)
    ids = make_group("auto", [("A站", "X-fail-500"), ("B站", "gpt-5")])
    check("创建统一模型 auto 并加 2 个节点", ids is not None and len(ids) == 2, ids)

    st, nodes = admin("/groups/auto/nodes")
    check("节点按拖拽顺序返回", st == 200 and len(nodes) == 2
          and nodes[0]["real_model"] == "X-fail-500"
          and nodes[1]["real_model"] == "gpt-5",
          [(n["site_name"], n["real_model"], n["node_priority"]) for n in nodes] if st == 200 else st)

    # 反过来排序，验证排序真的落库
    admin("/groups/auto/order", "POST", {"ids": [ids[1], ids[0]]})
    st, nodes = admin("/groups/auto/nodes")
    check("拖拽排序生效（顺序已反转）",
          st == 200 and nodes[0]["real_model"] == "gpt-5", 
          [n["real_model"] for n in nodes] if st == 200 else st)
    admin("/groups/auto/order", "POST", {"ids": ids})  # 还原，让坏节点在前

    print("\n=== 4. 网关 Key ===", flush=True)
    st, r = admin("/keys", "POST", {"name": "e2e", "daily_limit": 0})
    check("生成网关 Key", st == 200 and r.get("key"), r)
    KEY = r.get("key", "")

    print("\n=== 5. /v1/models 只暴露统一模型 ===", flush=True)
    r = gw("/models", "GET", key="")
    check("未带 Key 调用被拒", r.status_code == 401, r.status_code)
    r = gw("/models", "GET", key="sk-wrong-key")
    check("错误 Key 被拒", r.status_code == 401, r.status_code)
    r = gw("/models", "GET")
    ids_out = [m["id"] for m in r.json().get("data", [])]
    check("/v1/models 带 Key 正常", r.status_code == 200, r.status_code)
    check("列表包含 auto", "auto" in ids_out, ids_out)
    check("列表不含坏模型名 X-fail-500", "X-fail-500" not in ids_out, ids_out)
    check("列表不含上游真实模型 claude-sonnet-4-5",
          "claude-sonnet-4-5" not in ids_out, ids_out)

    print("\n=== 6. 普通请求 + 故障切换 ===", flush=True)
    r = gw("/chat/completions", "POST",
           {"model": "auto", "messages": [{"role": "user", "content": "你好"}]})
    check("auto 请求成功", r.status_code == 200, r.text[:200])
    if r.status_code == 200:
        d = r.json()
        content = d["choices"][0]["message"]["content"]
        check("故障切换到了 B站（内容带 8102）", "8102" in content, content)
        check("返回的 model 被改写回统一名 auto", d.get("model") == "auto", d.get("model"))

    st, logs = admin("/logs?limit=20")
    auto_logs = [x for x in logs if x["model"] == "auto"]
    check("日志里记了统一模型 auto", bool(auto_logs), len(auto_logs))
    check("日志里记了真实模型 X-fail-500 / gpt-5",
          any(x["upstream_model"] in ("X-fail-500", "gpt-5") for x in auto_logs),
          [x["upstream_model"] for x in auto_logs])
    check("日志里有故障切换次数 switches >= 1",
          any((x.get("switches") or 0) >= 1 for x in auto_logs),
          [x.get("switches") for x in auto_logs])

    print("\n=== 7. 不可重试错误不该切节点 ===", flush=True)
    make_group("badparam", [("A站", "X-fail-400"), ("B站", "gpt-5")])
    r = gw("/chat/completions", "POST",
           {"model": "badparam", "messages": [{"role": "user", "content": "hi"}]})
    check("400 参数错误原样返回 400（不是 502）", r.status_code == 400, r.status_code)
    check("错误正文来自上游", "temperature" in r.text, r.text[:200])
    st, logs = admin("/logs?limit=10")
    bp = [x for x in logs if x["model"] == "badparam"]
    check("没有切到 B站（只尝试了 1 次）",
          bool(bp) and all((x.get("switches") or 0) == 0 for x in bp),
          [(x.get("switches"), x.get("site_name")) for x in bp])

    print("\n=== 8. 模型不存在可重试 ===", flush=True)
    make_group("missing", [("A站", "X-missing"), ("B站", "gpt-5")])
    r = gw("/chat/completions", "POST",
           {"model": "missing", "messages": [{"role": "user", "content": "hi"}]})
    check("模型不存在时切到 B站并成功", r.status_code == 200
          and "8102" in r.text, r.status_code)

    print("\n=== 9. 流式请求 ===", flush=True)
    make_group("s1", [("B站", "gpt-5")])
    got_model_fields = set()
    body = ""
    with c.stream("POST", BASE + "/v1/chat/completions",
                  headers={"Authorization": "Bearer " + KEY},
                  json={"model": "s1", "stream": True,
                        "messages": [{"role": "user", "content": "流式测试"}]}) as resp:
        check("流式返回 200", resp.status_code == 200, resp.status_code)
        raw = ""
        for line in resp.iter_lines():
            raw += line + "\n"
            if line.startswith("data:"):
                p = line[5:].strip()
                if p and p != "[DONE]":
                    try:
                        o = json.loads(p)
                    except Exception:
                        continue
                    if o.get("model"):
                        got_model_fields.add(o["model"])
                    for ch in o.get("choices") or []:
                        body += (ch.get("delta") or {}).get("content") or ""
    check("流式正文拼出内容", "流式回答" in body, body[:120])
    check("流式 chunk 的 model 被改写为统一名 s1",
          got_model_fields == {"s1"}, got_model_fields)
    check("流式结尾有 [DONE]", "[DONE]" in raw, raw[-80:])

    print("\n=== 10. 流式已输出后断流：不重试、不切换 ===", flush=True)
    make_group("brk", [("B站", "X-stream-break"), ("A站", "gpt-5")])
    st, nodes_brk = admin("/groups/brk/nodes")
    # 第二个节点换个站点才叫“切换”，这里用 A 站做后备
    admin(f"/groups/nodes/{nodes_brk[1]['id']}", "PUT", {"enabled": False})
    body2 = ""
    try:
        with c.stream("POST", BASE + "/v1/chat/completions",
                      headers={"Authorization": "Bearer " + KEY},
                      json={"model": "brk", "stream": True,
                            "messages": [{"role": "user", "content": "断流测试"}]}) as resp:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    p = line[5:].strip()
                    if p and p != "[DONE]":
                        try:
                            o = json.loads(p)
                        except Exception:
                            continue
                        for ch in o.get("choices") or []:
                            body2 += (ch.get("delta") or {}).get("content") or ""
    except Exception as e:
        print(f"      （客户端读到断流：{type(e).__name__}）", flush=True)
    check("断流前已收到部分内容", "已经开始输出" in body2, body2[:120])

    st, logs = admin("/logs?limit=10")
    brk = [x for x in logs if x["model"] == "brk"]
    check("日志 stream_phase 标记为输出后断流(post)",
          bool(brk) and any((x.get("stream_phase") or "") == "post" for x in brk),
          [(x.get("stream_phase"), (x.get("error") or "")[:60]) for x in brk])

    print("\n=== 11. 导出 / 导入 ===", flush=True)
    st, safe = admin("/export?mode=safe")
    check("安全备份不含站点 Key", st == 200 and all("api_key" not in s for s in safe["sites"]),
          [list(s.keys()) for s in safe.get("sites", [])])
    check("安全备份不含网关 Key",
          st == 200 and all("key" not in k for k in safe["keys"]),
          safe.get("keys"))
    st, full = admin("/export?mode=full")
    check("完整备份含站点 Key",
          st == 200 and any(s.get("api_key") for s in full["sites"]))
    check("完整备份含网关 Key", st == 200 and any(k.get("key") for k in full["keys"]))
    check("备份里含统一模型 groups", st == 200 and len(full.get("groups", [])) >= 1,
          len(full.get("groups", [])))

    # 坏文件不能被清空
    st, r = admin("/import", "POST", {"data": {"sites": "不是数组", "routes": []}})
    check("格式错误的导入被拒绝(400)", st == 400, (st, r))
    st, sites_now = admin("/sites")
    check("拒绝非法导入后原数据完好", len(sites_now) == 2, len(sites_now))

    # 合并导入
    patch = {"app": "relayhub", "sites": [{"name": "C站", "base_url": MOCK_A}],
             "groups": [{"name": "auto", "display": "自动", "is_public": 1}],
             "routes": [{"site_name": "C站", "model": "auto",
                         "upstream_model": "gemini-2.5-pro", "priority": 30}]}
    st, r = admin("/import", "POST", {"data": patch, "strategy": "merge"})
    check("合并导入成功", st == 200 and r.get("strategy") == "merge", r)
    st, sites_now = admin("/sites")
    check("合并导入后站点变为 3 个", len(sites_now) == 3, len(sites_now))
    st, nodes = admin("/groups/auto/nodes")
    check("合并导入后 auto 有 3 个节点", len(nodes) == 3, len(nodes))

    # 覆盖导入：整体替换成「只有 A站」，
    # 并且 A站原有的 Key 必须被保留（因为安全备份本身不带 Key）
    st, full2 = admin("/export?mode=full")
    key_before = next((x.get("api_key") for x in full2["sites"] if x["name"] == "A站"), None)
    ow = {"app": "relayhub",
          "sites": [{"name": "A站", "base_url": MOCK_A, "enabled": 1,
                     "priority": 100, "note": ""}],
          "groups": [{"name": "auto", "display": "", "enabled": 1,
                      "is_public": 1, "strategy": "", "note": ""}],
          "routes": [{"site_name": "A站", "model": "auto", "upstream_model": "gpt-5",
                      "enabled": 1, "daily_limit": 0, "priority": 10}],
          "keys": []}
    st, r = admin("/import", "POST", {"data": ow, "strategy": "overwrite"})
    check("覆盖导入成功", st == 200 and r.get("strategy") == "overwrite", r)
    st, sites_now = admin("/sites")
    check("覆盖导入后只剩 1 个站点", len(sites_now) == 1, [s["name"] for s in sites_now])
    st, full3 = admin("/export?mode=full")
    key_after = next((x.get("api_key") for x in full3["sites"] if x["name"] == "A站"), None)
    check("覆盖导入保留了同名站点的原 Key（安全备份场景不丢密钥）",
          bool(key_before) and key_after == key_before, (key_before, key_after))
    st, nodes = admin("/groups/auto/nodes")
    check("覆盖导入后 auto 只剩 1 个节点", len(nodes) == 1, len(nodes))
    st, keys_now = admin("/keys")
    check("覆盖导入会清掉旧网关 Key", len(keys_now) == 0, len(keys_now))

    print("\n" + "=" * 56, flush=True)
    if FAILED:
        print(f"结果：{len(FAILED)} 项失败", flush=True)
        for f in FAILED:
            print("   - " + f, flush=True)
        return 1
    print("结果：全部通过 ✅", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
