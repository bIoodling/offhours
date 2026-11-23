@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Edit .env locally and enter your token, server ID and owner ID. Never upload .env.
  pause
  exit /b 1
)
py -3 -m pip install -r requirements.txt
py -3 bot.py
pause
