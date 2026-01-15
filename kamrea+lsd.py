# -*- coding: utf-8 -*-

from flask import Flask, render_template, Response, request, render_template_string, redirect, url_for, send_file
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

# ------------------ CONFIG ------------------
SERIAL_PORT = "/dev/ttyACM0"
BAUDRATE = 9600

JSON_FILE = "anmeldeversuche.json"
DB = "photos.db"

# PIR
PIR_PIN = 18
MOTION_COOLDOWN_SECONDS = 2.0

# Camera
CAMERA_DEVICE = "/dev/video0"
PHOTO_DIR = os.path.join("static", "photos")
PHOTO_RESOLUTION = "1280x720"

# RGB LED Pins
RGB_RED_PIN = 21
RGB_GREEN_PIN = 20
RGB_BLUE_PIN = 26

LED_FEEDBACK_SECONDS = 1.0
LED_IDLE_COLOR = (0, 0, 1)   # blau

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

# ------------------ DB / GALLERY ------------------
HTML = """
<!doctype html>
<title>Foto Galerie</title>
<h1>Foto hochladen</h1>
<form method="post" enctype="multipart/form-data">
  <input type="file" name="photo" required>
  <input type="submit" value="Upload">
</form>
<h2>Galerie</h2>
{% for photo in photos %}
  <img src="{{ url_for('get_photo', photo_id=photo[0]) }}" width="200">
{% endfor %}
"""

def get_db():
    return sqlite3.connect(DB)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            image BLOB
        )
    """)
    conn.commit()
    conn.close()

@app.route("/gallery", methods=["GET", "POST"])
def gallery():
    if request.method == "POST":
        file = request.files.get("photo")
        if file:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO photos (filename, image) VALUES (?, ?)",
                (file.filename, file.read())
            )
            conn.commit()
            conn.close()
        return redirect(url_for("gallery"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM photos")
    photos = cur.fetchall()
    conn.close()
    return render_template_string(HTML, photos=photos)

@app.route("/photo/<int:photo_id>")
def get_photo(photo_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT image FROM photos WHERE id=?", (photo_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return ("Not found", 404)
    return send_file(io.BytesIO(row[0]), mimetype="image/jpeg")

# ------------------ JSON ------------------
def load_json():
    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def save_entry(entry):
    try:
        data = load_json()
        data.append(entry)
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print("[JSON] error:", e)

# ------------------ CAMERA ------------------
def ensure_photo_dir():
    os.makedirs(PHOTO_DIR, exist_ok=True)

def take_photo_fswebcam():
    ensure_photo_dir()
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{ts}.jpg"
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
    return rel_path if os.path.exists(full_path) else None

# ------------------ FLASK ROUTES ------------------
@app.route("/")
def home():
    return render_template("index.html", entries=load_json())

@app.route("/events")
def events():
    def stream():
        while True:
            e = event_queue.get()
            yield "data: " + json.dumps(e, ensure_ascii=False) + "\n\n"
    return Response(stream(), mimetype="text/event-stream")

# ------------------ RFID ------------------
def rfid_listener():
    ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
    print("[RFID] ready")

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
                "status": "AUTH"
            }
            led_feedback("GREEN")
            ser.write(b"AUTH\n")
        else:
            entry = {
                "type": "RFID",
                "timestamp": ts,
                "uid": uid,
                "name": "Unbekannt",
                "status": "DENY"
            }
            led_feedback("RED")
            ser.write(b"DENY\n")

        save_entry(entry)
        event_queue.put(entry)

# ------------------ MOTION + PHOTO ------------------
def motion_listener():
    pir = MotionSensor(PIR_PIN)
    last = 0.0

    def photo_worker(base):
        led_set_white()
        try:
            photo = take_photo_fswebcam()
        finally:
            led_set_idle_blue()

        upd = dict(base)
        upd["type"] = "MOTION_PHOTO"
        upd["photo"] = photo
        save_entry(upd)
        event_queue.put(upd)

    while True:
        pir.wait_for_motion()
        now = time.time()
        if now - last < MOTION_COOLDOWN_SECONDS:
            continue
        last = now

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        base = {
            "type": "MOTION",
            "timestamp": ts,
            "status": "DETECTED",
            "photo": None
        }
        save_entry(base)
        event_queue.put(base)

        threading.Thread(target=photo_worker, args=(base,), daemon=True).start()
        pir.wait_for_no_motion()

# ------------------ START ------------------
init_db()
init_rgb_led(active_high=True)  # wenn Farben falsch: False

threading.Thread(target=rfid_listener, daemon=True).start()
threading.Thread(target=motion_listener, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)

