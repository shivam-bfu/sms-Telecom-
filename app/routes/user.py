import time
from datetime import datetime

from flask import Blueprint, request, jsonify

from ..db import get_db
from ..security import verify_user_password, generate_token, verify_token, get_bearer_token
from ..config import Config
from ..extensions import limiter
from .location_resolver import trigger_enrichment
from .phone_extractor import extract_first

user_bp = Blueprint("user", __name__, url_prefix="/api")


def _require_user():
    token = get_bearer_token(request)
    username = verify_token(token)
    if not username:
        return None, (jsonify({"error": "Unauthorized"}), 401)
    return username, None


@user_bp.route("/login", methods=["POST"])
@limiter.limit("5 per minute; 30 per hour")
def user_login():
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")

    user = verify_user_password(username, password or "")
    if user:
        token = generate_token(username)
        return (
            jsonify(
                {
                    "token": token,
                    "username": user["username"],
                    "name": user["name"],
                    "my_number": Config.MY_NUMBER,
                }
            ),
            200,
        )
    return jsonify({"error": "Invalid user credentials"}), 401


@user_bp.route("/inbox", methods=["GET"])
def user_inbox():
    username, err = _require_user()
    if err:
        return err

    conn = get_db()
    # Only this user's own sent messages and replies routed to them -
    # users should never see each other's conversations.
    rows = conn.execute(
        "SELECT * FROM messages WHERE owner = ? ORDER BY time DESC, id DESC LIMIT 200",
        (username,),
    ).fetchall()
    conn.close()
    
    messages = [dict(r) for r in rows]
    for msg in messages:
        trigger_enrichment(msg["id"], msg["text"])
        
    return jsonify({"messages": messages})


@user_bp.route("/send", methods=["POST"])
def user_send_sms():
    username, err = _require_user()
    if err:
        return err

    data = request.json or {}
    to = data.get("to")
    text = data.get("text")

    if not to or not text:
        return jsonify({"error": "Receiver and message are required"}), 400

    conn = get_db()
    msg_id = f"MSG-{int(time.time() * 1000)}"
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target_number = extract_first(text)

    conn.execute(
        """
        INSERT INTO messages (id, direction, sender, recipient, text, time, status, owner, target_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (msg_id, "out", username, to, text, time_str, "queued", username, target_number),
    )
    conn.commit()
    conn.close()

    trigger_enrichment(msg_id, text)

    return jsonify({"success": True, "message_id": msg_id}), 200
