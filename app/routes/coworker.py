import time
import io
import csv
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response

from ..db import get_db
from ..security import verify_coworker_password, generate_token, verify_token, get_bearer_token
from ..config import Config
from ..extensions import limiter
from ..scheduler import ALLOWED_FREQUENCIES_MINUTES
from .location_resolver import trigger_enrichment, reverse_geocode
from .phone_extractor import extract_first

coworker_bp = Blueprint("coworker", __name__, url_prefix="/api")

_DT_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _parse_datetime(value):
    """Accepts the <input type="datetime-local"> format ("2026-07-20T14:30")
    as well as a plain "YYYY-MM-DD HH:MM[:SS]" string. Returns None if it
    doesn't match any of those, so callers can turn that into a 400."""
    if not value:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _require_coworker():
    token = get_bearer_token(request)
    username = verify_token(token)
    if not username:
        return None, (jsonify({"error": "Unauthorized"}), 401)
    return username, None


@coworker_bp.route("/login", methods=["POST"])
@limiter.limit("5 per minute; 30 per hour")
def coworker_login():
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")

    user = verify_coworker_password(username, password or "")
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


@coworker_bp.route("/numbers", methods=["GET"])
def coworker_get_numbers():
    username, err = _require_coworker()
    if err:
        return err

    conn = get_db()
    user_row = conn.execute("SELECT allowed_numbers FROM users WHERE username = ?", (username,)).fetchone()
    if not user_row:
        conn.close()
        return jsonify({"numbers": []}), 200

    allowed = user_row["allowed_numbers"] or "*"
    
    # Fetch all gateway numbers
    rows = conn.execute("SELECT phone_number, operator_name FROM gateway_numbers ORDER BY timestamp DESC").fetchall()
    conn.close()

    numbers = []
    for r in rows:
        phone = r["phone_number"]
        operator = r["operator_name"]
        
        # Filter if allowed_numbers is not '*'
        if allowed == "*" or phone in allowed.split(","):
            numbers.append({
                "phone_number": phone,
                "operator_name": operator
            })

    return jsonify({"numbers": numbers}), 200


@coworker_bp.route("/inbox", methods=["GET"])
def coworker_inbox():
    username, err = _require_coworker()
    if err:
        return err

    conn = get_db()
    # Only this coworker's own sent messages and replies routed to them -
    # coworkers should never see each other's conversations.
    rows = conn.execute(
        "SELECT * FROM messages WHERE owner = ? ORDER BY time DESC, id DESC LIMIT 200",
        (username,),
    ).fetchall()
    conn.close()
    
    messages = [dict(r) for r in rows]
    for msg in messages:
        trigger_enrichment(msg["id"], msg["text"])
        
    return jsonify({"messages": messages})


@coworker_bp.route("/reverse-geocode", methods=["GET"])
def coworker_reverse_geocode():
    username, err = _require_coworker()
    if err:
        return err

    lat = request.args.get("lat")
    lng = request.args.get("lng")
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lng query params are required"}), 400

    address = reverse_geocode(lat, lng)
    if not address:
        return jsonify({"address": None}), 200

    return jsonify({"address": address}), 200


@coworker_bp.route("/export", methods=["GET"])
def coworker_export_messages():
    username, err = _require_coworker()
    if err:
        return err

    conn = get_db()
    # Query this coworker's own messages
    rows = conn.execute(
        "SELECT * FROM messages WHERE owner = ? ORDER BY time DESC, id DESC",
        (username,),
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "ID", "Direction", "Sender", "Recipient", 
        "Message Text", "Timestamp", "Status", "SIM Operator"
    ])

    for r in rows:
        direction_label = "Outgoing" if r["direction"] == "out" else "Incoming"
        writer.writerow([
            r["id"],
            direction_label,
            r["sender"],
            r["recipient"],
            r["text"],
            r["time"],
            r["status"],
            r["sim_operator"] or ""
        ])

    csv_data = "\ufeff" + output.getvalue()
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-disposition": f"attachment; filename=sms_export_{username}.csv"}
    )


@coworker_bp.route("/send", methods=["POST"])
def coworker_send_sms():
    username, err = _require_coworker()
    if err:
        return err

    data = request.json or {}
    to = data.get("to")
    text = data.get("text")
    sim_operator = data.get("sim_operator", "").strip()

    if not to or not text:
        return jsonify({"error": "Receiver and message are required"}), 400

    conn = get_db()
    
    # Confirm user existence
    user_row = conn.execute("SELECT username, allowed_numbers FROM users WHERE username = ?", (username,)).fetchone()
    if not user_row:
        conn.close()
        return jsonify({"error": "User profile not found"}), 404

    allowed = user_row["allowed_numbers"] or "*"

    # Verify that the recipient number 'to' is a registered pre-defined gateway number
    gateway_row = conn.execute("SELECT phone_number FROM gateway_numbers WHERE phone_number = ?", (to,)).fetchone()
    if not gateway_row:
        conn.close()
        return jsonify({"error": "Invalid recipient. Only pre-defined admin numbers are allowed."}), 400

    if allowed != "*" and to not in allowed.split(","):
        conn.close()
        return jsonify({"error": "You are not authorized to send messages to this number."}), 400

    # The customer/account number being asked about, embedded in the message
    # text itself (e.g. "BAL 9876543210"). Stored alongside the message so
    # conversations/location history can be grouped by this number instead of
    # by whichever gateway number the operator happens to reply from.
    target_number = extract_first(text)

    if sim_operator == "ALL_OPERATORS":
        # Fetch all gateway numbers
        rows = conn.execute("SELECT phone_number, operator_name FROM gateway_numbers").fetchall()
        
        # Filter matching operators
        operators_to_send = []
        for r in rows:
            phone = r["phone_number"]
            operator = r["operator_name"]
            if allowed == "*" or phone in allowed.split(","):
                operators_to_send.append(operator)
        
        if not operators_to_send:
            operators_to_send = [""]
        
        inserted_ids = []
        base_time = int(time.time() * 1000)
        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        
        for idx, op in enumerate(operators_to_send):
            msg_id = f"MSG-{base_time}-{idx}"
            conn.execute(
                """
                INSERT INTO messages (id, direction, sender, recipient, text, time, status, owner, sim_operator, target_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (msg_id, "out", username, to, text, time_str, "queued", username, op, target_number),
            )
            inserted_ids.append(msg_id)
        
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message_id": inserted_ids[0]}), 200
    else:
        msg_id = f"MSG-{int(time.time() * 1000)}"
        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            INSERT INTO messages (id, direction, sender, recipient, text, time, status, owner, sim_operator, target_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (msg_id, "out", username, to, text, time_str, "queued", username, sim_operator, target_number),
        )
        conn.commit()
        conn.close()

        return jsonify({"success": True, "message_id": msg_id}), 200


@coworker_bp.route("/schedules", methods=["GET", "POST"])
def coworker_schedules():
    username, err = _require_coworker()
    if err:
        return err

    conn = get_db()

    if request.method == "GET":
        rows = conn.execute(
            "SELECT * FROM schedules WHERE owner = ? ORDER BY created_at DESC",
            (username,),
        ).fetchall()
        conn.close()
        return jsonify({"schedules": [dict(r) for r in rows]}), 200

    data = request.json or {}
    to = data.get("to")
    text = data.get("text")
    sim_operator = data.get("sim_operator", "").strip()
    frequency_minutes = data.get("frequency_minutes")
    start_raw = data.get("start_at")
    end_raw = data.get("end_at")

    if not to or not text:
        conn.close()
        return jsonify({"error": "Receiver and message are required"}), 400

    try:
        frequency_minutes = int(frequency_minutes)
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"error": "Invalid frequency"}), 400
    if frequency_minutes not in ALLOWED_FREQUENCIES_MINUTES:
        conn.close()
        return jsonify({"error": "Unsupported frequency"}), 400

    start_at = _parse_datetime(start_raw)
    end_at = _parse_datetime(end_raw)
    if not start_at or not end_at:
        conn.close()
        return jsonify({"error": "start_at and end_at are required (YYYY-MM-DDTHH:MM)"}), 400
    if end_at <= start_at:
        conn.close()
        return jsonify({"error": "end_at must be after start_at"}), 400

    # Same authorization rules as a manual send: the number must be a
    # registered gateway number and (if restricted) one this user is allowed
    # to use.
    user_row = conn.execute("SELECT allowed_numbers FROM users WHERE username = ?", (username,)).fetchone()
    if not user_row:
        conn.close()
        return jsonify({"error": "User profile not found"}), 404
    allowed = user_row["allowed_numbers"] or "*"

    if sim_operator != "ALL_OPERATORS":
        gateway_row = conn.execute("SELECT phone_number FROM gateway_numbers WHERE phone_number = ?", (to,)).fetchone()
        if not gateway_row:
            conn.close()
            return jsonify({"error": "Invalid recipient. Only pre-defined admin numbers are allowed."}), 400
    if allowed != "*" and to not in allowed.split(","):
        conn.close()
        return jsonify({"error": "You are not authorized to send messages to this number."}), 400

    schedule_id = uuid.uuid4().hex[:12]
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    start_at_str = start_at.strftime("%Y-%m-%d %H:%M:%S")
    end_at_str = end_at.strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """
        INSERT INTO schedules
            (id, owner, recipient, text, sim_operator, frequency_minutes, start_at, end_at, next_run_at, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (schedule_id, username, to, text, sim_operator, frequency_minutes, start_at_str, end_at_str, start_at_str, now_str),
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True, "schedule_id": schedule_id}), 200


@coworker_bp.route("/schedules/<schedule_id>", methods=["DELETE"])
def coworker_cancel_schedule(schedule_id):
    username, err = _require_coworker()
    if err:
        return err

    conn = get_db()
    row = conn.execute("SELECT owner FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Schedule not found"}), 404
    if row["owner"] != username:
        conn.close()
        return jsonify({"error": "Unauthorized"}), 403

    conn.execute("UPDATE schedules SET status = 'cancelled' WHERE id = ?", (schedule_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 200