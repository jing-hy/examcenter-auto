# 打成免安装的 exe 目录（PyInstaller onedir）
#
# 关键：必须用 **Python 3.8** 打包。Python 3.9+ 的运行时依赖 Win7 没有的系统 API，
# 打出来的 exe 在 Win10/11 能跑、回 Win7 会直接报错。
#
# 用法（在项目根目录）：
#   py -3.8 -m pip install pyinstaller==5.13.2
#   powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
#
# 产物：dist\autoexam\ 整个目录（可压缩后分发，目标机不需要 Python）

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Push-Location $root
try {
    Write-Host ""
    Write-Host "项目根目录: $root"

    # 校验 Python 3.8
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyLauncher) {
        Write-Host "[错误] 没找到 py 启动器；请安装 Python 3.8 后再打包。" -ForegroundColor Red
        exit 1
    }
    $version = (& py -3.8 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1)
    if ($version -ne "3.8") {
        Write-Host "[错误] 需要用 Python 3.8 打包，当前拿到：$version" -ForegroundColor Red
        Write-Host "       安装：https://www.python.org/downloads/release/python-3810/" -ForegroundColor Yellow
        exit 1
    }
    & py -3.8 -c "import PyInstaller, sys; sys.exit(0 if PyInstaller.__version__.startswith('5.') else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 需要 PyInstaller 5.x（6.x 的 bootloader 不再支持 Win7）。" -ForegroundColor Red
        Write-Host "       py -3.8 -m pip install pyinstaller==5.13.2" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "[打包] PyInstaller onedir ..."
    & py -3.8 -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --console `
        --name "autoexam" `
        --add-data "config.json;." `
        --add-data "accounts.example.txt;." `
        run_autoexam.py
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Copy-Item -Path (Join-Path $root "README.md") -Destination (Join-Path $root "dist\autoexam\README.md") -Force
    Copy-Item -Path (Join-Path $root "LICENSE") -Destination (Join-Path $root "dist\autoexam\LICENSE") -Force

    $zip = Join-Path $root "dist\autoexam-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $root "dist\autoexam\*") -DestinationPath $zip -Force

    Write-Host ""
    Write-Host "完成：dist\autoexam\autoexam.exe" -ForegroundColor Green
    Write-Host "压缩包：dist\autoexam-win64.zip" -ForegroundColor Green
    Write-Host "把整个 dist\autoexam 目录（或压缩包）拷到目标电脑即可，不需要 Python。"
} finally {
    Pop-Location
}
