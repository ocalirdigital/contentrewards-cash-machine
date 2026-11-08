@echo off
chcp 65001 >nul
title Whop & AI Kesit Otomasyon Paneli
cd /d "%~dp0"
echo Whop AI Otomasyon Paneli baslatiliyor...
start http://127.0.0.1:8765
python app.py
pause
