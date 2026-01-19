# -*- coding: utf-8 -*-

from flask import (
    Flask, render_template, Response, request,
    render_template_string, redirect, url_for, send_file, jsonify
)
import serial
import json
from datetime import datetime
import threading
import queue
import time
import os
import subprocess
import sqlite3
import io

from gpiozero import MotionSensor, RGBLED

# ------------------ PATHS ------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PHOTOS_DB = os.path.join(BASE_DIR, "photos.db")   # only photos
EVENTS_DB = os.path.join(BASE_DIR, "events.db")   # only motion + rfid logs

PHOTO_DIR = os.path.join(BASE_DIR, "static", "photos")  # absolute

# ------------------ CONFIG ------------------
SERIAL_PORT = "/dev/ttyACM0"
BAUDRATE = 9600

# Limits
MAX_PHOTOS = 10
MAX_EVENTS = 1000

# PIR
PIR_PIN = 18
MOTION_COOLDOWN_SECONDS = 2.0

# Camera
CAMERA_DEVICE = "/dev/video0"
PHOTO_RESOLUTION = "1280x720"

# RGB LED Pins
RGB_RED_PIN = 21
RGB_GREEN_PIN = 20
RGB_BLUE_PIN = 26

LED_FEEDBACK_SECONDS = 1.0
LED_IDLE_COLOR = (0, 0, 1)  # blue

ALLOWED_UIDS = {
    "333647F7": "Blauer Chip",
    "61D1AA17": "Weisse Karte",
    "04E0391AC16680": "Angelausweis"
}

# ------------------ APP ------------------
app = Flask(__name__)
event_queue = queue.Queue()

# ------------------ RGB LED ------------------
rgb_led = None
_led_lock = threading.Lock()
_led_timer = None


def init_rgb_led(active_high=True):
    global rgb_led
    try:
        rgb_led = RGBLED(
            red=RGB_RED_PIN,
            green=RGB_GREEN_PIN,
            blue=RGB_BLUE_PIN,
            active_high=active_high
        )
        rgb_led.color = LED_IDLE_COLOR
        print("[LED] RGB ready (idle=blue)")
    except Exception as e:
        print("[LED] init failed:", e)
        rgb_led = None


def _set_idle_blue():
    global _led_timer
    with _led_lock:
        if rgb_led:
            rgb_led.color = LED_IDLE_COLOR
        _led_timer = None


def led_feedback(color, seconds=LED_FEEDBACK_SECONDS):
    global _led_timer
    with _led_lock:
        if not rgb_led:
            return

        if _led_timer:
            try:
                _led_timer.cancel()
            except Exception:
                pass
            _led_timer = None

        if color == "GREEN":
            rgb_led.color = (0, 1, 0)
        elif color == "RED":
            rgb_led.color = (1, 0, 0)
        else:
            rgb_led.color = LED_IDLE_COLOR
            return

        _led_timer = threading.Timer(seconds, _set_idle_blue)
        _led_timer.daemon = True
        _led_timer.start()


def led_set_white():
    with _led_lock:
        if rgb_led:
            rgb_led.color = (1, 1, 1)


def led_set_idle_blue():
    with _led_lock:
        if rgb_led:
            rgb_led.color = LED_IDLE_COLOR


# ------------------ DB: PHOTOS ------------------
def get_photos_db():
    return sqlite3.connect(PHOTOS_DB)


def init_photos_db():
    conn = get_photos_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            image BLOB,
            created_at TEXT,
            mime TEXT
        )
    """)
    conn.commit()

    # If DB existed without mime column, try to add it
    try:
        cur.execute("ALTER TABLE photos ADD COLUMN mime TEXT")
        conn.commit()
    except Exception:
        pass

    conn.close()


def ensure_photo_dir():
    os.makedirs(PHOTO_DIR, exist_ok=True)


def photo_rel_to_abs(rel_path: str):
    # rel_path like: "photos/2026-...jpg"
    if not rel_path:
        return None
    rel_path = rel_path.replace("\\", "/")
    if rel_path.startswith("photos/"):
        return os.path.join(BASE_DIR, "static", rel_path)
    return None


def trim_photos_db(max_rows=MAX_PHOTOS):
    """
    Keep only latest max_rows photos. Delete oldest from DB.
    Also delete related motion files from static/photos if filename matches "motion_*.jpg"
    (Uploads normally have arbitrary filenames and are DB-only.)
    """
    conn = get_photos_db()
    cur = conn.cursor()

    # How many rows?
    cur.execute("SELECT COUNT(*) FROM photos")
    count = cur.fetchone()[0] or 0
    excess = count - int(max_rows)

    if excess > 0:
        # Fetch oldest rows to delete (id + filename)
        cur.execute(
            "SELECT id, filename FROM photos ORDER BY id ASC LIMIT ?",
            (excess,)
        )
        to_delete = cur.fetchall()

        # Delete those rows
        ids = [row[0] for row in to_delete]
        cur.executemany("DELETE FROM photos WHERE id = ?", [(i,) for i in ids])
        conn.commit()

        # Best-effort: delete matching motion files
        for _id, fn in to_delete:
            try:
                # We store DB filenames like "motion_YYYY-mm-dd...jpg" for motion photos
                if isinstance(fn, str) and fn.startswith("motion_") and fn.endswith(".jpg"):
                    # motion file saved by camera was named by timestamp without "motion_"
                    # so we cannot map perfectly. We keep files unless you want aggressive cleanup.
                    pass
            except Exception:
                pass

    conn.close()


def insert_photo_to_db(filename, image_bytes, mime="image/jpeg"):
    if not image_bytes:
        return
    conn = get_photos_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO photos (filename, image, created_at, mime) VALUES (?, ?, ?, ?)",
        (filename, image_bytes, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mime)
    )
    conn.commit()
    conn.close()

    # enforce limit
    try:
        trim_photos_db(MAX_PHOTOS)
    except Exception as e:
        print("[PHOTOS] trim failed:", e)


# ------------------ DB: EVENTS ------------------
def get_events_db():
    return sqlite3.connect(EVENTS_DB)


def init_events_db():
    conn = get_events_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            type TEXT,
            status TEXT,
            uid TEXT,
            name TEXT,
            photo TEXT,
            payload TEXT
        )
    """)
    conn.commit()
    conn.close()


def normalize_event(entry: dict) -> dict:
    e = dict(entry) if isinstance(entry, dict) else {}
    if "timestamp" not in e:
        e["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "type" not in e:
        e["type"] = "UNKNOWN"
    if "status" not in e:
        e["status"] = None
    if "uid" not in e:
        e["uid"] = None
    if "name" not in e:
        e["name"] = None
    if "photo" not in e:
        e["photo"] = None
    return e


def trim_events_db(max_rows=MAX_EVENTS):
    conn = get_events_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM events")
    count = cur.fetchone()[0] or 0
    excess = count - int(max_rows)

    if excess > 0:
        # delete oldest ids
        cur.execute(
            "SELECT id FROM events ORDER BY id ASC LIMIT ?",
            (excess,)
        )
        ids = [r[0] for r in cur.fetchall()]
        cur.executemany("DELETE FROM events WHERE id = ?", [(i,) for i in ids])
        conn.commit()

    conn.close()


def insert_event_to_db(entry):
    e = normalize_event(entry)
    created_at = e.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(e, ensure_ascii=False)

    conn = get_events_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events (created_at, type, status, uid, name, photo, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (created_at, e.get("type"), e.get("status"), e.get("uid"), e.get("name"), e.get("photo"), payload)
    )
    conn.commit()
    conn.close()

    # enforce limit
    try:
        trim_events_db(MAX_EVENTS)
    except Exception as e:
        print("[EVENTS] trim failed:", e)


def get_last_events(limit=300):
    conn = get_events_db()
    cur = conn.cursor()
    cur.execute("SELECT payload FROM events ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()

    out = []
    for (payload,) in rows:
        try:
            e = json.loads(payload) if payload else {}
        except Exception:
            e = {}
        out.append(normalize_event(e))
    return out


# ------------------ GALLERY ------------------
GALLERY_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Foto Galerie</title>
</head>
<body>
<h1>Foto hochladen</h1>
<form method="post" enctype="multipart/form-data">
  <input type="file" name="photo" required>
  <input type="submit" value="Upload">
</form>

<h2>Galerie</h2>
{% for photo in photos %}
  <div style="display:inline-block;margin:6px;text-align:center;">
    <img src="{{ url_for('get_photo', photo_id=photo[0]) }}" width="200"><br>
    <small>{{ photo[1] }}</small>
  </div>
{% endfor %}
</body>
</html>
"""


@app.route("/gallery", methods=["GET", "POST"])
def gallery():
    if request.method == "POST":
        file = request.files.get("photo")
        if file:
            insert_photo_to_db(
                filename=file.filename,
                image_bytes=file.read(),
                mime=(file.mimetype or "application/octet-stream")
            )
        return redirect(url_for("gallery"))

    conn = get_photos_db()
    cur = conn.cursor()
    cur.execute("SELECT id, filename FROM photos ORDER BY id DESC")
    photos = cur.fetchall()
    conn.close()
    return render_template_string(GALLERY_HTML, photos=photos)


@app.route("/photo/<int:photo_id>")
def get_photo(photo_id):
    conn = get_photos_db()
    cur = conn.cursor()
    cur.execute("SELECT image, COALESCE(mime,'image/jpeg') FROM photos WHERE id=?", (photo_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return ("Not found", 404)

    image_bytes, mime = row
    return send_file(io.BytesIO(image_bytes), mimetype=mime)


# ------------------ HOME ------------------
@app.route("/")
def home():
    entries = get_last_events(limit=500)
    return render_template("index.html", entries=entries)


# ------------------ DEBUG ------------------
@app.route("/debug/events")
def debug_events():
    return jsonify(get_last_events(limit=20))


# ------------------ LIVE EVENTS (SSE) ------------------
@app.route("/events")
def events():
    def stream():
        while True:
            e = event_queue.get()
            yield "data: " + json.dumps(e, ensure_ascii=False) + "\n\n"
    return Response(stream(), mimetype="text/event-stream")


# ------------------ CAMERA ------------------
def take_photo_fswebcam():
    ensure_photo_dir()
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = ts + ".jpg"
    full_path = os.path.join(PHOTO_DIR, filename)
    rel_path = os.path.join("photos", filename)

    cmd = [
        "fswebcam",
        "-q",
        "-d", CAMERA_DEVICE,
        "-r", PHOTO_RESOLUTION,
        "--no-banner",
        full_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not os.path.exists(full_path):
        return None, None

    try:
        with open(full_path, "rb") as f:
            image_bytes = f.read()
    except Exception:
        image_bytes = None

    return rel_path, image_bytes


# ------------------ THREAD WRAPPERS ------------------
def rfid_listener_forever():
    while True:
        try:
            rfid_listener()
        except Exception as e:
            print("[RFID] crashed:", e)
            time.sleep(2)


def motion_listener_forever():
    while True:
        try:
            motion_listener()
        except Exception as e:
            print("[MOTION] crashed:", e)
            time.sleep(2)


# ------------------ RFID ------------------
def rfid_listener():
    ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
    print("[RFID] ready on", SERIAL_PORT)

    while True:
        line = ser.readline().decode("utf-8", errors="ignore").strip()
        if not line or not line.startswith("UID:"):
            continue

        uid = line.replace("UID:", "").strip()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if uid in ALLOWED_UIDS:
            entry = {
                "type": "RFID",
                "timestamp": ts,
                "uid": uid,
                "name": ALLOWED_UIDS[uid],
                "status": "AUTH",
                "photo": None
            }
            led_feedback("GREEN")
            ser.write(b"AUTH\n")
        else:
            entry = {
                "type": "RFID",
                "timestamp": ts,
                "uid": uid,
                "name": "Unbekannt",
                "status": "DENY",
                "photo": None
            }
            led_feedback("RED")
            ser.write(b"DENY\n")

        entry = normalize_event(entry)
        insert_event_to_db(entry)
        event_queue.put(entry)


# ------------------ MOTION + PHOTO ------------------
def motion_listener():
    pir = MotionSensor(PIR_PIN)
    print("[MOTION] ready on GPIO", PIR_PIN)

    last = 0.0

    def photo_worker(base_event):
        led_set_white()
        try:
            rel_path, img_bytes = take_photo_fswebcam()
        finally:
            led_set_idle_blue()

        if img_bytes:
            db_filename = "motion_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".jpg"
            insert_photo_to_db(db_filename, img_bytes, mime="image/jpeg")

        upd = dict(base_event)
        upd["type"] = "MOTION_PHOTO"
        upd["photo"] = rel_path

        upd = normalize_event(upd)
        insert_event_to_db(upd)
        event_queue.put(upd)

    while True:
        pir.wait_for_motion()
        now = time.time()
        if now - last < MOTION_COOLDOWN_SECONDS:
            continue
        last = now

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        base = normalize_event({
            "type": "MOTION",
            "timestamp": ts,
            "status": "DETECTED",
            "photo": None,
            "uid": None,
            "name": None
        })

        insert_event_to_db(base)
        event_queue.put(base)

        threading.Thread(target=photo_worker, args=(base,), daemon=True).start()
        pir.wait_for_no_motion()


# ------------------ START ------------------
init_photos_db()
init_events_db()
init_rgb_led(active_high=True)

threading.Thread(target=rfid_listener_forever, daemon=True).start()
threading.Thread(target=motion_listener_forever, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)

