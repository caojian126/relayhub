import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "relayhub.db")))

# 持久卷里的配置文件：面板账号 / 密码 / 签名密钥都放在这里，
# 修改后重启服务即可生效，无需重新部署。
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", str(DATA_DIR / "config.json")))

# 仅在首次初始化时使用；之后以 CONFIG_PATH 为准
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "15"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "600"))
