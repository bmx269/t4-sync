@echo off
rem Double-click this on Windows to open the t4-sync control panel.
rem
rem Copy it into your project directory -- the one holding .t4\ -- so that the
rem panel opens on that project. Explorer starts a batch file in its own
rem directory, which is the project, so no cd is needed.

where t4 >nul 2>nul
if %ERRORLEVEL% == 0 (
    t4 app
    goto :eof
)

rem Not on PATH: fall back to the module via the py launcher, then python.
py -c "import t4sync" >nul 2>nul
if %ERRORLEVEL% == 0 (
    py -m t4sync app
    goto :eof
)
python -c "import t4sync" >nul 2>nul
if %ERRORLEVEL% == 0 (
    python -m t4sync app
    goto :eof
)

echo t4-sync is not installed for any Python on this machine.
echo Install it by running install.ps1 from the t4-sync checkout, then
echo double-click this again.
echo.
pause
