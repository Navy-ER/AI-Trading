#!/bin/bash
# Cron wrapper: run monitor.py with the pythoncore-3.14 interpreter (has requests).
# Hermes cron runs .sh via bash; direct script= would use wrong python.
exec "C:/Users/reyvan/AppData/Local/Python/pythoncore-3.14-64/python.exe" "C:/Users/reyvan/pixel-agents/monitor.py"
