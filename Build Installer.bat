@echo off
title Build Shelfrip installer
cd /d "%~dp0"
python build_installer.py
pause
