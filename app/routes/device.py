import time
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

from ..db import get_db
from ..security import check_device_auth
from ..config import Config
from ..extensions import limiter
from .location_resolver import trigger_enrichment
from .phone_extractor import extract_first

device_bp = Blueprint("device", __name__, url_prefix="/api")

# The legitimate device polls every ~15s (4/min) and reports status/incoming
# messages occasionally, so this limit comfortably covers normal traffic
# while still bounding how fast an attacker can brute-force the device token.
_DEVICE_RATE_LIMIT = "30 per minute"


def _require_device():
    if not check_device_auth(request):
        return jsonify({"error": "Unauthorized Device"}), 401
    return None


def _touch_heartbeat(conn, time_str, battery=None):
    # Try to extract actual physical device info from custom request headers
    device_name = request.headers.get("X-Device-Name")
    device_battery = request.headers.get("X-Device-Battery") or battery
    device_version = request.headers.get("X-Device-Version")

    # Construct the dynamic SQL update statement
    updates = ["status='online'", "last_seen=?"]
    params = [time_str]

    if device_name:
        updates.append("name=?")
        params.append(device_name)
    if device_battery:
        updates.append("battery=?")
        params.append(device_battery)
    if device_version:
        updates.append("version=?")
        params.append(device_version)

    params.append("node1") # target ID
    query = f"UPDATE device_status SET {', '.join(updates)} WHERE id=?"
    conn.execute(query, tuple(params))


@device_bp.route("/v1/device/messages", methods=["GET"])
@device_bp.route("/v1/messages", methods=["GET"])
@limiter.limit(_DEVICE_RATE_LIMIT)
def device_poll_messages():
    if (err := _require_device()) is not None:
        return err

    conn = get_db()
    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _touch_heartbeat(conn, time_str, battery="88%")
    conn.commit()

    rows = conn.execute(
        "SELECT id, recipient, text, sender, sim_operator FROM messages WHERE status='queued' AND direction='out'"
    ).fetchall()
    conn.close()

    return (
        jsonify(
            [
                {
                    "id": r["id"],
                    "recipient": r["recipient"],
                    "message": r["text"],
                    "sender": r["sender"],
                    "sim_operator": r["sim_operator"] or "",
                }
                for r in rows
            ]
        ),
        200,
    )


@device_bp.route("/v1/device/messages/<message_id>/status", methods=["POST"])
@device_bp.route("/v1/messages/status", methods=["POST"])
@limiter.limit(_DEVICE_RATE_LIMIT)
def device_report_status(message_id=None):
    if (err := _require_device()) is not None:
        return err

    data = request.json or {}
    target_id = message_id or data.get("id")
    if not target_id:
        return jsonify({"error": "Message ID required"}), 400

    status = data.get("status")

    conn = get_db()
    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (target_id,)).fetchone()
    if not msg:
        conn.close()
        return jsonify({"error": "Message not found"}), 404

    new_status = "sent" if status == "sent" else "failed"
    conn.execute("UPDATE messages SET status = ? WHERE id = ?", (new_status, target_id))

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _touch_heartbeat(conn, time_str)
    conn.commit()
    conn.close()

    return jsonify({"success": True}), 200


@device_bp.route("/v1/incoming", methods=["POST"])
@limiter.limit(_DEVICE_RATE_LIMIT)
def device_incoming_message():
    # Strictly enforce device authentication to prevent spoofing or unauthorized messages
    if (err := _require_device()) is not None:
        return err

    data = request.json or {}
    sender = data.get("sender")
    message = data.get("message")

    # New: list of coworkers this reply was matched to (can be more than one
    # when several coworkers queried the same target number - the reply gets
    # duplicated to each of them). Falls back to the old singular field for
    # older app builds that haven't been updated yet.
    routed_users = data.get("routed_users")
    if not routed_users:
        legacy_single = data.get("routed_user", "System")
        routed_users = [legacy_single] if legacy_single else ["System"]

    if not sender or not message:
        return jsonify({"error": "sender and message are required"}), 400

    # Owners this reply should land in. "System"/blank means nobody specific
    # claimed this conversation - it won't show in any coworker's personal
    # inbox, only in the admin's global log (owner = None).
    owners = [u for u in routed_users if u and u != "System"]
    if not owners:
        owners = [None]

    conn = get_db()
    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # The number being tracked/queried, echoed back inside the operator's reply
    # text. Storing this (instead of relying on `sender`, which can be a
    # different gateway number on every reply) is what lets the dashboard show
    # every reply about the same customer number in one conversation/location
    # history, even when a telecom operator replies from several different
    # numbers of their own.
    target_number = extract_first(message)

    # Insert one row per matched owner so each coworker's filtered inbox
    # (WHERE owner = ?) shows the reply. A shared `id` prefix keeps the
    # duplicates traceable back to the same physical SMS in the admin view.
    base_id = f"MSG-IN-{int(time.time() * 1000)}"
    for idx, owner in enumerate(owners):
        msg_id = base_id if idx == 0 else f"{base_id}-{idx}"
        conn.execute(
            """
            INSERT INTO messages (id, direction, sender, recipient, text, time, status, owner, target_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (msg_id, "in", sender, Config.MY_NUMBER, message, time_str, "delivered", owner, target_number),
        )
        trigger_enrichment(msg_id, message)

    _touch_heartbeat(conn, time_str, battery="100%")
    conn.commit()
    conn.close()

    return jsonify({"success": True, "routed_to": owners}), 200
