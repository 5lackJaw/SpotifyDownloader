@echo off
setlocal

if "%~1"=="" (
  echo Usage: dl ^<spotify_playlist_url_or_id^> [additional options]
  exit /b 1
)

set "PLAYLIST=%~1"
shift

set "SCRIPT_PATH=%CD%\downloader.py"

python "%SCRIPT_PATH%" "%PLAYLIST%" --skip-existing %*
exit /b %ERRORLEVEL%
