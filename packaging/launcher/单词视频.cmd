@echo off
rem Member launcher for the packaged word-video engine.
rem ASCII only on purpose: cmd.exe reads a .cmd file with the OEM code page.
rem Everything is resolved from this file's own folder (%~dp0), so the package
rem works from any drive and any working directory.
rem
rem   no arguments     -> the editor window (what a member double-clicks)
rem   --editor [more]  -> the editor window, driven by script arguments
rem                       (--import/--into/--export/--report); with more than
rem                       the switch it runs in the foreground so the caller
rem                       gets the exit code and the JSON report
rem   anything else    -> the unchanged headless JSON CLI (doctor/submit/...)
setlocal enableextensions
set "PKG=%~dp0"
if "%PKG:~-1%"=="\" set "PKG=%PKG:~0,-1%"

if not exist "%PKG%\WordVideo\WordVideo.exe" (
  echo [WordVideo] incomplete package: "%PKG%\WordVideo\WordVideo.exe" is missing.
  echo [WordVideo] copy the whole folder again; do not move single files out of it.
  exit /b 3
)
if not exist "%PKG%\WordVideoEditor\WordVideoEditor.exe" (
  echo [WordVideo] incomplete package: "%PKG%\WordVideoEditor\WordVideoEditor.exe" is missing.
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

if "%~1"=="" goto editor_window
if /i "%~1"=="--editor" goto editor
"%PKG%\WordVideo\WordVideo.exe" %*
exit /b %ERRORLEVEL%

:editor
rem The switch alone opens a window: `start` lets the console this .cmd brought
rem with it close, instead of sitting behind the editor for the whole session.
rem More arguments mean the editor is being driven, and a caller that drives it
rem wants its exit code - so that case stays in the foreground.
if "%~2"=="" goto editor_window
"%PKG%\WordVideoEditor\WordVideoEditor.exe" %*
exit /b %ERRORLEVEL%

:editor_window
start "" "%PKG%\WordVideoEditor\WordVideoEditor.exe" --editor
exit /b 0
