@echo off
title OpenCode Session Viewer
cd /d "%~dp0"
python server.py --port 8765
pause
