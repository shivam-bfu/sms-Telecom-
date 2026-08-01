"""
Runs recurring/date-range searches created by coworkers or the admin.

A row in the `schedules` table means: "send this exact text to this
recipient every `frequency_minutes`, starting at start_at, until end_at".
Every tick we insert one 'queued' outgoing message per due schedule (reusing
the same insert shape as coworker_send_sms / admin so the Android node picks
it up exactly like a normal manual send), then push next_run_at forward. Once
next_run_at would land after end_at, the schedule is marked 'completed' so it
stops firing.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from .db import get_db
from .routes.phone_extractor import extract_first

logger = logging.getLogger(__name__)

# Keep in sync with the frequency options offered in the UI.
ALLOWED_FREQUENCIES_MINUTES = {5, 15, 45, 60, 120, 240, 480, 720, 1440}


def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse(dt_str):
    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")


def run_due_schedules():
    """Called on a timer (see register_scheduler). Safe to call concurrently
    since each due schedule is processed in its own commit."""
    try:
        conn = get_db()
        now_str = _now_str()
        due = conn.execute(
            """
            SELECT * FROM schedules
            WHERE status = 'active' AND next_run_at <= ? AND start_at <= ?
            """,
            (now_str, now_str),
        ).fetchall()

        for sched in due:
            try:
                _fire_schedule(conn, sched, now_str)
            except Exception:
                logger.exception("Failed to process schedule %s", sched["id"])
        conn.close()
    except Exception:
        logger.exception("run_due_schedules failed")


def _fire_schedule(conn, sched, now_str):
    # Resolve which operators to actually send to, same semantics as the
    # manual /api/send ALL_OPERATORS option.
    if sched["sim_operator"] == "ALL_OPERATORS":
        rows = conn.execute("SELECT operator_name FROM gateway_numbers").fetchall()
        operators = [r["operator_name"] for r in rows] or [""]
    else:
        operators = [sched["sim_operator"] or ""]

    base_id = f"SCH-{sched['id']}-{int(time.time() * 1000)}"
    target_number = extract_first(sched["text"])
    for idx, op in enumerate(operators):
        msg_id = base_id if idx == 0 else f"{base_id}-{idx}"
        conn.execute(
            """
            INSERT INTO messages (id, direction, sender, recipient, text, time, status, owner, sim_operator, target_number)
            VALUES (?, 'out', ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (msg_id, sched["owner"], sched["recipient"], sched["text"], now_str, sched["owner"], op, target_number),
        )

    next_run = _parse(sched["next_run_at"]) + timedelta(minutes=sched["frequency_minutes"])
    end_at = _parse(sched["end_at"])

    if next_run > end_at:
        conn.execute(
            "UPDATE schedules SET status = 'completed', last_run_at = ? WHERE id = ?",
            (now_str, sched["id"]),
        )
    else:
        conn.execute(
            "UPDATE schedules SET next_run_at = ?, last_run_at = ? WHERE id = ?",
            (next_run.strftime("%Y-%m-%d %H:%M:%S"), now_str, sched["id"]),
        )
    conn.commit()


def register_scheduler(app):
    """Start a background job that ticks every 60s inside the app context.
    Only one process/worker should run this (fine for Railway's default
    single-instance web service; if you scale to >1 replica, move this to a
    dedicated worker process instead so schedules don't fire multiple times).
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(daemon=True)

    def _tick():
        with app.app_context():
            run_due_schedules()

    scheduler.add_job(_tick, "interval", seconds=60, id="run_due_schedules", replace_existing=True)
    scheduler.start()
    return scheduler
