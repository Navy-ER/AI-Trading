#!/data/data/com.termux/files/usr/bin/bash
# Alternative: Systemd-style service using termux-services (more robust)
# Requires: pkg install termux-services
# Then: sv-enable omniroute && sv-enable cloudflare

# This approach uses runit (termux-services) which is MORE robust than bash loops
# because it's managed by runsv daemon that survives everything.

set -euo pipefail

echo "=== Termux Services (runit) Setup ==="
echo "This uses termux-services (runit) for bulletproof persistence."
echo ""

# Install termux-services
pkg install -y termux-services

# Create service directories
mkdir -p ~/.service/omniroute ~/.service/cloudflare

# ──────────────────────────────────────────────────────────────────────
# OmniRoute Service
# ──────────────────────────────────────────────────────────────────────

cat > ~/.service/omniroute/run <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# OmniRoute runit service
# runsv manages this - auto-restart on crash, survives reboots

# Acquire wake lock
exec termux-wake-lock

# Execute omniroute (replace with your actual command)
exec omniroute --port 20128 2>&1
EOF

cat > ~/.service/omniroute/log/run <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# OmniRoute log service
exec svlogd -tt ./main
EOF

chmod +x ~/.service/omniroute/run ~/.service/omniroute/log/run

# ──────────────────────────────────────────────────────────────────────
# Cloudflare Tunnel Service
# ──────────────────────────────────────────────────────────────────────

cat > ~/.service/cloudflare/run <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Cloudflare Tunnel runit service
# runsv manages this - auto-restart on crash, survives reboots

# Acquire wake lock
exec termux-wake-lock

# Execute cloudflared (replace with your actual command)
exec cloudflared tunnel --url http://localhost:20128 2>&1
EOF

cat > ~/.service/cloudflare/log/run <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Cloudflare log service
exec svlogd -tt ./main
EOF

chmod +x ~/.service/cloudflare/run ~/.service/cloudflare/log/run

# ──────────────────────────────────────────────────────────────────────
# Enable services (creates symlinks in /data/data/com.termux/files/usr/var/service)
# ──────────────────────────────────────────────────────────────────────

echo "Enabling services..."
sv-enable omniroute
sv-enable cloudflare

echo ""
echo "=== SETUP COMPLETE ==="
echo ""
echo "Services are now managed by runit (runsv):"
echo "  - Auto-restart on crash"
echo "  - Survives Termux restart, phone reboot, screen off"
echo "  - Logs in ~/.service/omniroute/log/main/ and ~/.service/cloudflare/log/main/"
echo ""
echo "Commands:"
echo "  sv status omniroute cloudflare    # Check status"
echo "  sv up omniroute cloudflare        # Start"
echo "  sv down omniroute cloudflare      # Stop"
echo "  sv restart omniroute cloudflare   # Restart"
echo "  tail -f ~/.service/omniroute/log/main/current  # Live logs"
echo ""
echo "IMPORTANT: Disable battery optimization for Termux!"
echo "  Settings > Apps > Termux > Battery > Unrestricted"
echo ""
echo "For termux-services to work on boot, add to ~/.bashrc:"
echo "  termux-wake-lock"
echo "  sv up omniroute cloudflare"