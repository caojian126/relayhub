import json
import os
import secrets

from .config import ADMIN_PASSWORD, ADMIN_USERNAME, CONFIG_PATH

_TEMPLATE_COMMENT = (
    "这是 RelayHub 的配置文件，位于持久卷中。"
    "修改后重启服务即可生效，不需要重新部署。"
    "在面板「设置」页修改密码时，也会同步写回本文件。"
)


def save(data):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def load(create=True):
    """读取配置文件。不存在时生成一份默认配置。"""
    if not CONFIG_PATH.exists():
        if not create:
            return {}
        data = {
            "_comment": _TEMPLATE_COMMENT,
            "admin": {
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD or secrets.token_urlsafe(12),
            },
            "server": {
                "secret_key": os.getenv("SECRET_KEY") or secrets.token_urlsafe(48),
            },
        }
        save(data)
        print("=" * 64, flush=True)
        print(f"[RelayHub] 已生成配置文件: {CONFIG_PATH}", flush=True)
        print(f"[RelayHub] 面板账号: {data['admin']['username']}", flush=True)
        if not ADMIN_PASSWORD:
            print(f"[RelayHub] 初始密码: {data['admin']['password']}", flush=True)
            print("[RelayHub] 可直接编辑该文件，或在面板「设置」页修改", flush=True)
        print("=" * 64, flush=True)
        return data
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[RelayHub] 读取 {CONFIG_PATH} 失败：{e}", flush=True)
        return {}


def get(keys, default=None):
    node = load()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def set_value(keys, value):
    data = load()
    if not isinstance(data, dict):
        data = {}
    node = data
    for k in keys[:-1]:
        nxt = node.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            node[k] = nxt
        node = nxt
    node[keys[-1]] = value
    save(data)
    return data
