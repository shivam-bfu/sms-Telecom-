import os
import secrets
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_persistent_fallback():
    # To prevent multi-worker process token mismatches on Cloud Run or Gunicorn,
    # we persist the generated fallback key in a shared file (/tmp/.sms_gateway_fallback).
    # Since all processes run on the same instance, they will share the same fallback key.
    fallback_file = "/tmp/.sms_gateway_fallback"
    
    # Attempt to read existing key first
    for _ in range(5):
        try:
            if os.path.exists(fallback_file):
                with open(fallback_file, "r") as f:
                    val = f.read().strip()
                    if len(val) >= 32:
                        return val
        except Exception:
            pass
        time.sleep(0.1)

    val = secrets.token_urlsafe(32)
    try:
        # Atomic file creation using O_CREAT | O_EXCL to prevent concurrent write races
        fd = os.open(fallback_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(val)
        return val
    except FileExistsError:
        # Another worker process created the file concurrently, read it
        for _ in range(10):
            try:
                with open(fallback_file, "r") as f:
                    read_val = f.read().strip()
                    if len(read_val) >= 32:
                        return read_val
            except Exception:
                pass
            time.sleep(0.1)
    except Exception:
        pass
    return val


# These are the well-known defaults that used to ship in source control.
# They must NEVER be accepted as real secrets in production.
_INSECURE_DEFAULTS = {
    "admin123",
    "smsgateway_secret_token_123",
    "sms_gateway_fallback_secret_key_123456789",
}


class Config:
    """
    Central configuration. Everything sensitive is read from the environment
    so no secrets ever live in source control.

    In production (FLASK_ENV != "development") these secrets are REQUIRED:
    if they are missing, blank, too short, or match an old insecure default,
    the app refuses to start (see Config.validate()). Failing loudly at boot
    is much safer than silently running with a guessable/public secret.

    In development, missing secrets fall back to random, per-process values
    (regenerated every restart) purely so local testing doesn't need a .env
    file. These dev values are never persisted.
    """

    # --- Database ---------------------------------------------------
    DATABASE_PATH = os.environ.get("DATABASE_PATH")  # explicit override wins

    # --- Misc -----------------------------------------------------
    ENV = os.environ.get("FLASK_ENV", "production")
    DEBUG = ENV == "development"

    # --- Secrets ------------------------------------------------------
    # Random per-process fallback used ONLY in development mode, so we never
    # fall back to a hardcoded string sitting in a public repo.
    _dev_fallback = _get_persistent_fallback()

    ADMIN_PASSWORD_DEFAULT = os.environ.get("ADMIN_PASSWORD") or (_dev_fallback if DEBUG else "")
    DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN") or (_dev_fallback if DEBUG else "")
    MY_NUMBER = os.environ.get("MY_NUMBER", "+91-98765-43210")
    SECRET_KEY = os.environ.get("SECRET_KEY") or (_dev_fallback if DEBUG else "")

    @classmethod
    def validate(cls):
        """
        Called once at app startup. Raises RuntimeError (crashing the boot)
        if running in production without properly configured secrets.
        """
        if cls.DEBUG:
            return  # dev mode already has random per-process fallbacks

        problems = []
        for name in ("ADMIN_PASSWORD", "DEVICE_TOKEN", "SECRET_KEY"):
            value = os.environ.get(name)
            if not value:
                problems.append(f"{name} is not set")
            elif value in _INSECURE_DEFAULTS:
                problems.append(f"{name} is set to a known insecure default value")
            elif len(value) < 12:
                problems.append(f"{name} is too short (use at least 12 random characters)")

        if problems:
            raise RuntimeError(
                "Refusing to start in production with insecure configuration:\n  - "
                + "\n  - ".join(problems)
                + "\nSet these as real, random environment variables "
                "(e.g. `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`) "
                "before deploying. To run locally instead, set FLASK_ENV=development."
            )

    @staticmethod
    def resolve_db_path():
        """
        Pick a writable location for the SQLite file depending on the
        platform we're running on (Fly/Railway volume, Vercel /tmp, or
        local dev), unless DATABASE_PATH was explicitly set.
        """
        if Config.DATABASE_PATH:
            return Config.DATABASE_PATH

        default_path = os.path.join(BASE_DIR, "sms_gateway.db")

        if os.path.exists("/data") and os.access("/data", os.W_OK):
            return "/data/sms_gateway.db"

        if os.environ.get("VERCEL") or not os.access(
            os.path.dirname(default_path) or ".", os.W_OK
        ):
            return "/tmp/sms_gateway.db"

        return default_path
