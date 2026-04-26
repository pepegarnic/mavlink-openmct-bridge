#!/bin/bash


sleep 2

# Create Folder if it doesnt exist
mkdir -p /app/openmct-core/dist

# 3. copy index.html into openmpc built dist-Folder
if [ -f "/app/openmct/index.html" ]; then
    cp /app/openmct/index.html /app/openmct-core/dist/index.html
    echo "[*] index.html erfolgreich kopiert."
fi

CouchDB Auto-Setup
echo "[*] Initialisiere CouchDB Datenbanken..."

# Try to reach CouchDB 10 times
for i in {1..10}; do
    # check CouchDB answers
    if curl -s -f http://127.0.0.1:5984/ > /dev/null; then
        echo "[*] CouchDB ist online. Lege Datenbanken an..."
        
        # Create System-Database
        curl -s -X PUT http://admin:openmct123@127.0.0.1:5984/_users > /dev/null
        curl -s -X PUT http://admin:openmct123@127.0.0.1:5984/_replicator > /dev/null
        curl -s -X PUT http://admin:openmct123@127.0.0.1:5984/_global_changes > /dev/null
        
        # OpenMCT Datenbank anlegen
        curl -s -X PUT http://admin:openmct123@127.0.0.1:5984/openmct_db > /dev/null
        
        # Database to pubilc
        curl -s -X PUT http://admin:openmct123@127.0.0.1:5984/openmct_db/_security \
             -H "Content-Type: application/json" \
             -d '{"admins":{"names":[],"roles":[]},"members":{"names":[],"roles":[]}}' > /dev/null
             
        echo "[OK] CouchDB Setup abgeschlossen."
        break
    fi
    echo "Warte auf CouchDB ($i/10)..."
    sleep 2
done
# -------------------------------

echo "[*] Starte Master Launcher..."
python3 -u /app/launch_all.py