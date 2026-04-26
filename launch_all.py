import subprocess
import os
import time
import sys
import signal
import glob


#output port Configuration
MAVLINK_INPUT_IP = "0.0.0.0" #0.0.0.0 listens to all incoming connections
MAVLINK_INPUT_PORT = "14550"
MAVLINK_OUT_IP = "255.255.255.255" #255.255.255.255 outputs data to the whole network
MAVLINK_OUT_PORT = "14552"


#path configuration
USER_HOME = "/app"
OPENMCT_DIR = "/app/openmct-core"
DIST_DIR = os.path.join(OPENMCT_DIR, "dist") 
BRIDGE_SCRIPT = "/app/launch_telemetry_bridge.py"
MAVPROXY_EXE = "/usr/local/bin/mavproxy.py"

processes = {}

def signal_handler(sig, frame):
    print("\n--- MAVLINK-OPENMCT-BRIDGE is closing... ---")
    for name, p in processes.items():
        print(f"Stop {name}...")
        p.terminate()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def start_process(cmd, cwd=None, name="", silent=False):
    print(f"[*] Starting {name}...")
    out_target = subprocess.DEVNULL if silent else None
    proc = subprocess.Popen(
        cmd, 
        cwd=cwd, 
        stdout=out_target, 
        stderr=subprocess.STDOUT,
        universal_newlines=True
    )
    processes[name] = proc
    return proc

def main():
    print("=== MAVLINK-OPENMCT-BRIDGE LAUNCHER ===\n")
    
    print("[1/4] Cleaning ports...")
    os.system("sudo fuser -k 8081/tcp 5000/tcp 8080/tcp 14551/udp 14550/udp 2>/dev/null")
    time.sleep(2)

    # --- AUTOMATIC PORT-DISCOVERY ---
    print("[2/4] Looking for Connections...")
    
    # Search fo all USB/ACM Devices
    usb_ports = glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyUSB*')
    
    mav_cmd = [sys.executable, MAVPROXY_EXE]

    # Adds every USB Device as Master
    for port in usb_ports:
        print(f"    -> USB-Gerät gefunden: {port}")
        mav_cmd.extend(["--master", port])

    # ADD IP-Master (UDP Listen)
    # 0.0.0.0:14550 is listening to all MAVLink-Packages via network
    print(f"    -> IP-Netzwerk-Listener aktiviert (UDP 14550)")
    mav_cmd.extend(["--master", f"udp:{MAVLINK_INPUT_IP}:{MAVLINK_INPUT_PORT}"])

    # output for bridge
    mav_cmd.extend([
        "--baudrate", "57600",
        "--out", "127.0.0.1:14551",
        "--out", f"udpbcast:{MAVLINK_OUT_IP}:{MAVLINK_OUT_PORT}", # sends out Mavlink to whole NETWORK
        "--daemon"
    ])

    if len(usb_ports) == 0:
        print("    [!] No USB-device discovered. Waiting for network data")

    start_process(mav_cmd, name="MAVProxy-Universal", silent=True)
    time.sleep(5)

    # 3. Telemetry Bridge
    start_process([sys.executable, "-u", BRIDGE_SCRIPT], name="Telemetry-Bridge")
    time.sleep(2)

    # 4. Open MCT Static Server (from dist-Folder)
    if os.path.exists(DIST_DIR):
        start_process(["http-server", ".", "-p", "8080", "-c-1", "--proxy", "http://admin:openmct123@127.0.0.1:5984"], cwd=DIST_DIR, name="Open-MCT-Static")
    else:
        print(f"[ERROR] dist-Ordner not found! Run 'npm run build' ")

    print("\n[OK] listening to all Channels!")
    print("[OK] System ready!")
    print("open http://192.168.8.3:8080 in browser.")

    while True:
        for name, p in processes.items():
            if p.poll() is not None:
                print(f"[ERROR] {name} is down! (Code: {p.poll()})")
        time.sleep(10)

if __name__ == "__main__":
    main()