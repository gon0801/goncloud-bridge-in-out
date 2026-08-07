import sqlite3

# Ruta de la base de datos
DB = "/mnt/data/appdata/bridge/db/bridge.db"

# Conexión y cursor
con = sqlite3.connect(DB)
cur = con.cursor()

# Crear la tabla events
cur.execute("""CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    channel TEXT,
    item_count INTEGER,
    payload TEXT
)""")

# Crear la tabla snapshot_items
cur.execute("""CREATE TABLE IF NOT EXISTS snapshot_items (
    event_id INTEGER,
    channel TEXT,
    sku TEXT,
    qty INTEGER,
    derived_zero INTEGER,
    PRIMARY KEY (event_id, sku)
)""")

# Confirmar los cambios
con.commit()
con.close()

print("Tablas creadas o ya existen.")
