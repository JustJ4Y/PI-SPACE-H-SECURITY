# -*- coding: utf-8 -*-

from flask import Flask, render_template, Response
import serial
import json
from datetime import datetime
import threading
import queue
import time

from gpiozero import MotionSensor

SERIAL_PORT = "/dev/ttyACM0"
BAUDRATE = 9600
JSON_FILE = "anmeldeversuche.json"

PIR_PIN = 18
MOTION_COOLDOWN_SECONDS = 2.0  # spam-schutz, aber ohne wait_for_no_motion -> "snappy"

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


# ===== RFID Listener (BLEIBT) =====
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


# ===== PIR Motion Listener (SNAPPY, ohne wait_for_no_motion) =====
def motion_listener():
    try:
        pir = MotionSensor(PIR_PIN)
        print("[PIR] MotionSensor active on GPIO", PIR_PIN)
    except Exception as e:
        print("[PIR] Cannot start MotionSensor:", e)
        return

    last = 0.0

    while True:
        try:
            pir.wait_for_motion()

            now = time.time()
            if now - last < MOTION_COOLDOWN_SECONDS:
                continue
            last = now

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry = {
                "type": "MOTION",
                "timestamp": timestamp,
                "status": "DETECTED"
            }

            save_entry(entry)
            event_queue.put(entry)

            # Kein wait_for_no_motion -> OUT darf lange HIGH sein, wir bleiben trotzdem "schnell"
            # Cooldown verhindert Spam

        except Exception as e:
            print("[PIR] Listener error:", e)
            time.sleep(1)


threading.Thread(target=rfid_listener, daemon=True).start()
threading.Thread(target=motion_listener, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)

