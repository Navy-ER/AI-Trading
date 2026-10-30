#!/bin/bash
cd /home/server/trading-bot
pkill -f 'python3 dashboard_server.py' 2>/dev/null
nohup python3 dashboard_server.py > /dev/null 2>&1 &
echo 'Dashboard running on port 5000'
