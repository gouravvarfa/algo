@echo off
cd /d %~dp0
start "" http://127.0.0.1:8020
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8020
