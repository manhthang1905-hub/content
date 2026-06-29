# =============================================================================
#  CONTENT --- Setup script cho may moi (Windows, PowerShell)
#  Chay: Right-click -> "Run with PowerShell" hoac: .\setup.ps1
# =============================================================================

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "`n=== CONTENT Setup ===" -ForegroundColor Cyan

# ------ 1. Kiem tra Python 3.11+ ------------------------------------------------------------------------------------------------------------------------------------------------------
Write-Host "`n[1/5] Kiem tra Python..." -ForegroundColor Yellow
try {
    $pyver = python --version 2>&1
    if ($pyver -notmatch "3\.(11|12|13)") {
        Write-Host "  CANH BAO: Phat hien $pyver --- tool test tren 3.11" -ForegroundColor Yellow
        Write-Host "  Tai Python 3.11: https://www.python.org/downloads/release/python-3119/"
    } else {
        Write-Host "  OK: $pyver" -ForegroundColor Green
    }
} catch {
    Write-Host "  LOI: Khong tim thay Python!" -ForegroundColor Red
    Write-Host "  Tai tai: https://www.python.org/downloads/release/python-3119/"
    Write-Host "  (Check 'Add Python to PATH' khi cai)"
    exit 1
}

# ------ 2. Kiem tra Node.js 18+ ---------------------------------------------------------------------------------------------------------------------------------------------------------
Write-Host "`n[2/5] Kiem tra Node.js..." -ForegroundColor Yellow
try {
    $nodever = node --version 2>&1
    $major = [int]($nodever -replace 'v(\d+)\..*', '$1')
    if ($major -lt 18) {
        Write-Host "  CANH BAO: $nodever --- can Node.js 18+. Tai: https://nodejs.org/" -ForegroundColor Yellow
    } else {
        Write-Host "  OK: Node.js $nodever" -ForegroundColor Green
    }
} catch {
    Write-Host "  CANH BAO: Khong tim thay Node.js" -ForegroundColor Yellow
    Write-Host "  Can cho YouTube bypass (Method 2). Tai: https://nodejs.org/en/download (chon LTS)"
}

# ------ 3. Kiem tra ffmpeg ------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Write-Host "`n[3/5] Kiem tra ffmpeg..." -ForegroundColor Yellow
try {
    $ff = ffmpeg -version 2>&1 | Select-Object -First 1
    Write-Host "  OK: $ff" -ForegroundColor Green
} catch {
    Write-Host "  CANH BAO: Khong tim thay ffmpeg (can cho Whisper fallback)" -ForegroundColor Yellow
    Write-Host "  Tai: https://www.gyan.dev/ffmpeg/builds/ -> ffmpeg-release-essentials.zip"
    Write-Host "  Giai nen -> copy ffmpeg.exe vao C:\Windows\System32\"
}

# ------ 3b. Microsoft Visual C++ Redistributable (torch/whisper load DLL nay) -------------------
# Thieu cai nay -> import torch/whisper bao WinError 126 "module could not be found" -> Method 4 chet.
Write-Host "`n[3b] Kiem tra Visual C++ Redistributable (torch can)..." -ForegroundColor Yellow
$vcInstalled = $false
try {
    $vc = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction Stop
    if ($vc.Installed -eq 1) { $vcInstalled = $true }
} catch {}
if ($vcInstalled) {
    Write-Host "  OK: Da cai VC++ Redistributable" -ForegroundColor Green
} else {
    Write-Host "  Chua co -> tai + cai vc_redist.x64.exe (im lang)..." -ForegroundColor Yellow
    try {
        $vcExe = Join-Path $env:TEMP "vc_redist.x64.exe"
        Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $vcExe -UseBasicParsing
        Start-Process -FilePath $vcExe -ArgumentList "/install","/quiet","/norestart" -Wait
        Write-Host "  OK: Da cai VC++ Redistributable" -ForegroundColor Green
    } catch {
        Write-Host "  LOI tai/cai VC++: $_" -ForegroundColor Red
        Write-Host "  Tai tay: https://aka.ms/vs/17/release/vc_redist.x64.exe" -ForegroundColor Yellow
    }
}

# ------ 4. Cai Python packages ------------------------------------------------------------------------------------------------------------------------------------------------------------
Write-Host "`n[4/5] Cai Python packages..." -ForegroundColor Yellow
Set-Location $ROOT

python -m pip install --upgrade pip --quiet

Write-Host "  Cai curl-cffi==0.14.0 (pinned - sai version la crash)..."
pip install "curl-cffi==0.14.0" --quiet

# torch + openai-whisper = Method 4 (mp3 -> Whisper), PHAO CUOI khi YouTube chan IP.
# Cai LOUD (khong --quiet) de neu loi mang/dia/RAM thi thay ngay, khong am tham bo qua.
Write-Host "  Cai torch + openai-whisper (Method 4, co the mat 5-10 phut)..."
pip install torch openai-whisper

Write-Host "  Cai cac package con lai..."
pip install -r requirements.txt --quiet

Write-Host "  OK: Tat ca packages da cai" -ForegroundColor Green

# ------ 4b. Kiem tra import that su (bat loi cai am tham) ------------------------------------------
Write-Host "`n[4b] Kiem tra dependencies (Method 1-4)..." -ForegroundColor Yellow
$pycheck = @"
import importlib
mods = [
 ('youtube_transcript_api','Method 1 (sub san co)'),
 ('yt_dlp','Method 2 (yt-dlp sub)'),
 ('curl_cffi','Method 2 bypass (impersonate chrome)'),
 ('whisper','Method 4 (mp3 -> Whisper) = PHAO CUOI'),
]
for m,desc in mods:
    try:
        importlib.import_module(m)   # import THAT su, bat loi ABI (numpy/torch)
        print('OK    | ' + m + ' -> ' + desc)
    except Exception as e:
        print('THIEU | ' + m + ' -> ' + desc + '  [' + type(e).__name__ + ': ' + str(e)[:80] + ']')
"@
$checkOut = $pycheck | python -
foreach ($line in $checkOut) {
    if ($line -match '^THIEU') { Write-Host "  $line" -ForegroundColor Red }
    else { Write-Host "  $line" -ForegroundColor Green }
}
if ($checkOut -match 'THIEU.*whisper') {
    Write-Host "  !! whisper CHUA cai duoc -> Method 4 se khong cuu duoc job khi YouTube chan IP." -ForegroundColor Red
    Write-Host "     Cach khac: them OPENAI_API_KEY vao api_keys.json (dung Whisper API), hoac chay tay:" -ForegroundColor Yellow
    Write-Host "       pip install torch openai-whisper" -ForegroundColor Yellow
}

# ------ 5. Tao folder neu chua co ---------------------------------------------------------------------------------------------------------------------------------------------------
Write-Host "`n[5/5] Tao folders..." -ForegroundColor Yellow

$configDir = Join-Path $ROOT "config"
if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir | Out-Null
    Write-Host "  Tao: config/" -ForegroundColor Green
} else {
    Write-Host "  OK: config/ da ton tai" -ForegroundColor Green
}

$outputDir = Join-Path $ROOT "output"
if (-not (Test-Path $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir | Out-Null
    Write-Host "  Tao: output/" -ForegroundColor Green
} else {
    Write-Host "  OK: output/ da ton tai" -ForegroundColor Green
}

# ------ 6. Tao shortcut CONTENT.lnk (chay khong co CMD) -------------------------------------------------------------------------------------------------
Write-Host "`n[6] Tao shortcut..." -ForegroundColor Yellow
try {
    $pydir   = Split-Path (Get-Command python).Source
    $pythonw = Join-Path $pydir "pythonw.exe"
    $ws  = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut("$ROOT\CONTENT.lnk")
    $lnk.TargetPath       = $pythonw
    $lnk.Arguments        = "`"$ROOT\gui.py`""
    $lnk.WorkingDirectory = $ROOT
    $lnk.IconLocation     = $pythonw
    $lnk.Save()
    Write-Host "  OK: CONTENT.lnk (double-click de mo)" -ForegroundColor Green
} catch {
    Write-Host "  CANH BAO: Khong tao duoc shortcut: $_" -ForegroundColor Yellow
}

# ------ Cau hinh API backend ----------------------------------------------------------------------------------------------------------------------------------
Write-Host "`n[Extra] Cau hinh API backend..." -ForegroundColor Yellow
Write-Host "  [1] Claude Code CLI (khuyen nghi - dung tai khoan Max 20x voi Opus 4.8)"
Write-Host "  [2] HTTP API (dung routerapi / ANTHROPIC_API_KEY)"
$choice = Read-Host "  Chon (1/2, Enter = 1)"
if ($choice -eq "2") {
    Write-Host "  -> Giu api_backend: http (mac dinh)" -ForegroundColor Gray
} else {
    Write-Host "  -> Cai/cap nhat Claude Code CLI len phien ban moi nhat..." -ForegroundColor Yellow
    npm install -g @anthropic-ai/claude-code
    $claudeVer = claude --version 2>&1
    Write-Host "  -> Claude CLI: $claudeVer" -ForegroundColor Green

    $configPath = Join-Path $ROOT "config\config.yaml"
    if (Test-Path $configPath) {
        (Get-Content $configPath -Raw) -replace "api_backend: http", "api_backend: cli" | Set-Content $configPath -Encoding UTF8
        Write-Host "  -> Da set api_backend: cli + cli_model: claude-opus-4-8" -ForegroundColor Green
    } else {
        Write-Host "  -> config.yaml chua co - copy truoc roi chay setup lai" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "  -> Dang nhap tai khoan Claude Max (trinh duyet se mo)..." -ForegroundColor Yellow
    claude login
    Write-Host ""
    Write-Host "  -> Kiem tra model claude-opus-4-8..." -ForegroundColor Yellow
    $testOut = claude --model claude-opus-4-8 --print "reply OK" 2>&1
    if ($testOut -match "OK") {
        Write-Host "  -> claude-opus-4-8 san sang!" -ForegroundColor Green
    } else {
        Write-Host "  -> Phan hoi: $testOut" -ForegroundColor Yellow
    }
}

# ------ Ket noi Network Drives -------------------------------------------------------------------------------------------------------------------------------
Write-Host "`n[Extra] Ket noi network drives..." -ForegroundColor Yellow
$envPath = Join-Path $ROOT "config\.env"
if (Test-Path $envPath) {
    $envContent = Get-Content $envPath -Raw
    if ($envContent -notmatch "SMB_USER") {
        Add-Content $envPath "`n# SMB network drives`nSMB_USER=smbuser`nSMB_PASS=159753" -Encoding UTF8
        Write-Host "  -> Da them SMB_USER/SMB_PASS vao .env" -ForegroundColor Green
    }
}
$drives = @{ "X" = "\\192.168.88.41\D"; "Y" = "\\192.168.88.183\D"; "Z" = "\\192.168.88.254\D" }
foreach ($letter in $drives.Keys) {
    $drive = "${letter}:"
    $path = $drives[$letter]
    net use $drive /delete /yes 2>$null | Out-Null
    $result = net use $drive $path /user:smbuser 159753 /persistent:yes 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  -> ${drive} ket noi OK: $path" -ForegroundColor Green
    } else {
        Write-Host "  -> ${drive} that bai: $result" -ForegroundColor Yellow
    }
}

# ------ Xong ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Write-Host "`n=== HOAN THANH ===" -ForegroundColor Green

Write-Host @"

Viec can lam thu cong (copy tu may chinh sang):

  config/
    .env                      <-- API key + ANTHROPIC_BASE_URL (du phong HTTP)
    creds.json                <-- Google service account
    config.yaml               <-- sua active_topic cho may nay
    youtube.com_cookies.txt   <-- export lai tu Chrome tren MAY NAY

  Neu dung CLI backend:
    claude login              <-- dang nhap tai khoan Max 20x

  Sau do chay:
    python gui.py

"@ -ForegroundColor White

Write-Host "Nhan phim bat ky de dong..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

