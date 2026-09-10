import asyncio
import json
import time

import httpx

from . import db, engine


async def check_site(client, site_id, base_url, api_key):
    base = engine.norm_base(base_url)
    headers = {}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    info = {"known": 0, "remaining": None, "used": None, "limit": None, "raw": "", "error": ""}
    raw = {}

    try:
        r = await client.get(base + "/dashboard/billing/subscription", headers=headers, timeout=20)
        if r.status_code == 200:
            raw["subscription"] = r.json()
    except Exception as e:
        info["error"] = str(e)[:200]

    try:
        r = await client.get(base + "/dashboard/billing/usage", headers=headers, timeout=20)
        if r.status_code == 200:
            raw["usage"] = r.json()
    except Exception as e:
        info["error"] = (info["error"] + " | " + str(e))[:200]

    sub = raw.get("subscription") or {}
    usg = raw.get("usage") or {}
    limit = None
    used = None

    if isinstance(sub, dict):
        for k in ("hard_limit_usd", "system_hard_limit_usd", "soft_limit_usd"):
            v = sub.get(k)
            if isinstance(v, (int, float)):
                limit = float(v)
                break
    if isinstance(usg, dict):
        v = usg.get("total_usage")
        if isinstance(v, (int, float)):
            used = float(v) / 100.0  # new-api 以「分」为单位

    if limit is not None and used is not None:
        info.update(known=1, limit=limit, used=used, remaining=round(limit - used, 4))
    elif used is not None:
        info.update(known=1, used=used)
    elif limit is not None:
        info.update(known=1, limit=limit)

    if not raw and not info["error"]:
        info["error"] = "该站点未提供额度查询接口"

    info["raw"] = json.dumps(raw, ensure_ascii=False)[:4000]
    db.execute(
        """UPDATE sites SET quota_known=?, quota_remaining=?, quota_used=?, quota_limit=?,
                            quota_raw=?, quota_error=?, quota_checked_at=?
           WHERE id=?""",
        (info["known"], info["remaining"], info["used"], info["limit"],
         info["raw"], info["error"], time.time(), site_id),
    )
    return info


async def check_all():
    rows = db.query("SELECT id, base_url, api_key FROM sites WHERE enabled=1")
    if not rows:
        return
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for row in rows:
            try:
                await check_site(client, row["id"], row["base_url"], row["api_key"])
            except Exception:
                pass
            await asyncio.sleep(0.4)


async def background_loop():
    await asyncio.sleep(15)
    while True:
        try:
            await check_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        interval = int(db.get_setting("quota_check_interval", 3600) or 3600)
        await asyncio.sleep(max(120, interval))
