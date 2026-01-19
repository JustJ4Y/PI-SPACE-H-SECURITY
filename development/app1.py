from flask import Flask, request, render_template_string, redirect, url_for, send_file
import sqlite3
import io

app = Flask(__name__)

DB = "photos.db"

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

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        file = request.files["photo"]
        if file:
            image_bytes = file.read()

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO photos (filename, image) VALUES (?, ?)",
                (file.filename, image_bytes)
            )
            conn.commit()
            conn.close()

        return redirect(url_for("index"))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM photos")
    photos = cursor.fetchall()
    conn.close()

    return render_template_string(HTML, photos=photos)

@app.route("/photo/<int:photo_id>")
def get_photo(photo_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT image FROM photos WHERE id=?", (photo_id,))
    image = cursor.fetchone()[0]
    conn.close()

    return send_file(
        io.BytesIO(image),
        mimetype="image/jpeg"
    )

if __name__ == "__main__":
    app.run(debug=True)
