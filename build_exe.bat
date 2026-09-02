@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  DLSS 5 Video Converter - PyInstaller build script
rem  Builds app.py into a single exe and assembles the release
rem  folder dist\DLSS5VideoConverter\ with all external assets.
rem ============================================================

echo [1/4] Checking PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller. Aborting.
        exit /b 1
    )
)

echo [2/4] Building exe with PyInstaller...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name DLSS5VideoConverter ^
    --add-data "dlss5_converter;dlss5_converter" ^
    --hidden-import dlss5_converter.core ^
    --hidden-import dlss5_converter.guides ^
    app.py
if errorlevel 1 (
    echo Build failed. Aborting.
    exit /b 1
)

echo [3/4] Assembling dist\DLSS5VideoConverter\ ...
set "DIST=dist\DLSS5VideoConverter"
if exist "%DIST%" rmdir /s /q "%DIST%"
mkdir "%DIST%"
move /y "dist\DLSS5VideoConverter.exe" "%DIST%\DLSS5VideoConverter.exe" >nul
if errorlevel 1 (
    echo Failed to move exe into %DIST%. Aborting.
    exit /b 1
)

rem Empty runtime folders (created on demand by the app, but the
rem release zip should already contain them)
mkdir "%DIST%\outputs" 2>nul
mkdir "%DIST%\jobs" 2>nul
mkdir "%DIST%\originals" 2>nul

echo Copying bin\runtime (nvngx.dll, nvngx_dlssnr.dll)...
robocopy "bin\runtime" "%DIST%\bin\runtime" /E /NFL /NDL /NJH /NJS /NP >nul
if %errorlevel% GEQ 8 (
    echo robocopy failed for bin\runtime. Aborting.
    exit /b 1
)

echo Copying bin\ffmpeg (ffmpeg.exe, ffprobe.exe)...
robocopy "bin\ffmpeg" "%DIST%\bin\ffmpeg" /E /NFL /NDL /NJH /NJS /NP >nul
if %errorlevel% GEQ 8 (
    echo robocopy failed for bin\ffmpeg. Aborting.
    exit /b 1
)

echo Copying preview (dlssnr-image.exe, caller\nvngx.dll)...
if exist "preview" (
    robocopy "preview" "%DIST%\preview" /E /NFL /NDL /NJH /NJS /NP >nul
    if %errorlevel% GEQ 8 (
        echo robocopy failed for preview. Aborting.
        exit /b 1
    )
    rem nvngx_dlssnr.dll копируется при старте из bin\runtime — не дублируем
    if exist "%DIST%\preview\nvngx_dlssnr.dll" del /q "%DIST%\preview\nvngx_dlssnr.dll"
) else (
    echo WARNING: preview\ not found - frame preview will be unavailable.
)

echo Copying README files...
copy /y "README.md" "%DIST%\" >nul
copy /y "README.ru.md" "%DIST%\" >nul

echo [4/4] Done.
echo.
echo Release folder ready: %DIST%
echo   - DLSS5VideoConverter.exe
echo   - bin\runtime\ (nvngx.dll, nvngx_dlssnr.dll)
echo   - bin\ffmpeg\ (ffmpeg.exe, ffprobe.exe)
echo   - outputs\ jobs\ originals\
echo   - README.md, README.ru.md
echo.
echo Zip this folder and upload it to the GitHub release.
pause
