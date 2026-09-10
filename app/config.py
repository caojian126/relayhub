import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "relayhub.db")))

# 首次启动时用来创建管理员账号；密码留空则随机生成并打印到容器日志
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "15"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "600"))
