# 截图 + Windows OCR：给不能读图的模型提供微信窗口的文字与坐标
# 用法: powershell -NoProfile -File wx-ocr.ps1 [-Pid 18364] [-OutDir <dir>] [-RawOnly]
param(
  [int]$ProcId = 0,
  [string]$OutDir = "$env:TEMP",
  [switch]$RawOnly,
  [string]$Crop = '',
  [int]$Scale = 1
)

$ErrorActionPreference = 'Stop'

Add-Type @"
using System;using System.Runtime.InteropServices;
public class WxWin {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  public struct RECT { public int L,T,R,B; }
}
"@
[WxWin]::SetProcessDPIAware() | Out-Null
Add-Type -AssemblyName System.Drawing

if ($ProcId -eq 0) {
  $p = Get-Process | Where-Object { $_.ProcessName -in @('Weixin','WeChat') -and $_.MainWindowHandle -ne 0 } | Select-Object -First 1
} else {
  $p = Get-Process -Id $ProcId
}
if (-not $p) { Write-Output "NO_WECHAT_WINDOW"; exit 3 }

$h = $p.MainWindowHandle
$iconic = [WxWin]::IsIconic($h)
if ($iconic) { [WxWin]::ShowWindow($h, 9) | Out-Null; Start-Sleep -Milliseconds 800 }
[WxWin]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 600

$r = New-Object WxWin+RECT
[WxWin]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.R - $r.L; $ht = $r.B - $r.T
Write-Output "WINDOW pid=$($p.Id) title=$($p.MainWindowTitle) rect=$($r.L),$($r.T) size=${w}x${ht} wasIconic=$iconic"

$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size)
$g.Dispose()
if ($Crop) {
  $p4 = $Crop -split ','
  $cr = New-Object System.Drawing.Rectangle([int]$p4[0], [int]$p4[1], [int]$p4[2], [int]$p4[3])
  $sub = $bmp.Clone($cr, $bmp.PixelFormat)
  $bmp.Dispose()
  if ($Scale -gt 1) {
    $big = New-Object System.Drawing.Bitmap(($sub.Width * $Scale), ($sub.Height * $Scale))
    $g2 = [System.Drawing.Graphics]::FromImage($big)
    $g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g2.DrawImage($sub, 0, 0, $big.Width, $big.Height)
    $g2.Dispose(); $sub.Dispose()
    $bmp = $big
  } else { $bmp = $sub }
}
$png = Join-Path $OutDir "wx-shot-$(Get-Date -Format 'HHmmss').png"
$bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output "SHOT $png"
if ($RawOnly) { Write-Output "ORIGIN $($r.L),$($r.T)"; exit 0 }

# ---- Windows.Media.Ocr ----
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $type) {
  $t = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  $t.Result
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { Write-Output "OCR_UNAVAILABLE language packs missing"; exit 4 }
Write-Output "OCR_LANG $($engine.RecognizerLanguage.LanguageTag)"

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($png)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$sb = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$res = Await ($engine.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult])
$stream.Dispose()

Write-Output "ORIGIN $($r.L),$($r.T)"
Write-Output "---LINES---"
foreach ($line in $res.Lines) {
  try {
    $ws = @($line.Words)
    if ($ws.Count -eq 0) { continue }
    $x = [int][math]::Round($ws[0].BoundingRect.X); $y = [int][math]::Round($ws[0].BoundingRect.Y)
    $x2 = [int][math]::Round($ws[$ws.Count-1].BoundingRect.X + $ws[$ws.Count-1].BoundingRect.Width)
    $y2 = [int][math]::Round($ws[0].BoundingRect.Y + $ws[0].BoundingRect.Height)
    "{0,5},{1,5} {2,5},{3,5} | {4}" -f $x, $y, $x2, $y2, $line.Text
  } catch {
    "       ???? | (unparsed) $($line.Text)"
  }
}
