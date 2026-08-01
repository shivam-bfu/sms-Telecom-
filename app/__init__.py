import logging
import os

from flask import Flask

from .config import Config
from .db import init_db
from .extensions import limiter
from .routes import pages_bp, admin_bp, coworker_bp, device_bp


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    logging.basicConfig(
        level=logging.INFO if not Config.DEBUG else logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    # Fail fast rather than silently booting with a guessable admin
    # password / device token / signing key in production.
    Config.validate()

    if Config.DEBUG:
        logger.warning(
            "Running in DEVELOPMENT mode with randomly generated, "
            "non-persistent secrets. Never use FLASK_ENV=development in "
            "production."
        )

    limiter.init_app(app)

    with app.app_context():
        init_db()

    # Skip starting a second copy of the scheduler in the Flask dev
    # reloader's watcher process (it forks a child that re-runs this
    # module); only the actual running process should have os.environ
    # WERKZEUG_RUN_MAIN set to "true" once it re-execs. In production
    # (gunicorn, no reloader) this env var is simply unset, so we start
    # normally.
    if not Config.DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        from .scheduler import register_scheduler
        app.scheduler = register_scheduler(app)

    app.register_blueprint(pages_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(coworker_bp)
    app.register_blueprint(device_bp)

    @app.after_request
    def set_security_headers(response):
        # Defense-in-depth: even though templates now escape all
        # user-controlled data, these headers reduce the blast radius of
        # any future XSS/clickjacking/mime-sniffing issue.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: https://*.tile.openstreetmap.org; "
            # Tailwind is now compiled locally at build time (see
            # app/static/css/tailwind.css) instead of the Play CDN, so it
            # no longer needs a script-src allowance or network access at
            # all - this also fixes styling for users whose network/ISP/
            # extensions blocked cdn.tailwindcss.com.
            # unpkg.com is Leaflet's map JS/CSS (used by app.html).
            # Fonts are the native OS font stack now (no Google Fonts network
            # dependency), so no font-src / fonts.googleapis.com allowance is needed.
            "script-src 'self' 'unsafe-inline' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com;",
        )
        return response

    return app
