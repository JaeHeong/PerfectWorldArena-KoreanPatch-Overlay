# Builds dist\PWAKoreanOverlay\PWAKoreanOverlay.exe (one-folder build).
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# PyInstaller wipes its output folder on every build, so it builds into
# build\stage and only the program files are mirrored into dist\PWAKoreanOverlay.
# The user's data there - data\ (cache, user dictionary, glossary), logs\ and
# config.json - is never deleted or overwritten. Only the bundled mappings and the
# published shared cache are refreshed.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

uv sync
uv run python packaging\make_icon.py

$stage = Join-Path $root "build\stage"
uv run pyinstaller --noconfirm --clean `
    --name PWAKoreanOverlay `
    --windowed `
    --icon (Join-Path $root "packaging\icon.ico") `
    --paths (Join-Path $root "src") `
    --collect-all rapidocr `
    --collect-all onnxruntime `
    --distpath $stage --workpath build --specpath build `
    (Join-Path $root "packaging\launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$app = Join-Path $root "dist\PWAKoreanOverlay"
New-Item -ItemType Directory -Force $app | Out-Null
# mirror program files; excluded dirs/files are neither copied nor purged
robocopy (Join-Path $stage "PWAKoreanOverlay") $app /MIR /XD data logs /XF config.json /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
$global:LASTEXITCODE = 0

$data = Join-Path $app "data"
New-Item -ItemType Directory -Force (Join-Path $data "mappings") | Out-Null
# bundled mappings and the published shared cache are refreshed (the app merges the
# shared cache into its own cache on the next start, then deletes the copy)...
Copy-Item -Force data\mappings\*.json (Join-Path $data "mappings")
if (Test-Path data\translation_cache.shared.json) {
    Copy-Item -Force data\translation_cache.shared.json $data
}
# ...but anything the user edits or accumulates is never overwritten
foreach ($f in @("user_dict.json", "glossary.json")) {
    $dst = Join-Path $data $f
    if ((Test-Path "data\$f") -and -not (Test-Path $dst)) { Copy-Item "data\$f" $dst }
}
Copy-Item -Force README.md $app
Copy-Item -Force packaging\icon.ico $app
Write-Host "built: $app\PWAKoreanOverlay.exe"
