from flask import Flask, render_template, Response
import serial
import json
from datetime import datetime
import threading
import queue

SERIAL_PORT = "/dev/ttyACM0"
BAUDRATE = 9600
JSON_FILE = "anmeldeversuche.json"

ALLOWED_UIDS = {
    "333647F7": "Blauer Chip",
    "61D1AA17": "Weisse Karte",
    "04E0391AC16680": "Angelausweis"
}

app = Flask(__name__)
event_queue = queue.Queue()

def load_json():
    try:
        with open(JSON_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def save_entry(entry):
    data = load_json()
    data.append(entry)
    with open(JSON_FILE, "w") as f:
        json.dump(data, f, indent=4)

@app.route("/")
def index():
    return render_template("index.html", entries=load_json())

@app.route("/events")
def events():
    def stream():
        while True:
            entry = event_queue.get()
            yield f"data: {json.dumps(entry)}\n\n"
    return Response(stream(), mimetype="text/event-stream")

# ===== RFID Listener =====
def rfid_listener():
    ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)

    while True:
        line = ser.readline().decode("utf-8", errors="ignore").strip()

        if line.startswith("UID:"):
            uid = line.replace("UID:", "")
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if uid in ALLOWED_UIDS:
                entry = {
                    "timestamp": timestamp,
                    "uid": uid,
                    "name": ALLOWED_UIDS[uid],
                    "status": "AUTH"
                }
                ser.write(b"AUTH\n")
            else:
                entry = {
                    "timestamp": timestamp,
                    "uid": uid,
                    "name": "Unbekannt",
                    "status": "DENY"
                }
                ser.write(b"DENY\n")

            save_entry(entry)
            event_queue.put(entry)

threading.Thread(target=rfid_listener, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)

