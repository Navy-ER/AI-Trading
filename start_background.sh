#!/bin/bash
cd /home/server/trading-bot
pkill -f 'python3 bridge.py' 2>/dev/null
nohup python3 bridge.py >> bridge.log 2>&1 &
echo 'Bot started in background!'
