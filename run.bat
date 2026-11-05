@echo off
chcp 65001 >nul
cd /d "%~dp0"
where uv >nul 2>&1 || (echo [error] uv가 필요합니다: https://docs.astral.sh/uv/ & pause & exit /b 1)
start "" /b uv run --no-dev pythonw -m pwa_overlay %*
