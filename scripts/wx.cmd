@echo off
rem wx - Windows command entry (cmd / PowerShell). Draft-only by default: it fills
rem the input box and never presses send unless you pass --confirm.
rem Set WX_BASH to point at Git Bash if it is installed somewhere unusual.
setlocal
if not defined WX_BASH (
  for %%P in (
    "%ProgramFiles%\Git\bin\bash.exe"
    "%ProgramFiles%\Git\usr\bin\bash.exe"
    "%ProgramFiles(x86)%\Git\bin\bash.exe"
    "%LocalAppData%\Programs\Git\bin\bash.exe"
  ) do (
    if not defined WX_BASH if exist %%P set "WX_BASH=%%~fP"
  )
)
if not defined WX_BASH (
  echo wx: Git Bash not found. Set WX_BASH to bash.exe ^(e.g. set WX_BASH=C:\Program Files\Git\bin\bash.exe^)
  exit /b 2
)
"%WX_BASH%" "%~dp0wx.sh" %*
