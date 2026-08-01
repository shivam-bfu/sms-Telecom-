import base64
import hashlib
import hmac
import time

from werkzeug.security import generate_password_hash, check_password_hash

from .config import Config
from .db import get_db


def get_admin_password_hash():
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'admin_password'"
    ).fetchone()
    conn.close()
    if row:
        return row["value"]
    return generate_password_hash(Config.ADMIN_PASSWORD_DEFAULT)


def set_admin_password(new_pw):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_password', ?)",
        (generate_password_hash(new_pw),),
    )
    conn.commit()
    conn.close()


def verify_admin_password(candidate):
    return check_password_hash(get_admin_password_hash(), candidate or "")


def check_admin_auth(req):
    """Admin requests authenticate via X-Admin-Password header."""
    pw = req.headers.get("X-Admin-Password")
    return verify_admin_password(pw)


def check_device_auth(req):
    """Android integrator devices authenticate via HTTP Basic auth."""
    auth_header = req.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        encoded = auth_header.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        _dev_id, _, dev_pwd = decoded.partition(":")
        return hmac.compare_digest(dev_pwd, Config.DEVICE_TOKEN)
    except Exception:
        return False


def verify_coworker_password(username, candidate_password):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    if user and check_password_hash(user["password_hash"], candidate_password):
        return user
    return None


# Alias for compatibility if imported elsewhere
verify_user_password = verify_coworker_password


def generate_token(username):
    """Secure HMAC signed bearer token for the coworker portal."""
    timestamp = str(int(time.time()))
    payload = f"{username}:{timestamp}"
    signature = hmac.new(
        Config.SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    token = f"{payload}:{signature}"
    return base64.b64encode(token.encode("utf-8")).decode("utf-8")


def verify_token(token):
    if not token:
        return None
    try:
        decoded = base64.b64decode(token).decode("utf-8")
        parts = decoded.split(":")
        if len(parts) != 3:
            return None
        username, timestamp, signature = parts
        
        # Verify signature
        payload = f"{username}:{timestamp}"
        expected_sig = hmac.new(
            Config.SECRET_KEY.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(signature, expected_sig):
            return None
            
        # Optional: verify token has not expired (e.g. 7 days = 604800 seconds)
        token_time = int(timestamp)
        if time.time() - token_time > 604800:
            return None
            
        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
        if user:
            return username
    except Exception:
        pass
    return None


def get_bearer_token(req):
    auth_header = req.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    return auth_header.split(" ", 1)[1]
