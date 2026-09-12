import asyncio
import json
import time

import httpx

from . import db, engine

# ------------------------------------------------------------------ 额度查询
#
# 这一段踩过坑，说明在下面：
#
# 1) 路径不止一个。new-api 同时挂 /v1/dashboard/billing/* 和 /dashboard/billing/*，
#    老站和分叉常常只挂后者。以前只用 /v1 前缀，碰到只有裸路径的站直接 404，
#    结果被当成「该站点未提供额度查询接口」。所以两种前缀都试。
#
# 2) 「不限额度」几乎都不是 null，而是塞一个大数占位。见过最多的是 100000000
#    （一个亿），于是面板上就显示「剩余 1 亿」。这种数不能当成真实余额，
#    也不能喂给 balanced 策略排序 —— 否则所有站看起来都一样富，排序全乱。
#
# 3) system_hard_limit_usd 是「整台部署」的上限，不是这个账号的余额。
#    只在没有 hard/soft 时才退而求其次，并且明确标注。

# 真实余额不可能到这个量级（1 亿美元）。到这个数就认定是「不限」的占位值。
UNLIMITED_FLOOR = 1e8


def _billing_bases(base_url):
    """额度接口的两个可能前缀：带 /v1 的，和不带的。"""
    bare = (base_url or "").strip().rstrip("/")
    if bare.endswith("/v1"):
        bare = bare[:-3]
    out = []
    for b in (engine.norm_base(base_url), bare):
        if b and b not in out:
            out.append(b)
    return out


def _looks_like_unlimited(v):
    """占位值判定：负数（-1 = 不限）或大到不可能是真钱。"""
    if v is None:
        return False
    return v < 0 or v >= UNLIMITED_FLOOR


async def _get_json(client, urls, headers):
    """按顺序试几个 URL，返回第一个 200 且能解析成 JSON 的响应。"""
    errors = []
    for u in urls:
        try:
            r = await client.get(u, headers=headers, timeout=20)
        except Exception as e:
            errors.append(f"{u}: {str(e)[:80]}")
            continue
        if r.status_code == 200:
            try:
                return r.json(), ""
            except Exception:
                errors.append(f"{u}: 200 但不是 JSON")
                continue
        errors.append(f"{u}: HTTP {r.status_code}")
    return None, " | ".join(errors)[:300]


def _num(d, key):
    """取一个数字字段，布尔/字符串不算。"""
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


async def check_site(client, site_id, base_url, api_key):
    headers = {}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    bases = _billing_bases(base_url)
    sub, sub_err = await _get_json(
        client, [b + "/dashboard/billing/subscription" for b in bases], headers)
    usg, usg_err = await _get_json(
        client, [b + "/dashboard/billing/usage" for b in bases], headers)

    info = {"known": 0, "remaining": None, "used": None, "limit": None,
            "raw": "", "error": "", "note": "", "source": ""}

    limit, source = None, ""
    for key in ("hard_limit_usd", "soft_limit_usd", "system_hard_limit_usd"):
        v = _num(sub, key)
        if v is not None:
            limit, source = v, key
            break

    used = _num(usg, "total_usage")
    if used is not None:
        used = used / 100.0  # new-api 文档：total_usage 单位是 0.01 美元

    notes = []
    if _looks_like_unlimited(limit):
        shown = f"{limit:,.0f}" if float(limit).is_integer() else f"{limit:g}"
        notes.append(f"站点返回的上限是占位值 {shown}，通常表示「不限」，已忽略")
        limit, source = None, ""
    elif source == "system_hard_limit_usd":
        notes.append("该站没给出账号额度，只返回了系统级上限（整台部署共用），仅供参考")

    if limit is not None and used is not None:
        info.update(known=1, limit=limit, used=used, remaining=round(limit - used, 4))
    elif used is not None:
        info.update(known=1, used=used)
    elif limit is not None:
        info.update(known=1, limit=limit)

    info["note"] = "；".join(notes)
    info["source"] = source

    raw = {}
    if sub is not None:
        raw["subscription"] = sub
    if usg is not None:
        raw["usage"] = usg
    if not raw:
        info["error"] = (sub_err or usg_err or "该站点未提供额度查询接口")[:300]
    info["raw"] = json.dumps(raw, ensure_ascii=False)[:4000]

    db.execute(
        """UPDATE sites SET quota_known=?, quota_remaining=?, quota_used=?, quota_limit=?,
                            quota_raw=?, quota_error=?, quota_note=?, quota_checked_at=?
           WHERE id=?""",
        (info["known"], info["remaining"], info["used"], info["limit"],
         info["raw"], info["error"], info["note"], time.time(), site_id),
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
