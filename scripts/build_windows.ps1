# Builds dist\RemoteSub (PyInstaller) and dist\RemoteSub-Setup-<version>.exe (Inno Setup),
# with the latest chiaki-ng Windows installer bundled inside.
param(
    [string]$Version = "0.1.0",
    [switch]$RefreshChiaki
)
# Not "Stop": Windows PowerShell treats any stderr line from pip/PyInstaller as an error.
# Exit codes are checked explicitly instead.
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

python -m pip install -q -r requirements.txt
if ($LASTEXITCODE) { throw "pip install failed" }

# RapidOCR's optional engines would pull in PyTorch and friends; it only needs onnxruntime here.
$exclude = 'platform_mac', 'torch', 'torchvision', 'tensorrt', 'paddle', 'openvino', 'MNN', 'matplotlib', 'scipy', 'pandas', 'IPython' |
    ForEach-Object { '--exclude-module', $_ }
python -m PyInstaller --noconfirm --clean --windowed --name RemoteSub --paths src `
    --collect-all rapidocr --hidden-import oneocr @exclude `
    --distpath dist --workpath build src\remotesub.py
if ($LASTEXITCODE) { throw "PyInstaller failed" }

$redist = Join-Path $root "installer\windows\redist\chiaki-ng-setup.exe"
if ($RefreshChiaki -or -not (Test-Path $redist)) {
    $release = Invoke-RestMethod https://api.github.com/repos/streetpea/chiaki-ng/releases/latest
    $asset = $release.assets | Where-Object { $_.name -like "*win_x64*installer*.exe" } | Select-Object -First 1
    if (-not $asset) { throw "chiaki-ng Windows installer not found in $($release.tag_name)" }
    New-Item -ItemType Directory -Force (Split-Path $redist) | Out-Null
    Write-Host "Downloading chiaki-ng $($release.tag_name): $($asset.name)"
    Invoke-WebRequest $asset.browser_download_url -OutFile $redist
}

$iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found" }
& $iscc "/DAppVersion=$Version" installer\windows\RemoteSub.iss
if ($LASTEXITCODE) { throw "ISCC failed" }
Get-Item "dist\RemoteSub-Setup-$Version.exe"
