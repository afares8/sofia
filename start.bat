@echo off
echo Iniciando Sofia Monitor...
cd /d %~dp0\backend
start "Sofia Backend" python run.py
echo Backend iniciado en http://localhost:5180
echo Abre http://localhost:5180 en tu navegador
pause
