# 生成便携 Python 运行时（免安装，Win7 也能用）
#
# 用法（在项目根目录，用 PowerShell 管理员或普通权限都行）：
#   powershell -ExecutionPolicy Bypass -File tools\make_portable.ps1
#
# 做完之后双击 启动-便携.bat 即可，目标电脑不需要装 Python。
# 之所以选 3.8.10：这是最后一个官方支持 Windows 7 的 Python 版本。

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$runtime = Join-Path $root 'runtime'
$url = 'https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-amd64.zip'
$zip = Join-Path $env:TEMP 'python-3.8.10-embed-amd64.zip'

Write-Host ""
Write-Host "项目根目录: $root"
Write-Host "目标目录  : $runtime"
Write-Host ""

if (Test-Path (Join-Path $runtime 'python.exe')) {
    Write-Host "[跳过] runtime\python.exe 已存在。想重新下载就先删掉 runtime 目录。" -ForegroundColor Yellow
    & (Join-Path $runtime 'python.exe') -c "import sys; print('[OK] 现有运行时:', sys.version.split()[0])"
    exit 0
}

# Win7 的 .NET 默认可能还是 TLS 1.0，下载 python.org 会失败
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    Write-Host "[警告] 无法设置 TLS 1.2，下载可能失败。" -ForegroundColor Yellow
}

if (-not (Test-Path $zip)) {
    Write-Host "[下载] $url"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
} else {
    Write-Host "[跳过] 安装包已在缓存: $zip"
}

if (-not (Test-Path $runtime)) {
    New-Item -ItemType Directory -Path $runtime | Out-Null
}
Write-Host "[解压] -> $runtime"
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $runtime)
} catch {
    # 老 PowerShell 兜底
    Expand-Archive -Path $zip -DestinationPath $runtime -Force
}

# embeddable 包靠 python38._pth 决定 sys.path：把项目根目录加进去，
# 这样 `import autoexam` 不用额外设 PYTHONPATH。
$pth = Join-Path $runtime 'python38._pth'
if (Test-Path $pth) {
    $lines = Get-Content $pth
    if (-not ($lines -contains '..')) {
        $lines = @('..') + $lines
        Set-Content -Path $pth -Value $lines -Encoding ASCII
        Write-Host "[改写] python38._pth 已加入 '..'（项目根目录）"
    }
} else {
    Write-Host "[警告] 没找到 python38._pth，若 import 失败请手工处理。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[校验] 便携运行时版本："
& (Join-Path $runtime 'python.exe') -c "import sys; print('   ', sys.version.split()[0], '|', sys.executable)"

Write-Host ""
Write-Host "[校验] 能否加载本项目代码："
Push-Location $root
try {
    & (Join-Path $runtime 'python.exe') run_autoexam.py --help | Select-Object -First 3
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "完成。以后双击 启动-便携.bat 就能跑（目标机不需要装 Python）。" -ForegroundColor Green
Write-Host "整个 runtime 目录约 15~20MB，可以和应用一起拷到别的电脑。"
