import base64
import hashlib
import hmac
import secrets
import time

from . import db, fileconfig
from .config import ADMIN_PASSWORD, ADMIN_USERNAME

ITERATIONS = 120_000


def _secret():
    v = db.get_setting("secret_key")
    if not v:
        v = fileconfig.get(["server", "secret_key"]) or secrets.token_urlsafe(48)
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
    """以 /data/config.json 为准初始化管理员账号。"""
    cfg = fileconfig.load()
    admin = cfg.get("admin") if isinstance(cfg.get("admin"), dict) else {}
    file_username = (admin.get("username") or "").strip() or ADMIN_USERNAME
    file_password = admin.get("password") or ""

    secret_key = (cfg.get("server") or {}).get("secret_key")
    if secret_key:
        db.set_setting("secret_key", secret_key)

    row = db.query_one("SELECT * FROM admins ORDER BY id LIMIT 1")

    if row is None:
        password = file_password or ADMIN_PASSWORD or secrets.token_urlsafe(12)
        db.execute(
            "INSERT INTO admins(username, password_hash, created_at) VALUES(?, ?, ?)",
            (file_username, hash_password(password), time.time()),
        )
        return

    # 配置文件是唯一事实来源：密码/用户名与文件中不一致就同步过来
    if file_password and not verify_password(file_password, row["password_hash"]):
        db.execute(
            "UPDATE admins SET password_hash=? WHERE id=?",
            (hash_password(file_password), row["id"]),
        )
        print("[RelayHub] 已按 config.json 更新管理员密码", flush=True)
    if file_username and file_username != row["username"]:
        db.execute("UPDATE admins SET username=? WHERE id=?", (file_username, row["id"]))
        print(f"[RelayHub] 已按 config.json 更新管理员账号为 {file_username}", flush=True)


def gen_api_key():
    return "sk-rh-" + secrets.token_urlsafe(32).replace("-", "").replace("_", "")


def mask_key(k):
    if not k:
        return ""
    if len(k) <= 10:
        return k[:2] + "***"
    return f"{k[:6]}...{k[-4:]}"
