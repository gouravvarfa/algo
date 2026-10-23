@echo off
cd /d %~dp0
rem the web app (React) is built once; rebuild after pulling changes:  cd frontend ^& npm install ^& npm run build
if not exist frontend\dist\index.html (
  pushd frontend
  call npm install
  call npm run build
  popd
)
start "" http://127.0.0.1:8020/app/
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8020
