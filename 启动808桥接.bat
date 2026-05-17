@echo off
chcp 65001 >nul
title 808 Local Bridge Launcher

echo ==================================================
echo Checking environment and installing dependencies...
echo ==================================================
pip install bleak aiohttp -q

cls
python toy_bridge.py

echo.
echo ==================================================
echo Program exited. Press any key to close...
pause >nul
