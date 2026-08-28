#!/data/data/com.termux/files/usr/bin/bash
# Termux Keep-Alive Service for OmniRoute + Cloudflare Tunnel
# Run this ONCE in Termux to install persistent background services.
# Survives reboots, screen off, Doze mode.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# CONFIG - EDIT THESE TO MATCH YOUR SETUP
# ──────────────────────────────────────────────────────────────────────

OMNIROUTE_PORT=20128
OMNIROUTE_CMD="omniroute"  # or full path: /data/data/com.termux/files/usr/bin/omniroute
CLOUDFLARE_CMD="cloudflared"  # or full path
CLOUDFLARE_TUNNEL_URL="https://your-tunnel.trycloudflare.com"  # your tunnel URL
CLOUDFLARE_ARGS="tunnel --url http://localhost:${OMNIROUTE_PORT}"

# ──────────────────────────────────────────────────────────────────────
# INSTALLATION
# ──────────────────────────────────────────────────────────────────────

echo "=== Termux Keep-Alive Installer ==="
echo "This will set up persistent services for OmniRoute + Cloudflare Tunnel"
echo ""

# 1. Acquire wake lock (prevents CPU sleep)
echo "[1/5] Setting up termux-wake-lock..."
termux-wake-lock
# Make it persistent via boot service
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/wake-lock <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Acquire wake lock on boot
termux-wake-lock
EOF
chmod +x ~/.termux/boot/wake-lock

# 2. Disable battery optimization for Termux (critical!)
echo "[2/5] Checking battery optimization..."
echo "MANUAL STEP REQUIRED: Go to Android Settings > Apps > Termux > Battery > Unrestricted"
echo "Or run: termux-battery-optimization disable (requires Termux:API)"
echo ""

# 3. Create OmniRoute service script
echo "[3/5] Creating OmniRoute service..."
cat > ~/omniroute-service.sh <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# OmniRoute persistent service
# Restarts on crash, logs to ~/omniroute.log

LOG_FILE="\$HOME/omniroute.log"
PID_FILE="\$HOME/omniroute.pid"

# Ensure wake lock
termux-wake-lock

# Function to start OmniRoute
start_omniroute() {
    echo "[\$(date)] Starting OmniRoute on port ${OMNIROUTE_PORT}..." >> "\$LOG_FILE"
    nohup ${OMNIROUTE_CMD} --port ${OMNIROUTE_PORT} >> "\$LOG_FILE" 2>&1 &
    echo \$! > "\$PID_FILE"
    echo "[\$(date)] OmniRoute started with PID \$!" >> "\$LOG_FILE"
}

# Function to check if process is alive
is_alive() {
    if [ -f "\$PID_FILE" ]; then
        local pid=\$(cat "\$PID_FILE" 2>/dev/null)
        if [ -n "\$pid" ] && kill -0 "\$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# Main loop
echo "[\$(date)] OmniRoute service started" >> "\$LOG_FILE"
start_omniroute

while true; do
    sleep 30
    if ! is_alive; then
        echo "[\$(date)] OmniRoute died, restarting..." >> "\$LOG_FILE"
        start_omniroute
    fi
    # Health check: verify port responds
    if ! nc -z localhost ${OMNIROUTE_PORT} 2>/dev/null; then
        echo "[\$(date)] Port ${OMNIROUTE_PORT} not responding, restarting..." >> "\$LOG_FILE"
        start_omniroute
    fi
done
EOF
chmod +x ~/omniroute-service.sh

# 4. Create Cloudflare Tunnel service script
echo "[4/5] Creating Cloudflare Tunnel service..."
cat > ~/cloudflare-service.sh <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Cloudflare Tunnel persistent service
# Restarts on crash, logs to ~/cloudflare.log

LOG_FILE="\$HOME/cloudflare.log"
PID_FILE="\$HOME/cloudflare.pid"

# Ensure wake lock
termux-wake-lock

# Function to start Cloudflare Tunnel
start_tunnel() {
    echo "[\$(date)] Starting Cloudflare Tunnel..." >> "\$LOG_FILE"
    nohup ${CLOUDFLARE_CMD} ${CLOUDFLARE_ARGS} >> "\$LOG_FILE" 2>&1 &
    echo \$! > "\$PID_FILE"
    echo "[\$(date)] Cloudflare Tunnel started with PID \$!" >> "\$LOG_FILE"
}

# Function to check if process is alive
is_alive() {
    if [ -f "\$PID_FILE" ]; then
        local pid=\$(cat "\$PID_FILE" 2>/dev/null)
        if [ -n "\$pid" ] && kill -0 "\$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# Main loop
echo "[\$(date)] Cloudflare Tunnel service started" >> "\$LOG_FILE"
start_tunnel

while true; do
    sleep 30
    if ! is_alive; then
        echo "[\$(date)] Cloudflare Tunnel died, restarting..." >> "\$LOG_FILE"
        start_tunnel
    fi
done
EOF
chmod +x ~/cloudflare-service.sh

# 5. Create master launcher (runs both services)
echo "[5/5] Creating master launcher..."
cat > ~/start-services.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Master launcher - starts both services in background
# Run this once per Termux session, or add to ~/.bashrc for auto-start

echo "=== Starting Termux Trading Services ==="

# Acquire wake lock
termux-wake-lock
echo "Wake lock acquired"

# Start OmniRoute service
if [ ! -f ~/omniroute.pid ] || ! kill -0 \$(cat ~/omniroute.pid 2>/dev/null) 2>/dev/null; then
    nohup ~/omniroute-service.sh >> ~/omniroute-service.log 2>&1 &
    echo "OmniRoute service started (PID: \$!)"
else
    echo "OmniRoute service already running"
fi

# Start Cloudflare Tunnel service
if [ ! -f ~/cloudflare.pid ] || ! kill -0 \$(cat ~/cloudflare.pid 2>/dev/null) 2>/dev/null; then
    nohup ~/cloudflare-service.sh >> ~/cloudflare-service.log 2>&1 &
    echo "Cloudflare Tunnel service started (PID: \$!)"
else
    echo "Cloudflare Tunnel service already running"
fi

echo "=== All services running ==="
echo "Logs: ~/omniroute.log, ~/cloudflare.log"
echo "To stop: ~/stop-services.sh"
EOF
chmod +x ~/start-services.sh

# 6. Create stop script
cat > ~/stop-services.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
echo "=== Stopping Termux Trading Services ==="

for pid_file in ~/omniroute.pid ~/cloudflare.pid ~/omniroute-service.pid ~/cloudflare-service.pid; do
    if [ -f "\$pid_file" ]; then
        pid=\$(cat "\$pid_file" 2>/dev/null)
        if [ -n "\$pid" ] && kill -0 "\$pid" 2>/dev/null; then
            kill "\$pid" 2>/dev/null
            echo "Stopped PID \$pid"
        fi
        rm -f "\$pid_file"
    fi
done

# Also kill by port
pkill -f "omniroute.*--port ${OMNIROUTE_PORT}" 2>/dev/null
pkill -f "cloudflared.*tunnel.*${OMNIROUTE_PORT}" 2>/dev/null

# Release wake lock
termux-wake-unlock
echo "Wake lock released"
echo "=== All services stopped ==="
EOF
chmod +x ~/stop-services.sh

# 7. Add auto-start to .bashrc (optional but recommended)
echo ""
echo "=== Adding auto-start to ~/.bashrc ==="
if ! grep -q "start-services.sh" ~/.bashrc 2>/dev/null; then
    cat >> ~/.bashrc <<'EOF'

# Auto-start trading services on Termux launch
if [ -f ~/start-services.sh ]; then
    ~/start-services.sh
fi
EOF
    echo "Added to ~/.bashrc"
else
    echo "Already in ~/.bashrc"
fi

# 8. Install netcat for health checks (if missing)
echo ""
echo "=== Checking netcat (nc) for health checks ==="
if ! command -v nc &> /dev/null; then
    echo "Installing netcat-openbsd..."
    pkg install -y netcat-openbsd
else
    echo "netcat already installed"
fi

echo ""
echo "=== INSTALLATION COMPLETE ==="
echo ""
echo "NEXT STEPS:"
echo "1. EDIT ~/omniroute-service.sh and ~/cloudflare-service.sh"
echo "   - Set OMNIROUTE_CMD to your omniroute binary path"
echo "   - Set CLOUDFLARE_CMD to your cloudflared binary path"
echo "   - Set CLOUDFLARE_ARGS with your actual tunnel config"
echo ""
echo "2. DISABLE BATTERY OPTIMIZATION FOR TERMUX:"
echo "   Settings > Apps > Termux > Battery > Unrestricted"
echo "   (Or install Termux:API and run: termux-battery-optimization disable)"
echo ""
echo "3. TEST MANUALLY FIRST:"
echo "   ~/start-services.sh"
echo "   Check logs: tail -f ~/omniroute.log ~/cloudflare.log"
echo ""
echo "4. SERVICES WILL AUTO-START ON NEXT TERMUX LAUNCH"
echo "   (via ~/.bashrc)"
echo ""
echo "To stop: ~/stop-services.sh"
echo "To view logs: tail -f ~/omniroute.log ~/cloudflare.log"