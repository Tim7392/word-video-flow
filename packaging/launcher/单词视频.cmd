@echo off
rem Member launcher for the packaged word-video engine.
rem ASCII only on purpose: cmd.exe reads a .cmd file with the OEM code page.
rem Everything is resolved from this file's own folder (%~dp0), so the package
rem works from any drive and any working directory.
setlocal enableextensions
set "PKG=%~dp0"
if "%PKG:~-1%"=="\" set "PKG=%PKG:~0,-1%"

if not exist "%PKG%\WordVideo\WordVideo.exe" (
  echo [WordVideo] incomplete package: "%PKG%\WordVideo\WordVideo.exe" is missing.
  echo [WordVideo] copy the whole folder again; do not move single files out of it.
  exit /b 3
)

rem Temp files stay inside the package unless the member points WORD_VIDEO_TEMP
rem at another writable folder.  Nothing is written to C: by default.
if defined WORD_VIDEO_TEMP (set "TEMP=%WORD_VIDEO_TEMP%") else (set "TEMP=%PKG%\temp")
set "TMP=%TEMP%"
if not exist "%TEMP%\" mkdir "%TEMP%" 2>nul
if not exist "%TEMP%\" (
  echo [WordVideo] cannot create "%TEMP%".
  echo [WordVideo] set WORD_VIDEO_TEMP to a writable folder and run this file again.
  exit /b 3
)

rem A packaged app must not inherit a developer Python environment.
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONSTARTUP="

rem PATH gains exactly one entry: the ffmpeg shipped inside this package.
set "PATH=%PKG%\ffmpeg;%PATH%"

cd /d "%PKG%"
"%PKG%\WordVideo\WordVideo.exe" %*
exit /b %ERRORLEVEL%
