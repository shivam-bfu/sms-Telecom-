from flask import Blueprint, render_template, send_from_directory, current_app, request

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        from .device import device_incoming_message
        return device_incoming_message()
    return render_template("app.html")


@pages_bp.route("/admin")
def admin_page():
    return render_template("admin.html")


@pages_bp.route("/manifest.json")
def manifest():
    return send_from_directory(current_app.static_folder, "manifest.json")


@pages_bp.route("/sw.js")
def service_worker():
    return send_from_directory(current_app.static_folder, "sw.js")
