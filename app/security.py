import base64
import hashlib
import hmac
import os
import secrets
import time

from . import db
from .config import ADMIN_PASSWORD, ADMIN_USERNAME

ITERATIONS = 120_000


def _secret():
    v = db.get_setting("secret_key")
    if not v:
        v = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
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
    row = db.query_one("SELECT COUNT(*) AS c FROM admins")
    if row and row["c"]:
        return
    password = ADMIN_PASSWORD or secrets.token_urlsafe(12)
    db.execute(
        "INSERT INTO admins(username, password_hash, created_at) VALUES(?, ?, ?)",
        (ADMIN_USERNAME, hash_password(password), time.time()),
    )
    print("=" * 64, flush=True)
    print(f"[RelayHub] 管理员账号: {ADMIN_USERNAME}", flush=True)
    if ADMIN_PASSWORD:
        print("[RelayHub] 密码来自环境变量 ADMIN_PASSWORD", flush=True)
    else:
        print(f"[RelayHub] 随机初始密码: {password}", flush=True)
        print("[RelayHub] 登录后请到「设置」里改掉它", flush=True)
    if not os.getenv("SECRET_KEY"):
        print("[RelayHub] 建议设置环境变量 SECRET_KEY，否则每次重建容器会掉登录态", flush=True)
    print("=" * 64, flush=True)


def gen_api_key():
    return "sk-rh-" + secrets.token_urlsafe(32).replace("-", "").replace("_", "")


def mask_key(k):
    if not k:
        return ""
    if len(k) <= 10:
        return k[:2] + "***"
    return f"{k[:6]}...{k[-4:]}"
