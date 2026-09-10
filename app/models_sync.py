import asyncio

from . import db, engine


def _extract_ids(data):
    if not isinstance(data, dict):
        return []
    out = []
    for item in data.get("data") or []:
        if isinstance(item, dict) and item.get("id"):
            out.append(str(item["id"]))
        elif isinstance(item, str):
            out.append(item)
    return sorted(set(out))


async def scan_site(client, site):
    """拉取单个站点的模型列表。"""
    result = {
        "site_id": site["id"],
        "site_name": site["name"],
        "ok": False,
        "error": "",
        "models": [],
        "existing": [],
    }
    result["existing"] = sorted(
        r["model"] for r in db.query("SELECT model FROM routes WHERE site_id=?", (site["id"],))
    )

    base = engine.norm_base(site.get("base_url"))
    if not base:
        result["error"] = "Base URL 为空"
        return result

    try:
        r = await client.get(base + "/models", headers=engine.upstream_headers(site), timeout=25)
    except Exception as e:
        result["error"] = f"连接失败: {e}"
        return result

    if r.status_code != 200:
        result["error"] = f"HTTP {r.status_code}: {(r.text or '')[:200]}"
        return result

    try:
        result["models"] = _extract_ids(r.json())
    except Exception:
        result["error"] = "返回内容不是合法 JSON"
        return result

    result["ok"] = True
    return result


async def scan_all(client):
    rows = db.query("SELECT * FROM sites WHERE enabled=1 ORDER BY priority, id")
    out = []
    for row in rows:
        site = db.rowdict(row)
        try:
            out.append(await scan_site(client, site))
        except Exception as e:
            out.append({
                "site_id": site["id"],
                "site_name": site["name"],
                "ok": False,
                "error": str(e)[:200],
                "models": [],
                "existing": [],
            })
        await asyncio.sleep(0.3)
    return out


def import_models(items, daily_limit=0):
    """导入模型到路由。已存在的记录不动（保留用户设置的上游名与限额）。"""
    added = 0
    for it in items or []:
        try:
            site_id = int(it.get("site_id"))
        except Exception:
            continue
        model = str(it.get("model") or "").strip()
        if not model:
            continue
        if db.query_one("SELECT 1 FROM routes WHERE site_id=? AND model=?", (site_id, model)):
            continue
        db.execute(
            """INSERT INTO routes(site_id, model, upstream_model, enabled, daily_limit)
               VALUES (?, ?, '', 1, ?)""",
            (site_id, model, int(daily_limit or 0)),
        )
        # 顺手建一条「自动生成」的统一模型记录（默认不对外公开）。
        # 这些模型仍然可以按原名直接请求，但不会污染 /v1/models 和统一模型页。
        db.ensure_group(model, auto=1, is_public=0)
        added += 1
    return added


async def auto_sync(client):
    """定时自动同步：只新增上游新出现的模型，绝不删除用户已配置的路由。"""
    results = await scan_all(client)
    items = []
    for r in results:
        if not r["ok"]:
            continue
        for m in r["models"]:
            items.append({"site_id": r["site_id"], "model": m})
    return import_models(items)


async def background_loop(client):
    await asyncio.sleep(45)
    while True:
        try:
            if db.get_setting("auto_sync_models", True):
                await auto_sync(client)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        interval = int(db.get_setting("auto_sync_interval", 86400) or 86400)
        await asyncio.sleep(max(900, interval))
