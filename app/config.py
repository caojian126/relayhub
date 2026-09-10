import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "relayhub.db")))

# 面板账号 / 密码：由环境变量控制。
# ADMIN_PASSWORD 非空时优先级最高，每次启动都会校正数据库里的密码，
# 因此忘记密码时改环境变量重启即可找回，不会被锁在面板外。
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")      # None 表示不干预
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")  # 空字符串表示不干预
DEFAULT_ADMIN_USERNAME = "admin"

CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "15"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "600"))

# 常见 PaaS / 容器平台的环境变量，用来判断「我是不是跑在云上」
_CLOUD_ENV_KEYS = (
    "ZEABUR", "ZEABUR_APP_ID", "ZEABUR_SERVICE_ID", "ZEABUR_PROJECT_ID",
    "RAILWAY_ENVIRONMENT", "FLY_APP_NAME", "DYNO", "K_SERVICE",
    "RENDER", "KUBERNETES_SERVICE_HOST", "VERCEL", "HEROKU_APP_NAME",
)

# 这些路径看起来像「临时容器层」而不是持久卷
_TEMP_PREFIXES = ("/tmp", "/var/tmp", "/run")


def looks_like_cloud():
    return any(os.getenv(k) for k in _CLOUD_ENV_KEYS)


def data_dir_is_mount():
    """判断 DATA_DIR 是不是一个独立挂载点（也就是持久卷）。

    真正的挂载点（Zeabur/Railway 挂的 Volume）st_dev 与父目录不同；
    如果相同，说明它只是容器可写层的一部分 —— 容器重建就没了。
    """
    try:
        d = Path(DATA_DIR).resolve()
        while not d.exists() and d != d.parent:
            d = d.parent
        return os.stat(str(d)).st_dev != os.stat(str(d.parent)).st_dev
    except Exception:
        return False


def data_dir_writable():
    try:
        p = Path(DATA_DIR)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def persistence_report():
    """给启动日志 + 管理面板用的持久化体检结果。"""
    raw = str(DATA_DIR)
    mounted = data_dir_is_mount()
    cloud = looks_like_cloud()
    writable = data_dir_writable()
    tempish = raw.startswith(_TEMP_PREFIXES)

    warning = ""
    if not mounted and (cloud or tempish):
        warning = (
            "当前数据目录可能不是持久化存储，重新部署后配置可能丢失。"
            f"（DATA_DIR={raw}，未检测到独立挂载点）"
        )
        if cloud:
            warning += " 请到部署平台创建 Volume 并挂载到该路径，且设置 DATA_DIR 指向它。"
    if not writable:
        warning = f"数据目录不可写（DATA_DIR={raw}），配置无法保存！请检查权限或挂载设置。"

    return {
        "data_dir": raw,
        "db_path": str(DB_PATH),
        "resolved": str(Path(DATA_DIR).resolve()),
        "is_mount": mounted,
        "looks_like_cloud": cloud,
        "writable": writable,
        "warning": warning,
    }
