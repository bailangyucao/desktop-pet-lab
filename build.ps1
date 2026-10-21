param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    $sprite = Join-Path $PSScriptRoot 'assets/spritesheet.png'
    if (-not (Test-Path -LiteralPath $sprite)) {
        throw '缺少 assets/spritesheet.png。请先放入具有使用授权的透明精灵图，参见 assets/README.md。'
    }
    $buildArgs = @('-m','PyInstaller','--noconfirm','--onefile','--windowed','--name','桌宠','--add-data',($sprite + ';assets'),'--exclude-module','numpy','--exclude-module','pandas','--exclude-module','matplotlib','--exclude-module','scipy','--exclude-module','cv2')
    # Some Python distributions load an additional certificate policy file in SSLContext.
    # Preserve that file when freezing, rather than altering TLS verification.
    $runtimeCert = & $Python -c "import pathlib,ssl; print(pathlib.Path(ssl.__file__).with_name('codex-rejected-cas.pem'))"
    if ($LASTEXITCODE -ne 0) { throw '无法检查 Python HTTPS 运行依赖。' }
    if (Test-Path -LiteralPath $runtimeCert) {
        $buildArgs += @('--add-data',($runtimeCert + ';.'))
    }
    $icon = Join-Path $PSScriptRoot 'assets/pet.ico'
    if (Test-Path -LiteralPath $icon) {
        $buildArgs += @('--icon',$icon,'--add-data',($icon + ';assets'))
    }
    $buildArgs += (Join-Path $PSScriptRoot 'desktop_pet.py')
    & $Python @buildArgs
    if ($LASTEXITCODE -ne 0) { throw '构建失败，请检查 PyInstaller 输出。' }
    Write-Output '构建完成：dist/桌宠.exe'
} finally {
    Pop-Location
}
