"""
Shared Flask extension instances.

Kept in their own module (rather than directly in app/__init__.py) so that
route blueprints can `from ..extensions import limiter` without triggering
a circular import (routes are imported *by* app/__init__.py while it's
still being constructed).
"""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=[])
