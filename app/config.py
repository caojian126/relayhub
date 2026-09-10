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
