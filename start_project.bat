@echo off
REM Pixel-Agents + Bridge auto-start (Startup folder, jalan saat login)
cd /d C:\Users\reyvan\Pixel-Agents

REM 1. Pixel-Agents di port 3000
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath 'node' -ArgumentList 'C:\Users\reyvan\AppData\Roaming\npm\node_modules\pixel-agents\dist\cli.js --port 3000'"

REM 2. Bridge (delay via ping - timeout butuh console input)
ping -n 9 127.0.0.1 >nul
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath 'C:\Users\reyvan\AppData\Local\Python\pythoncore-3.14-64\python.exe' -ArgumentList 'C:\Users\reyvan\Pixel-Agents\bridge.py' -WorkingDirectory 'C:\Users\reyvan\Pixel-Agents'"
