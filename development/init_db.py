import sqlite3

conn = sqlite3.connect("photos.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    image BLOB
)
""")

conn.commit()
conn.close()

print("Datenbank erstellt.")
