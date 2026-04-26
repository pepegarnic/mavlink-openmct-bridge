import asyncio
import websockets
import json
import sqlite3
import os
import struct
import time
import yaml
import xml.etree.ElementTree as ET
from aiohttp import web
from pymavlink import mavutil
from datetime import datetime, timedelta

# --- Konfiguration ---
MAVLINK_PORT = "14551"
WEBSOCKET_PORT = 8081
HTTP_PORT = 5000

CACHE_DIR = ".cache"
DB_PATH = os.path.join(CACHE_DIR, "telemetry_history.db")
HOURS_TO_KEEP = 24

LOG_DIR = "logs"
TLOG_FILENAME = f"flight_log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.tlog"
TLOG_PATH = os.path.join(LOG_DIR, TLOG_FILENAME)

for directory in [CACHE_DIR, LOG_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)

# --- 1. Metadaten & Skalierung laden ---
def load_mavlink_xml_units(filepath="/app/common.xml"):
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        xml_units = {}
        for msg in root.findall('.//message'):
            msg_name = msg.get('name')
            xml_units[msg_name] = {}
            for field in msg.findall('field'):
                f_name = field.get('name')
                units = field.get('units')
                if units:
                    xml_units[msg_name][f_name] = units
        print(f"[*] XML-Einheiten geladen für {len(xml_units)} Nachrichtentypen.")
        return xml_units
    except Exception as e:
        print(f"[WARNING] XML Units nicht geladen (Fehlt die Datei?): {e}")
        return {}

def load_scaling_rules(filepath="/app/scaling_rules.yaml"):
    try:
        with open(filepath, 'r') as f:
            print(f"[*] YAML-Skalierungsregeln erfolgreich geladen.")
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[WARNING] Konnte YAML nicht laden: {e}")
        return {}

MAV_XML_UNITS = load_mavlink_xml_units()
MAV_SCALING_TABLE = load_scaling_rules()

def get_metadata(msg_type, field_name):
    # 1. Check YAML (Deine Regeln)
    if msg_type in MAV_SCALING_TABLE and field_name in MAV_SCALING_TABLE[msg_type]:
        rule = MAV_SCALING_TABLE[msg_type][field_name]
        return rule.get('factor', 1.0), rule.get('unit', '')

    # 2. Check XML (MAVLink Standard)
    if msg_type in MAV_XML_UNITS and field_name in MAV_XML_UNITS[msg_type]:
        return 1.0, MAV_XML_UNITS[msg_type][field_name]

    return 1.0, ""

def apply_gcs_scaling(msg_type, data):
    for field in list(data.keys()):
        factor, unit = get_metadata(msg_type, field)
        if factor != 1.0:
            val = data[field]
            try:
                if isinstance(val, (int, float)):
                    data[field] = val / factor
                elif isinstance(val, list):
                    data[field] = [v / factor for v in val if v < 65535]
            except Exception:
                pass
    return data

# --- 2. Datenbank ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history 
                 (timestamp INTEGER, msg_type TEXT, data TEXT)''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_msg_type ON history(msg_type)')
    conn.commit()
    conn.close()

def cleanup_old_data():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        cutoff_timestamp_ms = int((datetime.now() - timedelta(hours=HOURS_TO_KEEP)).timestamp() * 1000)
        c.execute("DELETE FROM history WHERE timestamp < ?", (cutoff_timestamp_ms,))
        if c.rowcount > 0:
            conn.execute("VACUUM")
        conn.close()
    except Exception as e:
        print(f"[ERROR] Cache cleaning not succesful: {e}")

init_db()
cleanup_old_data()

connected_clients = {}

class MAVLinkEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (bytearray, bytes)):
            try:
                return obj.decode('utf-8', errors='replace').rstrip('\x00')
            except Exception:
                return list(obj)
        return super().default(obj)

# --- 3. HTTP Server Endpunkte ---
async def handle_types(request):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT DISTINCT msg_type FROM history")
        types = [row[0] for row in c.fetchall() if row[0] is not None]
        conn.close()
        return web.json_response(types, headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500, headers={"Access-Control-Allow-Origin": "*"})

async def handle_history(request):
    try:
        msg_type_full = request.match_info.get('key', "")
        msg_type, field = (msg_type_full.split('.') + [None])[:2]
        start = int(float(request.query.get('start', 0)))
        end = int(float(request.query.get('end', 0)))

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT timestamp, data FROM history WHERE msg_type = ? AND timestamp >= ? AND timestamp <= ?", (msg_type, start, end))
        rows = c.fetchall()
        conn.close()

        history_data = [{"utc": ts, "value": json.loads(raw)[field], "id": msg_type_full} 
                        for ts, raw in rows if field and field in json.loads(raw)]
        return web.json_response(history_data, headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500, headers={"Access-Control-Allow-Origin": "*"})

async def handle_metadata(request):
    combined = {}
    all_msgs = set(list(MAV_XML_UNITS.keys()) + list(MAV_SCALING_TABLE.keys()))
    
    for m in all_msgs:
        combined[m] = {}
        if m in MAV_XML_UNITS:
            for f, u in MAV_XML_UNITS[m].items():
                combined[m][f] = {"unit": u}
        if m in MAV_SCALING_TABLE:
            for f, cfg in MAV_SCALING_TABLE[m].items():
                if isinstance(cfg, dict):
                    combined[m][f] = {"unit": cfg.get('unit', '')}
    return web.json_response(combined, headers={"Access-Control-Allow-Origin": "*"})

# --- 4. Hauptlogik (Brücke) ---
async def bridge_logic():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Connecting with MAVLink at port {MAVLINK_PORT} (0.0.0.0)...")
    master = mavutil.mavlink_connection(f'udpin:0.0.0.0:{MAVLINK_PORT}')
    master.wait_heartbeat()
    print(f"Receive Heartbeat from system {master.target_system} component {master.target_component}")

    master.mav.request_data_stream_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, 10, 1
    )

    # WebSocket Pub/Sub Handler
    async def ws_handler(websocket):
        connected_clients[websocket] = set()
        try:
            async for message in websocket:
                try:
                    ws_json = json.loads(message)
                    action, msg_name = ws_json.get("action"), ws_json.get("message")
                    
                    if action == "subscribe" and msg_name:
                        connected_clients[websocket].add(msg_name)
                    elif action == "unsubscribe" and msg_name:
                        connected_clients[websocket].discard(msg_name)
                    elif action == "subscribeAll":
                        connected_clients[websocket].add("subscribeAll")
                except Exception:
                    pass
        finally:
            del connected_clients[websocket]

    ws_server = await websockets.serve(ws_handler, "0.0.0.0", WEBSOCKET_PORT, process_request=None)
    
    app = web.Application()
    app.router.add_get('/types', handle_types)
    app.router.add_get('/history/{key}', handle_history)
    app.router.add_get('/metadata', handle_metadata) # NEUER ENDPUNKT
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', HTTP_PORT).start()

    print(f"WebSocket at {WEBSOCKET_PORT}, HTTP Server (History/Types/Metadata) at {HTTP_PORT}")
    
    db_conn = sqlite3.connect(DB_PATH)
    db_cursor = db_conn.cursor()
    tlog_file = open(TLOG_PATH, "ab")
    msg_counter = 0

    while True:
        msg = master.recv_match(blocking=False)
        if msg:
            # 1. HINTERGRUND-SPEICHERUNG
            raw_bytes = msg.get_msgbuf()
            if raw_bytes:
                timestamp_us = int(time.time() * 1e6)
                tlog_file.write(struct.pack('>Q', timestamp_us) + raw_bytes)
                if msg_counter % 50 == 0:
                    tlog_file.flush()

            msg_type = msg.get_type()
            msg_data = apply_gcs_scaling(msg_type, msg.to_dict())
            msg_data["mavtype"] = msg_type

            timestamp = int(datetime.now().timestamp() * 1000)
            payload = json.dumps(msg_data, cls=MAVLinkEncoder)

            db_cursor.execute("INSERT INTO history VALUES (?, ?, ?)", (timestamp, msg_type, payload))
            msg_counter += 1
            if msg_counter % 20 == 0:
                db_conn.commit()

            # 2. PUB/SUB WEBSOCKET
            if connected_clients:
                for client, subscriptions in connected_clients.items():
                    if "subscribeAll" in subscriptions or not subscriptions:
                        msg_data["utc"] = timestamp
                        asyncio.create_task(client.send(json.dumps(msg_data, cls=MAVLinkEncoder)))
                    else:
                        for sub in subscriptions:
                            parts = sub.split('.')
                            if len(parts) == 2 and parts[0] == msg_type:
                                field = parts[1]
                                if field in msg_data:
                                    pub_data = {"timestamp": timestamp, "value": msg_data[field], "id": sub}
                                    asyncio.create_task(client.send(json.dumps(pub_data, cls=MAVLinkEncoder)))

        await asyncio.sleep(0.0001)

if __name__ == "__main__":
    try:
        asyncio.run(bridge_logic())
    except KeyboardInterrupt:
        print("\nFinished.")