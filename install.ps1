# install.ps1 - one-time setup for wx (WeChat desktop draft tool)
#
# What it does:
#   1. creates a local Python venv next to this script (no global installs)
#   2. installs the runtime dependencies
#   3. verifies the automation library imports
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
#
# Requirements: Windows 10/11, WeChat 4.x (Weixin.exe) installed and logged in,
#               Python 3.10+ on PATH, Git Bash (for the wx client shim).

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here 'venv'
$py   = Join-Path $venv 'Scripts\python.exe'

Write-Host "== pi-wechat-draft installer ==" -ForegroundColor Cyan
Write-Host "install dir: $here"

# --- locate a python interpreter ---------------------------------------------
$base = $null
foreach ($cand in @('py -3', 'python')) {
    try {
        $exe = $cand.Split(' ')[0]
        if (Get-Command $exe -ErrorAction SilentlyContinue) { $base = $cand; break }
    } catch { }
}
if (-not $base) {
    throw "Python not found on PATH. Install Python 3.10+ (python.org) and re-run."
}
Write-Host "python: $base"

# --- create the venv ---------------------------------------------------------
if (-not (Test-Path $py)) {
    Write-Host "creating venv..." -ForegroundColor Cyan
    if ($base -eq 'py -3') { & py -3 -m venv $venv } else { & python -m venv $venv }
} else {
    Write-Host "venv already exists, reusing it"
}

# --- dependencies ------------------------------------------------------------
$deps = @(
    'wechatauto-replica>=1.2.2.5',   # WeChat 4.x automation (UIA + OCR hybrid)
    'uiautomation>=2.0.20',          # UI Automation client
    'pywin32>=306',                  # Win32 APIs (windows, clipboard, DPI)
    'pillow>=10',                    # screen sampling for the ink check
    'pyperclip>=1.8',                # clipboard (fallback paste path only)
    'comtypes>=1.2'                  # COM glue used by uiautomation
)
Write-Host "installing dependencies (this may take a minute)..." -ForegroundColor Cyan
& $py -m pip install --upgrade pip --quiet
foreach ($d in $deps) { & $py -m pip install --quiet $d }

# --- verify ------------------------------------------------------------------
Write-Host "verifying imports..." -ForegroundColor Cyan
& $py -c "import wechatauto, uiautomation, win32gui, PIL, pyperclip; print('imports OK')"
& $py -c "import wechatauto, importlib.metadata as m; print('wechatauto-replica', m.version('wechatauto-replica'))"

$svc = Join-Path $here 'scripts\wx_service.py'
& $py -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read()); print('wx_service.py parses OK')" $svc

# --- runtime config template -------------------------------------------------
# The silent-mode preferences live in <repo>/wx-service.env (same dir as this script,
# because that is the default WX_DIR). Ship a commented template; never overwrite.
$envTpl  = Join-Path $here 'wx-service.env.example'
$envFile = Join-Path $here 'wx-service.env'
if ((Test-Path $envTpl) -and -not (Test-Path $envFile)) {
    Copy-Item -LiteralPath $envTpl -Destination $envFile
    Write-Host "wrote runtime config: wx-service.env (quiet defaults)" -ForegroundColor Cyan
} elseif (Test-Path $envFile) {
    Write-Host "runtime config already exists, left as is: wx-service.env"
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1) optional - another read-only WeChat reader for stronger verification:"
Write-Host "     setx WX_READER ""C:\path\to\rion_wechat_reader.py"""
Write-Host "     (without it, the tool still works; verification uses the UIA readback)"
Write-Host "  2) put the client on PATH (Git Bash):"
Write-Host "     echo 'export PATH=\""$here`"\':`$PATH' >> ~/.bashrc"
Write-Host "     or call scripts\wx.cmd directly from cmd / PowerShell"
Write-Host "  3) first run:"
Write-Host "     wx svc start        # start the background service"
Write-Host "     wx svc status       # should say: running pid=..."
Write-Host ""
Write-Host "Read SKILL.md for the full workflow and the pitfalls that matter."
Write-Host "Tune behaviour in wx-service.env (idle gate, fallback, blocker restore)."
