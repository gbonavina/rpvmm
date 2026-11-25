@echo off
chcp 65001 >nul
title PPG-PO CAPES - Coletor Bibliometrico e Otimizador
cd /d "%~dp0"

echo ==============================================================
echo   Iniciando Aplicativo Desktop PPG-PO CAPES (UNIFESP / ITA)
echo ==============================================================

where python3.11 >nul 2>&1
if %errorlevel%==0 (
    set "PY_CMD=python3.11"
) else (
    where python >nul 2>&1
    if %errorlevel%==0 (
        set "PY_CMD=python"
    ) else (
        where py >nul 2>&1
        if %errorlevel%==0 (
            set "PY_CMD=py -3"
        ) else (
            echo [ERRO] Python nao encontrado no sistema! Instale o Python 3.10+ em python.org.
            pause
            exit /b 1
        )
    )
)

%PY_CMD% -c "import customtkinter, pandas, openpyxl, requests, bs4, pulp, selenium" >nul 2>&1
if not %errorlevel%==0 (
    echo Instalando dependencias necessarias pela primeira vez...
    %PY_CMD% -m pip install -r requirements_app.txt
)

echo Abrindo interface grafica...
%PY_CMD% aplicativo_capes.py
if not %errorlevel%==0 (
    echo.
    echo [ERRO] O aplicativo encerrou com erro. Verifique as mensagens acima.
    pause
)
