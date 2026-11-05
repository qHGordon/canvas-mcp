@echo off
REM Wrapper for the scheduled CanvasDailyReport task. Task Scheduler's PATH/env is
REM often incomplete, so required variables are set explicitly here.
REM
REM Put your machine-specific values in scripts\local_env.cmd (git-ignored; copy
REM scripts\local_env.cmd.example). Already-set environment variables also work.

set "REPO_DIR=%~dp0.."
if exist "%~dp0local_env.cmd" call "%~dp0local_env.cmd"

if "%CANVAS_BASE_URL%"=="" (echo CANVAS_BASE_URL is not set - see scripts\local_env.cmd.example 1>&2 & exit /b 2)
if "%CANVAS_TOKEN_FILE%%CANVAS_TOKEN%"=="" (echo CANVAS_TOKEN_FILE / CANVAS_TOKEN is not set - see scripts\local_env.cmd.example 1>&2 & exit /b 2)

REM UV_EXE is normally passed in by install_schedule.ps1 (absolute path detected
REM at install time). Fall back to PATH lookup, then the default uv install dir.
if "%UV_EXE%"=="" for /f "delims=" %%i in ('where uv 2^>nul') do set "UV_EXE=%%i"
if "%UV_EXE%"=="" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"

"%UV_EXE%" run --directory "%REPO_DIR%" python scripts/daily_report.py %*
exit /b %ERRORLEVEL%
