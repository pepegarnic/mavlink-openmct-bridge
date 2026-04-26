import asyncio
import websockets
import json
import sqlite3
import os
import struct
import time
import yaml
from aiohttp import web
from pymavlink import mavutil
from datetime import datetime, timedelta

# --- configuration ---
MAVLINK_PORT = "14551"
WEBSOCKET_PORT = 8081
HTTP_PORT = 5000


# 1. Cache for live-data
CACHE_DIR = ".cache"
DB_PATH = os.path.join(CACHE_DIR, "telemetry_history.db")
HOURS_TO_KEEP = 24

# 2. Folder for Mission Planner RAW-Logs (.tlog)
LOG_DIR = "logs"
TLOG_FILENAME = f"flight_log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.tlog"
TLOG_PATH = os.path.join(LOG_DIR, TLOG_FILENAME)

for directory in [CACHE_DIR, LOG_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)

def load_scaling_rules(filepath="/app/scaling_rules.yaml"):
    try:
        with open(filepath, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[WARNING] Could not load {filepath}: {e}. Using no scaling.")
        return {}


MAV_SCALING_TABLE = load_scaling_rules()  # Scale Values
print(f"GELADENE REGELN: {MAV_SCALING_TABLE}")

def apply_gcs_scaling(msg_type, data):
    if msg_type in MAV_SCALING_TABLE:
        rules = MAV_SCALING_TABLE[msg_type]
        for field, factor in rules.items():
            if field in data:
                val = data[field]
                if isinstance(val, list):
                    data[field] = [v / factor for v in val if v < 65535]
                elif isinstance(val, (int, float)):
                    data[field] = val / factor
    return data

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history 
                 (timestamp INTEGER, msg_type TEXT, data TEXT)''')
    # Index for DISTINCT request of categories
    c.execute('CREATE INDEX IF NOT EXISTS idx_msg_type ON history(msg_type)')
    conn.commit()
    conn.close()

def cleanup_old_data():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        cutoff_date = datetime.now() - timedelta(hours=HOURS_TO_KEEP)
        cutoff_timestamp_ms = int(cutoff_date.timestamp() * 1000)
        c.execute("DELETE FROM history WHERE timestamp < ?", (cutoff_timestamp_ms,))
        if c.rowcount > 0:
            conn.execute("VACUUM")
        conn.close()
    except Exception as e:
        print(f"[ERROR] Cache cleaning not succesful: {e}")

init_db()
cleanup_old_data()

connected_clients = set()

class MAVLinkEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (bytearray, bytes)):
            try:
                return obj.decode('utf-8', errors='replace').rstrip('\x00')
            except Exception:
                return list(obj)
        return super().default(obj)

async def handle_types(request):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # fetches all categories for openmct object tree
        c.execute("SELECT DISTINCT msg_type FROM history")
        rows = c.fetchall()
        conn.close()
        
        types = [row[0] for row in rows if row[0] is not None]
        return web.json_response(types, headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500, headers={"Access-Control-Allow-Origin": "*"})

async def handle_history(request):
    try:
        msg_type_full = request.match_info.get('key', "")
        msg_type = msg_type_full.split('.')[0]
        field = msg_type_full.split('.')[1] if '.' in msg_type_full else None
        
        start = int(float(request.query.get('start', 0)))
        end = int(float(request.query.get('end', 0)))

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        query = "SELECT timestamp, data FROM history WHERE msg_type = ? AND timestamp >= ? AND timestamp <= ?"
        c.execute(query, (msg_type, start, end))
        rows = c.fetchall()
        conn.close()

        history_data = []
        for row in rows:
            ts, raw_json = row
            data = json.loads(raw_json)
            if field and field in data:
                history_data.append({"utc": ts, "value": data[field], "id": msg_type_full})

        return web.json_response(history_data, headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500, headers={"Access-Control-Allow-Origin": "*"})

async def bridge_logic():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Connecting with MAVLink at port {MAVLINK_PORT} (0.0.0.0)...")
    master = mavutil.mavlink_connection(f'udpin:0.0.0.0:{MAVLINK_PORT}')
    
    # 1. waiting for first heartbeat
    master.wait_heartbeat()
    print(f"Receive Heartbeat from system {master.target_system} component {master.target_component}")

    # 2. request all data streams
    master.mav.request_data_stream_send(
        master.target_system, 
        master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, 
        10, # 10 Hz
        1   # Start
    )

    msg_counter = 0

    async def ws_handler(websocket):
        connected_clients.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            connected_clients.remove(websocket)

    ws_server = await websockets.serve(ws_handler, "0.0.0.0", WEBSOCKET_PORT, process_request=None)
    
    app = web.Application()
    app.router.add_get('/types', handle_types)
    app.router.add_get('/history/{key}', handle_history)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
    await site.start()

    print(f"WebSocket at {WEBSOCKET_PORT}, HTTP Server (History & Types) at {HTTP_PORT}")
    print(f"Writing missing Mission Planner Raw-Log: {TLOG_PATH}")

    db_conn = sqlite3.connect(DB_PATH)
    db_cursor = db_conn.cursor()
    
    # open tlog-File to add in binary mode
    tlog_file = open(TLOG_PATH, "ab")

    while True:
        msg = master.recv_match(blocking=False)
        if msg:
            # 1. Save RAW TLOG for Mission Planner
            raw_bytes = msg.get_msgbuf()
            if raw_bytes:
                timestamp_us = int(time.time() * 1e6)
                tlog_file.write(struct.pack('>Q', timestamp_us) + raw_bytes)
                if msg_counter % 50 == 0:
                    tlog_file.flush()

            # 2. JSON processing for OpenMCT
            msg_type = msg.get_type()
            msg_data = msg.to_dict()
            msg_data = apply_gcs_scaling(msg_type, msg_data) #apply scaling
            msg_data["mavtype"] = msg_type

            timestamp = int(datetime.now().timestamp() * 1000)
            payload = json.dumps(msg_data, cls=MAVLinkEncoder)

            db_cursor.execute("INSERT INTO history VALUES (?, ?, ?)", (timestamp, msg_type, payload))
            msg_counter += 1
            if msg_counter % 20 == 0:
                db_conn.commit()

            if connected_clients:
                msg_data["utc"] = timestamp
                broadcast_payload = json.dumps(msg_data, cls=MAVLinkEncoder)
                await asyncio.gather(*(client.send(broadcast_payload) for client in connected_clients), return_exceptions=True)

        await asyncio.sleep(0.0001)

if __name__ == "__main__":
    try:
        asyncio.run(bridge_logic())
    except KeyboardInterrupt:
        print("\nFinished.")