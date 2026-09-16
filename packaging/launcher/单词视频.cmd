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
rem
rem No GOTO labels and no bracketed blocks, deliberately.  Measured: this file is
rem stored with LF line endings, and cmd.exe finds a GOTO label by scanning for a
rem CR-terminated line, so every label was reported as missing ("cannot find the
rem batch label specified") and the double-click did nothing.  A mode variable and
rem single-line IFs do the same job with neither that trap nor the delayed
rem expansion one (an %ERRORLEVEL% inside brackets is read before the line runs,
rem which is how a launcher reports the wrong exit code).
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

rem Which door: the window, the window driven by arguments, or the CLI.
set "WV_MODE=cli"
if "%~1"=="" set "WV_MODE=window"
if /i "%~1"=="--editor" set "WV_MODE=driven"
if /i "%~1"=="--editor" if "%~2"=="" set "WV_MODE=window"

rem The window alone: `start` lets the console this .cmd brought with it close,
rem instead of sitting behind the editor for the whole session.  The redirection
rem is not decoration: the started process inherits this command's handles, and a
rem caller that captured the launcher's output would otherwise hold its pipe open
rem until the member closed the editor.
if "%WV_MODE%"=="window" start "" "%PKG%\WordVideoEditor\WordVideoEditor.exe" --editor >nul 2>nul
if "%WV_MODE%"=="window" exit /b 0

rem Driven: in the foreground, because a caller that drives the editor wants its
rem exit code and the JSON report it writes.
if "%WV_MODE%"=="driven" "%PKG%\WordVideoEditor\WordVideoEditor.exe" %*
if "%WV_MODE%"=="driven" exit /b %ERRORLEVEL%

"%PKG%\WordVideo\WordVideo.exe" %*
exit /b %ERRORLEVEL%
