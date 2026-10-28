# QC360 AI - offline kit for PCs without internet.
#   -Make     (on a PC WITH internet) downloads every program and Python package into pc\offline_kit
#   -Install  (on any PC, no internet needed) installs everything from pc\offline_kit
param([switch]$Make, [switch]$Install)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot           # the tester folder (the one with beta\ and pc\)
$Kit  = Join-Path $PSScriptRoot "offline_kit"
$Req  = Join-Path $PSScriptRoot "requirements.txt"
$Tools = Join-Path $env:LOCALAPPDATA "QC360\tools"  # Poppler and FFmpeg are unpacked here (no admin rights needed)

$Packages = @(
  @{ id = "Python.Python.3.13";               name = "Python 3.13";    pattern = "*Python*" },
  @{ id = "Google.Chrome";                    name = "Google Chrome";  pattern = "*Chrome*" },
  @{ id = "TheDocumentFoundation.LibreOffice"; name = "LibreOffice";    pattern = "*LibreOffice*" },
  @{ id = "oschwartz10612.Poppler";           name = "Poppler";        pattern = "*Poppler*" },
  @{ id = "Gyan.FFmpeg";                      name = "FFmpeg";         pattern = "*FFmpeg*" }
)

function Say($t) { Write-Host "  $t" }
function Find-Kit($pattern, $ext) { Get-ChildItem -Path $Kit -Recurse -File -Filter "$pattern$ext" -ErrorAction SilentlyContinue | Select-Object -First 1 }

function Find-Python {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { return $cmd.Source }
  foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\Python313\python.exe", "$env:ProgramFiles\Python313\python.exe")) {
    if (Test-Path $p) { return $p }
  }
  return $null
}

if ($Make) {
  Write-Host "`n  QC360 AI - making the offline kit in $Kit`n"
  New-Item -ItemType Directory -Force -Path $Kit | Out-Null
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Say "winget is not available on this PC. Download these by hand into $Kit :"
    Say "  Python 3.13 (Windows installer 64-bit)  https://www.python.org/downloads/"
    Say "  Google Chrome for Business (MSI)        https://chromeenterprise.google/download/"
    Say "  LibreOffice (Windows x86-64 MSI)        https://www.libreoffice.org/download/"
    Say "  Poppler for Windows (zip)               https://github.com/oschwartz10612/poppler-windows/releases"
    Say "  FFmpeg essentials build (zip)           https://www.gyan.dev/ffmpeg/builds/"
  } else {
    foreach ($p in $Packages) {
      Say "Downloading $($p.name) ..."
      $dir = Join-Path $Kit ($p.id)
      winget download --id $p.id -e -d $dir --accept-package-agreements --accept-source-agreements | Out-Host
      if ($LASTEXITCODE -ne 0) { Say "  Could not download $($p.name): download it by hand into $Kit (see README_PC.md)." }
    }
  }
  $py = Find-Python
  if (-not $py) {
    Say "Python is not installed on this PC, so the Python packages cannot be downloaded."
    Say "Install Python 3.13 here (or run this on a PC that has it), then run make_offline_kit.bat again."
    exit 1
  }
  Say "Downloading the Python packages ..."
  & $py -m pip download -r $Req -d (Join-Path $Kit "wheels") --only-binary=:all: --platform win_amd64 --python-version 3.13 --implementation cp | Out-Host
  if ($LASTEXITCODE -ne 0) { Say "The Python packages could not be downloaded."; exit 1 }
  Write-Host "`n  Done. Copy the whole tester folder (with pc\offline_kit) to the other PCs and run pc\setup.bat there.`n"
  exit 0
}

if ($Install) {
  Write-Host "`n  QC360 AI - installing from the offline kit (no internet needed)`n"
  # 1. Python
  $py = Find-Python
  if (-not $py) {
    $inst = Find-Kit "*Python*" ".exe"
    if (-not $inst) { Say "Python 3.13 is not installed and its installer is not in the kit."; exit 1 }
    Say "1/5  Installing Python 3.13 ..."
    Start-Process -FilePath $inst.FullName -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0" -Wait
    $py = Find-Python
    if (-not $py) { Say "Python did not install. Run the Python installer in the kit by hand, then run setup.bat again."; exit 1 }
  } else { Say "1/5  Python found: $py" }
  # 2. Python packages, from the kit only
  Say "2/5  Python packages ..."
  & $py -m pip install --quiet --no-index --find-links (Join-Path $Kit "wheels") -r $Req
  if ($LASTEXITCODE -ne 0) { Say "The Python packages could not be installed from the kit."; exit 1 }
  # 3. Google Chrome and LibreOffice (MSI installers; Windows may ask for administrator rights)
  foreach ($p in @(@{ n = "Google Chrome"; pat = "*Chrome*" }, @{ n = "LibreOffice"; pat = "*LibreOffice*" })) {
    $msi = Find-Kit $p.pat ".msi"; $exe = Find-Kit $p.pat ".exe"
    $have = if ($p.n -eq "Google Chrome") { (Test-Path "$env:ProgramFiles\Google\Chrome\Application\chrome.exe") -or (Test-Path "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") -or (Test-Path "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") }
            else { Test-Path "$env:ProgramFiles\LibreOffice\program\soffice.exe" }
    if ($have) { Say "3/5  $($p.n) found"; continue }
    Say "3/5  Installing $($p.n) ..."
    if ($msi) { Start-Process msiexec.exe -ArgumentList "/i `"$($msi.FullName)`" /qn /norestart" -Wait -Verb RunAs }
    elseif ($exe) { Start-Process -FilePath $exe.FullName -ArgumentList "/silent /install" -Wait -Verb RunAs }
    else { Say "     $($p.n) is not in the kit: install it by hand." }
  }
  # 4. Poppler and FFmpeg: unpacked into the user's folder (found automatically by the tester)
  New-Item -ItemType Directory -Force -Path $Tools | Out-Null
  foreach ($p in @(@{ n = "Poppler"; pat = "*Poppler*" }, @{ n = "FFmpeg"; pat = "*FFmpeg*" })) {
    $zip = Find-Kit $p.pat ".zip"
    if (-not $zip) { Say "4/5  $($p.n) is not in the kit: install it by hand (see README_PC.md)."; continue }
    $dest = Join-Path $Tools $p.n.ToLower()
    if (Test-Path $dest) { Say "4/5  $($p.n) found"; continue }
    Say "4/5  Unpacking $($p.n) ..."
    Expand-Archive -Path $zip.FullName -DestinationPath $dest -Force
  }
  # 5. Check
  Say "5/5  Checking this PC ..."
  Push-Location $Root
  & $py -X utf8 -m beta.local setup
  Pop-Location
  exit 0
}

Write-Host "Use: offline_kit.ps1 -Make   or   offline_kit.ps1 -Install"
exit 1
