# Tao shortcut CONTENT.lnk, chay bang pythonw (khong co CMD)
$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$pydir   = Split-Path (Get-Command python).Source
$pythonw = Join-Path $pydir "pythonw.exe"

$ws  = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$root\CONTENT.lnk")
$lnk.TargetPath       = $pythonw
$lnk.Arguments        = "`"$root\gui.py`""
$lnk.WorkingDirectory = $root
$lnk.IconLocation     = $pythonw
$lnk.Save()

Write-Host "Da tao: $root\CONTENT.lnk" -ForegroundColor Green
Write-Host "Double-click CONTENT.lnk de mo khong co CMD" -ForegroundColor Cyan
