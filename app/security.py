import base64
import hashlib
import hmac
import os
import secrets
import time

from . import db
from .config import ADMIN_PASSWORD, ADMIN_USERNAME, DEFAULT_ADMIN_USERNAME

ITERATIONS = 120_000


def password_from_env():
    return bool(ADMIN_PASSWORD)


def username_from_env():
    return bool(ADMIN_USERNAME)


def _secret():
    """登录态签名密钥。环境变量优先，否则随机生成并存在持久卷里。"""
    env = os.getenv("SECRET_KEY")
    if env:
        if db.get_setting("secret_key") != env:
            db.set_setting("secret_key", env)
        return env
    v = db.get_setting("secret_key")
    if not v:
        v = secrets.token_urlsafe(48)
        db.set_setting("secret_key", v)
    return v


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return f"pbkdf2_sha256${salt}${dk.hex()}"


def verify_password(password, stored):
    try:
        _algo, salt, digest = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return hmac.compare_digest(dk.hex(), digest)


def make_token(username, ttl=7 * 86400):
    exp = int(time.time()) + ttl
    payload = f"{username}|{exp}"
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{body}.{sig}"


def verify_token(token):
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    try:
        pad = "=" * (-len(body) % 4)
        payload = base64.urlsafe_b64decode(body + pad).decode()
    except Exception:
        return None
    expect = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        username, exp = payload.rsplit("|", 1)
        if int(exp) < time.time():
            return None
    except Exception:
        return None
    return username


def check_login(username, password):
    row = db.query_one("SELECT * FROM admins WHERE username=?", (username,))
    if not row:
        return False
    return verify_password(password, row["password_hash"])


def seed_admin():
    """初始化 / 校正管理员账号。

    - 环境变量 ADMIN_PASSWORD 非空 -> 优先级最高，每次启动都校正
    - 未设置 -> 首次启动随机生成并打印到日志，之后以数据库为准（面板可改）
    """
    row = db.query_one("SELECT * FROM admins ORDER BY id LIMIT 1")

    if row is None:
        username = ADMIN_USERNAME or DEFAULT_ADMIN_USERNAME
        password = ADMIN_PASSWORD or secrets.token_urlsafe(12)
        db.execute(
            "INSERT INTO admins(username, password_hash, created_at) VALUES(?, ?, ?)",
            (username, hash_password(password), time.time()),
        )
        print("=" * 64, flush=True)
        print(f"[RelayHub] 已创建面板账号: {username}", flush=True)
        if ADMIN_PASSWORD:
            print("[RelayHub] 密码来自环境变量 ADMIN_PASSWORD", flush=True)
        else:
            print(f"[RelayHub] 随机初始密码: {password}", flush=True)
            print("[RelayHub] 建议设置环境变量 ADMIN_PASSWORD，以免忘记后进不去", flush=True)
        print("=" * 64, flush=True)
        return

    if ADMIN_PASSWORD:
        if not verify_password(ADMIN_PASSWORD, row["password_hash"]):
            db.execute(
                "UPDATE admins SET password_hash=? WHERE id=?",
                (hash_password(ADMIN_PASSWORD), row["id"]),
            )
            print("[RelayHub] 已根据环境变量 ADMIN_PASSWORD 重置面板密码", flush=True)
        if ADMIN_USERNAME and ADMIN_USERNAME != row["username"]:
            db.execute(
                "UPDATE admins SET username=? WHERE id=?", (ADMIN_USERNAME, row["id"])
            )
            print(f"[RelayHub] 已根据 ADMIN_USERNAME 将账号改为 {ADMIN_USERNAME}", flush=True)
    else:
        print(
            "[RelayHub] 未设置 ADMIN_PASSWORD，面板密码以数据库为准（可在面板修改）",
            flush=True,
        )


def gen_api_key():
    return "sk-rh-" + secrets.token_urlsafe(32).replace("-", "").replace("_", "")


def mask_key(k):
    if not k:
        return ""
    if len(k) <= 10:
        return k[:2] + "***"
    return f"{k[:6]}...{k[-4:]}"
