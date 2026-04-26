Markdown
# 🛸 MAVLink-OpenMCT-Bridge

This project creates a seamless bridge between **MAVLink-compatible flight controllers** (ArduPilot/PX4) and **NASA’s OpenMCT** dashboard. It is designed to run entirely within a Docker container, handling everything from hardware detection to data archiving automatically.



##  Features

* **Auto-Connect:** Automatically detects and connects to any MAVLink-capable USB device and all UDP network connections at port 14550
* **NASA OpenMCT:** Telemetry visualization directly in your browser.
* **CouchDB:** Storing OpenMCT panels directly in the Container.
* **Persistent Storage:** Raw telemetry is automatically stored into a logfile(logs/) for post-flight analysis.
* **Dual-Stream:** Distributes data to OpenMCT while simultaneously allowing a connection to **MissionPlanner**.



##  Warnings

* **Single Source Only:** The system attempts to connect to **every** USB-serial device it finds. To avoid data corruption or connection failures, **do not connect multiple MAVLink sources** (e.g., one UDP and one USB) at the same time.
* **Port 14550 Usage:** Because the container uses `network_mode: host`, it listens on port **14550** directly on your machine. Ensure no other Ground Control Station or MAVProxy instance is running locally on that port before starting the container.



##  Installation

### Prerequisites
* **Docker**
* **Docker Compose**

### Quick Start
1. **Clone the repository:**
```bash
   git clone [https://github.com/pepegarnic/mavlink-openmct-bridge.git](https://github.com/pepegarnic/mavlink-openmct-bridge.git)
   cd mavlink-openmct-bridge
   docker compose up --build
   ```

##  Usage

Once the container is running, the dashboard is accessible via your web browser:

* **Dashboard UI:** `http://<YOUR_PI_IP>:8080`
* **MAVLINK Input:** Port `14550` (UDP)
* **MAVLINK Output:** Port `14552` (UDP Broadcast)

> **Note:** If MissionPlanner does not connect automatically, ensure your laptop is on the same subnet (e.g., `192.168.1.x`) and check your firewall settings for port `14552`.



## Architecture

The "All-in-One" container operates as a multi-service environment, managing three primary tasks simultaneously:

1. **MAVProxy:** Serves as the central communication hub. It reads raw data from the USB-serial port and routes it to multiple UDP outputs (internal bridge and external network).
2. **Python Bridge:** A custom backend that processes incoming MAVLink packets. It performs two main functions:
    * **Persistence:** Writes telemetry data to a SQLite database located at `/data/telemetry.db`.
    * **Streaming:** Pipes live data to the openmct frontend via WebSockets.
3. **Integrated Web Server:** A minimalist `aiohttp` server that delivers the OpenMCT static files and handles REST API requests for historical data (History Provider).