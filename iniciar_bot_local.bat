@echo off
setlocal
title G3NESYS Bot Local

cd /d "%~dp0"

echo.
echo ==========================================
echo   BOT ECONOMIA G3NESYS - MODO LOCAL
echo ==========================================
echo.

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo Se creo el archivo .env desde .env.example.
        echo.
        echo Abre el archivo .env y coloca tu DISCORD_TOKEN.
        echo Luego vuelve a ejecutar este archivo.
        echo.
        pause
        exit /b 1
    ) else (
        echo No encontre .env ni .env.example.
        echo Crea un archivo .env con DISCORD_TOKEN=tu_token.
        echo.
        pause
        exit /b 1
    )
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set "PYTHON_CMD=python"
    ) else (
        echo No encontre Python instalado.
        echo Instala Python 3.11 o superior y vuelve a intentar.
        echo.
        pause
        exit /b 1
    )
)

echo Instalando o revisando dependencias...
%PYTHON_CMD% -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo No pude instalar dependencias.
    echo Revisa que Python y pip esten instalados correctamente.
    echo.
    pause
    exit /b 1
)

echo.
echo Iniciando bot...
echo Para detenerlo, cierra esta ventana o presiona Ctrl+C.
echo.

%PYTHON_CMD% main.py

echo.
echo El bot se detuvo.
pause
