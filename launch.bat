@echo off
setlocal
cd /d "%~dp0"
python "%~dp0archive_collector.py" --gui
