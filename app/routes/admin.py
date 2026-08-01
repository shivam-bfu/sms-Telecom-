import sqlite3
import io
import csv
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response
from werkzeug.security import generate_password_hash

from ..db import get_db
from ..security import check_admin_auth, verify_admin_password, set_admin_password
from ..config import Config
from ..extensions import limiter
from .location_resolver import trigger_enrichment

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


def _require_admin():
    """Returns an error response tuple if unauthorized, else None."""
    if not check_admin_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    return None


@admin_bp.route("/login", methods=["POST"])
@limiter.limit("5 per minute; 30 per hour")
def admin_login():
    data = request.json or {}
    if verify_admin_password(data.get("password")):
        return jsonify({"success": True}), 200
    return jsonify({"error": "Invalid admin password"}), 401


@admin_bp.route("/change-password", methods=["POST"])
@limiter.limit("10 per minute")
def admin_change_password():
    if (err := _require_admin()) is not None:
        return err

    data = request.json or {}
    new_password = data.get("new_password", "").strip()
    if not new_password:
        return jsonify({"error": "New password cannot be empty"}), 400

    set_admin_password(new_password)
    return jsonify({"success": True}), 200


@admin_bp.route("/status", methods=["GET"])
def admin_status():
    if (err := _require_admin()) is not None:
        return err

    conn = get_db()
    row = conn.execute("SELECT * FROM device_status LIMIT 1").fetchone()
    conn.close()

    if not row:
        return jsonify(
            {
                "status": "offline",
                "name": "No registered node",
                "battery": "0%",
                "version": "1.0",
                "last_seen": "Never",
                "secret_token": _mask_token(Config.DEVICE_TOKEN),
            }
        )

    status = row["status"]
    if row["last_seen"] != "Never":
        try:
            last_dt = datetime.strptime(row["last_seen"], "%Y-%m-%d %H:%M:%S")
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            if (now_utc - last_dt).total_seconds() > 180:
                status = "offline"
        except Exception:
            pass

    return jsonify(
        {
            "status": status,
            "name": row["name"],
            "battery": row["battery"],
            "version": row["version"],
            "last_seen": row["last_seen"],
            # Masked - the dashboard only needs to confirm which token is
            # configured, never to display it in full over the network.
            "secret_token": _mask_token(Config.DEVICE_TOKEN),
        }
    )


def _mask_token(token):
    """Show only the last 4 characters, e.g. '********ab12'. Never send the
    full device secret to the browser - it's a credential, not display data."""
    if not token:
        return ""
    return "*" * 8 + token[-4:]


@admin_bp.route("/users", methods=["GET", "POST"])
def admin_users():
    if (err := _require_admin()) is not None:
        return err

    conn = get_db()
    if request.method == "GET":
        rows = conn.execute("SELECT username, name, role, allowed_numbers FROM users").fetchall()
        conn.close()
        return jsonify({"users": [dict(r) for r in rows]})

    data = request.json or {}
    username = data.get("username", "").strip()
    name = data.get("name", "").strip()
    password = data.get("password", "").strip()
    allowed_numbers = data.get("allowed_numbers", "*").strip() or "*"

    if not username or not name or not password:
        conn.close()
        return jsonify({"error": "All fields are required"}), 400

    try:
        conn.execute(
            "INSERT INTO users (username, name, password_hash, allowed_numbers) VALUES (?, ?, ?, ?)",
            (username, name, generate_password_hash(password), allowed_numbers),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already exists"}), 400
    finally:
        conn.close()

    return jsonify({"success": True}), 200


@admin_bp.route("/users/<username>", methods=["PUT"])
def admin_update_user(username):
    if (err := _require_admin()) is not None:
        return err

    data = request.json or {}
    allowed_numbers = data.get("allowed_numbers", "*").strip() or "*"

    conn = get_db()
    conn.execute("UPDATE users SET allowed_numbers = ? WHERE username = ?", (allowed_numbers, username))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 200


@admin_bp.route("/users/<username>", methods=["DELETE"])
def admin_delete_user(username):
    if (err := _require_admin()) is not None:
        return err

    conn = get_db()
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 200


@admin_bp.route("/messages", methods=["GET"])
def admin_messages():
    if (err := _require_admin()) is not None:
        return err

    conn = get_db()
    rows = conn.execute("""
        SELECT m.*, u.name AS owner_name 
        FROM messages m 
        LEFT JOIN users u ON m.owner = u.username 
        ORDER BY m.time DESC, m.id DESC
    """).fetchall()
    conn.close()

    messages = []
    for r in rows:
        d = dict(r)
        if d.get("direction") == "in":
            owner_display = d.get("owner_name") or d.get("owner")
            if owner_display:
                d["recipient"] = owner_display
            else:
                d["recipient"] = "System"
        messages.append(d)
        trigger_enrichment(d["id"], d["text"])

    return jsonify({"messages": messages})


@admin_bp.route("/export", methods=["GET"])
def admin_export_messages():
    if (err := _require_admin()) is not None:
        return err

    # Optional filters mirroring the ones available on the Messages tab -
    # when one is active, the export only contains the matching rows instead
    # of the whole gateway log. "all" (or omitted) means no filtering.
    owner_filter = (request.args.get("owner") or "all").strip()
    direction_filter = (request.args.get("direction") or "all").strip()
    status_filter = (request.args.get("status") or "all").strip()
    search_q = (request.args.get("q") or "").strip().lower()

    conn = get_db()
    rows = conn.execute("""
        SELECT m.*, u.name AS owner_name 
        FROM messages m 
        LEFT JOIN users u ON m.owner = u.username 
        ORDER BY m.time DESC, m.id DESC
    """).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "ID", "Direction", "Sender", "Recipient", 
        "Message Text", "Timestamp", "Status", "Owner", "SIM Operator"
    ])

    exported_rows = 0
    for r in rows:
        direction_label = "Outgoing" if r["direction"] == "out" else "Incoming"
        recipient_display = r["recipient"]
        if r["direction"] == "in":
            owner_display = r["owner_name"] or r["owner"]
            recipient_display = owner_display if owner_display else "System"

        if owner_filter != "all" and (r["owner"] or "") != owner_filter:
            continue
        if direction_filter != "all" and r["direction"] != direction_filter:
            continue
        if status_filter != "all" and (r["status"] or "") != status_filter:
            continue
        if search_q:
            haystack = " ".join([
                r["text"] or "", r["sender"] or "", recipient_display or ""
            ]).lower()
            if search_q not in haystack:
                continue

        writer.writerow([
            r["id"],
            direction_label,
            r["sender"],
            recipient_display,
            r["text"],
            r["time"],
            r["status"],
            r["owner"] or "",
            r["sim_operator"] or ""
        ])
        exported_rows += 1

    csv_data = "\ufeff" + output.getvalue()
    # Give the file a name that reflects the applied filter so it's obvious
    # at a glance whether this is the full log or just one user's data.
    filename = f"sms_export_{owner_filter}.csv" if owner_filter != "all" else "sms_export_admin.csv"
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )


@admin_bp.route("/numbers", methods=["GET", "POST"])
def admin_numbers():
    if (err := _require_admin()) is not None:
        return err

    conn = get_db()
    if request.method == "GET":
        rows = conn.execute("SELECT phone_number, operator_name FROM gateway_numbers ORDER BY timestamp DESC").fetchall()
        conn.close()
        return jsonify({"numbers": [dict(r) for r in rows]})

    data = request.json or {}
    phone_number = data.get("phone_number", "").strip()
    operator_name = data.get("operator_name", "").strip()

    if not phone_number or not operator_name:
        conn.close()
        return jsonify({"error": "Phone number and operator name are required"}), 400

    try:
        conn.execute(
            "INSERT OR REPLACE INTO gateway_numbers (phone_number, operator_name) VALUES (?, ?)",
            (phone_number, operator_name),
        )
        conn.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

    return jsonify({"success": True}), 200


@admin_bp.route("/numbers/<phone_number>", methods=["DELETE"])
def admin_delete_number(phone_number):
    if (err := _require_admin()) is not None:
        return err

    conn = get_db()
    conn.execute("DELETE FROM gateway_numbers WHERE phone_number = ?", (phone_number,))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 200


@admin_bp.route("/schedules", methods=["GET"])
def admin_schedules():
    """List every coworker's periodic/date-range searches, newest first."""
    if (err := _require_admin()) is not None:
        return err

    conn = get_db()
    rows = conn.execute(
        """
        SELECT s.*, u.name AS owner_name
        FROM schedules s
        LEFT JOIN users u ON s.owner = u.username
        ORDER BY s.created_at DESC
        """
    ).fetchall()
    conn.close()
    return jsonify({"schedules": [dict(r) for r in rows]}), 200


@admin_bp.route("/schedules/<schedule_id>", methods=["DELETE"])
def admin_cancel_schedule(schedule_id):
    """Admin can cancel (stop) any coworker's schedule."""
    if (err := _require_admin()) is not None:
        return err

    conn = get_db()
    row = conn.execute("SELECT id FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Schedule not found"}), 404

    conn.execute("UPDATE schedules SET status = 'cancelled' WHERE id = ?", (schedule_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 200
