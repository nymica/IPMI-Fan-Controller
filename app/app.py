import os
import sqlite3
import subprocess
import json
import re
from flask import Flask, request, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

app = Flask(__name__)
DB_PATH = "/data/servers.db"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 623,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                fan_speed INTEGER NOT NULL DEFAULT 20,
                manual_fan_control INTEGER NOT NULL DEFAULT 1,
                disable_3rd_party_fan INTEGER NOT NULL DEFAULT 1,
                auto_apply INTEGER NOT NULL DEFAULT 1,
                server_model TEXT NOT NULL DEFAULT 'R730xd',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate existing databases that predate the server_model column
        try:
            conn.execute("ALTER TABLE servers ADD COLUMN server_model TEXT NOT NULL DEFAULT 'R730xd'")
        except Exception:
            pass
        conn.commit()


# ---------------------------------------------------------------------------
# ipmitool helpers
# ---------------------------------------------------------------------------

def run_ipmi(host, port, user, password, *raw_args, timeout=10):
    cmd = [
        "ipmitool", "-I", "lanplus",
        "-H", host, "-p", str(port),
        "-U", user, "-P", password,
    ] + list(raw_args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def set_manual_fan_control(host, port, user, password, enabled: bool):
    flag = "0x00" if enabled else "0x01"
    return run_ipmi(host, port, user, password, "raw", "0x30", "0x30", "0x01", flag)


def set_fan_speed(host, port, user, password, percent: int):
    hex_speed = hex(max(0, min(100, percent)))
    return run_ipmi(host, port, user, password, "raw", "0x30", "0x30", "0x02", "0xff", hex_speed)


def set_3rd_party_fan(host, port, user, password, disabled: bool):
    # 0x01 = disable 3rd-party fan ramp-up, 0x00 = re-enable default
    flag = "0x01" if disabled else "0x00"
    return run_ipmi(
        host, port, user, password,
        "raw", "0x30", "0xce",
        flag,
        "0x16", "0x05", "0x00", "0x00", "0x00",
        "0x00", "0x00", "0x00", "0x00", "0x00",
        "0x00", "0x00", "0x00", "0x00", "0x00",
        "0x00", "0x00", "0x00", "0x00", "0x00",
        "0x00", "0x00", "0x00", "0x00", "0x00",
    )


def get_fan_readings(host, port, user, password):
    rc, out, err = run_ipmi(host, port, user, password, "sdr", "type", "Fan")
    fans = []
    if rc == 0:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                fans.append({"name": parts[0], "value": parts[1], "status": parts[2]})
    return fans, err if rc != 0 else None


# 12th-gen iDRAC6 servers don't support the OEM 0x30/0xce 3rd-party PCIe fan ramp-up command
_GEN12_MODELS = {'R320', 'R420', 'R520', 'R620', 'R720', 'R720xd', 'R820', 'R920'}


def apply_server_settings(server):
    host, port, user, pw = server["host"], server["port"], server["username"], server["password"]
    results = {}

    if server["manual_fan_control"]:
        rc, _, err = set_manual_fan_control(host, port, user, pw, True)
        results["manual_fan_control"] = "ok" if rc == 0 else err
        if rc == 0:
            rc, _, err = set_fan_speed(host, port, user, pw, server["fan_speed"])
            results["fan_speed"] = "ok" if rc == 0 else err
    else:
        rc, _, err = set_manual_fan_control(host, port, user, pw, False)
        results["manual_fan_control"] = "ok (auto)" if rc == 0 else err

    if server.get("server_model", "") not in _GEN12_MODELS:
        rc, _, err = set_3rd_party_fan(host, port, user, pw, bool(server["disable_3rd_party_fan"]))
        results["3rd_party_fan"] = "ok" if rc == 0 else err

    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/servers", methods=["GET"])
def list_servers():
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, host, port, username, fan_speed, manual_fan_control, disable_3rd_party_fan, auto_apply, server_model FROM servers").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/servers", methods=["POST"])
def create_server():
    data = request.json
    required = ["name", "host", "username", "password"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing required fields: name, host, username, password"}), 400
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO servers (name, host, port, username, password, fan_speed, manual_fan_control, disable_3rd_party_fan, auto_apply, server_model) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (data["name"], data["host"], int(data.get("port", 623)), data["username"], data["password"],
             int(data.get("fan_speed", 20)), int(data.get("manual_fan_control", 1)),
             int(data.get("disable_3rd_party_fan", 1)), int(data.get("auto_apply", 1)),
             data.get("server_model", "R730xd"))
        )
        conn.commit()
        server_id = cur.lastrowid
    return jsonify({"id": server_id}), 201


@app.route("/api/servers/<int:sid>", methods=["GET"])
def get_server(sid):
    with get_db() as conn:
        row = conn.execute("SELECT id, name, host, port, username, fan_speed, manual_fan_control, disable_3rd_party_fan, auto_apply, server_model FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@app.route("/api/servers/<int:sid>", methods=["PUT"])
def update_server(sid):
    data = request.json
    with get_db() as conn:
        row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        server = dict(row)
        fields = ["name", "host", "port", "username", "password", "fan_speed", "manual_fan_control", "disable_3rd_party_fan", "auto_apply", "server_model"]
        for f in fields:
            if f in data:
                server[f] = data[f]
        conn.execute(
            "UPDATE servers SET name=?, host=?, port=?, username=?, password=?, fan_speed=?, manual_fan_control=?, disable_3rd_party_fan=?, auto_apply=?, server_model=? WHERE id=?",
            (server["name"], server["host"], int(server["port"]), server["username"], server["password"],
             int(server["fan_speed"]), int(server["manual_fan_control"]),
             int(server["disable_3rd_party_fan"]), int(server["auto_apply"]),
             server.get("server_model", "R730xd"), sid)
        )
        conn.commit()
    return jsonify({"status": "updated"})


@app.route("/api/servers/<int:sid>", methods=["DELETE"])
def delete_server(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM servers WHERE id=?", (sid,))
        conn.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/servers/<int:sid>/apply", methods=["POST"])
def apply_settings(sid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    results = apply_server_settings(dict(row))
    return jsonify(results)


@app.route("/api/servers/<int:sid>/fans", methods=["GET"])
def fan_readings(sid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    s = dict(row)
    fans, err = get_fan_readings(s["host"], s["port"], s["username"], s["password"])
    if err:
        return jsonify({"error": err}), 502
    return jsonify(fans)


@app.route("/api/servers/<int:sid>/test", methods=["POST"])
def test_connection(sid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    s = dict(row)
    rc, out, err = run_ipmi(s["host"], s["port"], s["username"], s["password"], "chassis", "status", timeout=8)
    if rc == 0:
        return jsonify({"status": "ok", "output": out})
    return jsonify({"status": "error", "output": err}), 502


# ---------------------------------------------------------------------------
# Scheduler: auto-apply settings on startup and periodically
# ---------------------------------------------------------------------------

def auto_apply_all():
    with app.app_context():
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT * FROM servers WHERE auto_apply=1").fetchall()
            for row in rows:
                try:
                    apply_server_settings(dict(row))
                except Exception as e:
                    app.logger.warning(f"Auto-apply failed for server {row['name']}: {e}")
        except Exception as e:
            app.logger.warning(f"Auto-apply scheduler error: {e}")


if __name__ == "__main__":
    init_db()
    scheduler = BackgroundScheduler()
    # Re-apply settings every 5 minutes to handle iDRAC resets / reboots
    scheduler.add_job(auto_apply_all, "interval", minutes=5, id="auto_apply")
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    # Apply once at startup
    auto_apply_all()
    app.run(host="0.0.0.0", port=5000, debug=False)
