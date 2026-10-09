@echo off
setlocal
cd /d "%~dp0.."
where uv >nul 2>nul
if errorlevel 1 (
    python scripts/video_notes.py serve --open %*
) else (
    uv run --python 3.12 --with pillow python scripts/video_notes.py serve --open %*
)
if errorlevel 1 pause
