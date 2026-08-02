@echo off
rem Creates a Start-with-Windows shortcut for Desktop Grid.
powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Startup') + '\DesktopGrid.lnk');" ^
  "$lnk.TargetPath = $env:LocalAppData + '\Programs\Python\Python312\pythonw.exe';" ^
  "$lnk.Arguments = '\"%~dp0main.py\" --tray';" ^
  "$lnk.WorkingDirectory = '%~dp0';" ^
  "$lnk.Save()"
echo Desktop Grid will now start with Windows.
pause
