# -*- coding: utf-8 -*-

from flask import Flask, render_template, Response
import serial
import json
from datetime import datetime
import threading
import queue
import time
import os
import subprocess

from gpiozero import MotionSensor

SERIAL_PORT = "/dev/ttyACM0"
BAUDRATE = 9600
JSON_FILE = "anmeldeversuche.json"

PIR_PIN = 18
MOTION_COOLDOWN_SECONDS = 2.0

# Kamera (fest auf video0)
CAMERA_DEVICE = "/dev/video0"
PHOTO_DIR = os.path.join("static", "photos")
PHOTO_RESOLUTION = "1280x720"

ALLOWED_UIDS = {
    "333647F7": "Blauer Chip",
    "61D1AA17": "Weisse Karte",
    "04E0391AC16680": "Angelausweis"
}

app = Flask(__name__)
event_queue = queue.Queue()


def load_json():
    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []
    except Exception as e:
        print("[JSON] load_json error:", e)
        return []


def save_entry(entry):
    try:
        data = load_json()
        data.append(entry)
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print("[JSON] save_entry error:", e)


def ensure_photo_dir():
    try:
        os.makedirs(PHOTO_DIR, exist_ok=True)
    except Exception as e:
        print("[CAM] Cannot create photo dir:", e)


def take_photo_fswebcam():
    """
    Returns relative path like 'photos/2026-01-13_12-34-56.jpg' or None.
    """
    ensure_photo_dir()

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{ts}.jpg"
    full_path = os.path.join(PHOTO_DIR, filename)
    rel_path = os.path.join("photos", filename).replace("\\", "/")

    try:
        cmd = [
            "fswebcam",
            "-q",
            "-d", CAMERA_DEVICE,
            "-r", PHOTO_RESOLUTION,
            "--no-banner",
            full_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if res.returncode == 0 and os.path.exists(full_path) and os.path.getsize(full_path) > 0:
            return rel_path

        print("[CAM] fswebcam failed:", (res.stderr or "").strip())
    except Exception as e:
        print("[CAM] fswebcam error:", e)

    return None


@app.route("/")
def index():
    return render_template("index.html", entries=load_json())


@app.route("/events")
def events():
    def stream():
        while True:
            entry = event_queue.get()
            yield "data: " + json.dumps(entry, ensure_ascii=False) + "\n\n"
    return Response(stream(), mimetype="text/event-stream")


# ===== RFID Listener =====
def rfid_listener():
    ser = None
    while ser is None:
        try:
            ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
            print("[RFID] Serial opened:", SERIAL_PORT)
        except Exception as e:
            print("[RFID] Cannot open serial:", e)
            time.sleep(2)

    while True:
        try:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            if line.startswith("UID:"):
                uid = line.replace("UID:", "").strip()
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if uid in ALLOWED_UIDS:
                    entry = {
                        "type": "RFID",
                        "timestamp": timestamp,
                        "uid": uid,
                        "name": ALLOWED_UIDS[uid],
                        "status": "AUTH"
                    }
                    try:
                        ser.write(b"AUTH\n")
                    except Exception:
                        pass
                else:
                    entry = {
                        "type": "RFID",
                        "timestamp": timestamp,
                        "uid": uid,
                        "name": "Unbekannt",
                        "status": "DENY"
                    }
                    try:
                        ser.write(b"DENY\n")
                    except Exception:
                        pass

                save_entry(entry)
                event_queue.put(entry)

        except Exception as e:
            print("[RFID] Listener error:", e)
            time.sleep(1)


# ===== Motion + Foto (Foto async, blockiert nichts) =====
def motion_listener():
    try:
        pir = MotionSensor(PIR_PIN)
        print("[PIR] MotionSensor active on GPIO", PIR_PIN)
    except Exception as e:
        print("[PIR] Cannot start MotionSensor:", e)
        return

    last = 0.0

    def photo_worker(base_entry):
        photo_rel = take_photo_fswebcam()
        update = dict(base_entry)
        update["photo"] = photo_rel
        update["type"] = "MOTION_PHOTO"  # separates event, frontend kann updaten
        save_entry(update)
        event_queue.put(update)

    while True:
        try:
            pir.wait_for_motion()

            now = time.time()
            if now - last < MOTION_COOLDOWN_SECONDS:
                time.sleep(0.05)
                continue
            last = now

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            base_entry = {
                "type": "MOTION",
                "timestamp": timestamp,
                "status": "DETECTED",
                "photo": None
            }

            # sofort in Log + Website
            save_entry(base_entry)
            event_queue.put(base_entry)

            # Foto in eigenem Thread
            threading.Thread(target=photo_worker, args=(base_entry,), daemon=True).start()

            pir.wait_for_no_motion()

        except Exception as e:
            print("[PIR] Listener error:", e)
            time.sleep(1)


threading.Thread(target=rfid_listener, daemon=True).start()
threading.Thread(target=motion_listener, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
