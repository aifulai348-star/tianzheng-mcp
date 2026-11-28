@echo off
cd /d "%~dp0"
if not defined TZ_PYTHON set "TZ_PYTHON=python"
"%TZ_PYTHON%" -X utf8 "%~dp0bootstrap.py" %*
