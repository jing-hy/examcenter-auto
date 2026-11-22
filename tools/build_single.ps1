# 把程序打成【单文件】exe（PyInstaller onefile）
#
# 为什么需要这个：tools\build_exe.ps1 打的是 onedir（一个目录 + 入口 exe），
# exe 必须和同目录的 python38.dll / base_library.zip / *.pyd 一起存在。
# 只把 autoexam.exe 单独拷走，双击会直接报
#   Error loading Python DLL '<目录>\python38.dll'. LoadLibrary: 找不到指定的模块。
# 而且报错发生在 bootloader 阶段，看起来就是“双击没反应、没窗口”。
#
# 本脚本产出 dist\autoexam-single.exe —— 真正只有一个文件，拷哪都能跑。
#
# 注意（重要）：onefile 用的是**当前解释器的 Python 运行时**。
#   - 用 Python 3.9+ 打包 => 目标机 Win7 跑不了（缺系统 API）。
#   - 需要 Win7 + 单文件：必须用 Python 3.8 + PyInstaller 5.x 打。
#   - 只给 Win10/11：用较新的 Python 打即可（体积略大、启动多 1~2 秒）。
#
# 用法（项目根目录）：
#   powershell -ExecutionPolicy Bypass -File tools\build_single.ps1
#   指定解释器：$env:PYBUILD = 'C:\path\python.exe'

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Push-Location $root
try {
    $pyExe = $env:PYBUILD
    $pyArgs = @()
    if (-not $pyExe) {
        # 优先用 runtimepython.exe（Python 3.8.10）：它打出来的单文件
        # 在 Win7 / 10 / 11 上都能跑，是唯一"一个 exe 通吃"的版本组合。
        $bundled = Join-Path $root 'runtime\python.exe'
        if (Test-Path $bundled) {
            $pyExe = $bundled
            Write-Host '[info] 使用 runtime\python.exe（Python 3.8，产物 Win7/10/11 通用）'
        } else {
            $pyExe = 'py'
            $pyArgs = @('-3')
            Write-Host '[提示] 没有 runtime\python.exe，回退到系统 Python —— 打出来的 exe 只能 Win10/11。' -ForegroundColor Yellow
            Write-Host '       要 Win7 通用：先跑 powershell -ExecutionPolicy Bypass -File tools\make_portable.ps1' -ForegroundColor Yellow
        }
    }

    $ver = (& $pyExe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1)
    Write-Host "[info] 打包解释器：Python $ver"
    if ($ver -notmatch '^3\.(7|8|9|1[0-9])$') {
        Write-Host "[错误] 解释器不可识别：$ver" -ForegroundColor Red
        exit 1
    }
    if ([int]($ver.Split('.')[1]) -ge 9) {
        Write-Host "[提示] 用 $ver 打出的单文件 exe 只能在 Win10/11 运行；需要 Win7 请用 Python 3.8 + PyInstaller 5.x。" -ForegroundColor Yellow
    }

    & $pyExe @pyArgs -c "import PyInstaller, sys; sys.exit(0 if PyInstaller.__version__ else 1)" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[错误] 没装 PyInstaller：' -ForegroundColor Red
        Write-Host "       $pyExe $($pyArgs -join ' ') -m pip install pyinstaller" -ForegroundColor Yellow
        exit 1
    }

    $outDir = Join-Path $root 'build_onefile'
    $distDir = Join-Path $outDir 'dist'
    if (Test-Path $distDir) { Remove-Item $distDir -Recurse -Force }

    Write-Host '[打包] PyInstaller onefile ...'
    & $pyExe @pyArgs -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --console `
        --name 'autoexam-single' `
        --distpath $distDir `
        --workpath (Join-Path $outDir 'work') `
        --specpath $outDir `
        --add-data "$root\config.json;." `
        --add-data "$root\accounts.example.txt;." `
        "$root\run_autoexam.py"
    if ($LASTEXITCODE -ne 0) { exit 1 }

    $produced = Join-Path $distDir 'autoexam-single.exe'
    if (-not (Test-Path $produced)) {
        Write-Host '[错误] 没找到产物 autoexam-single.exe' -ForegroundColor Red
        exit 1
    }
    $final = Join-Path $root 'dist\autoexam-single.exe'
    Copy-Item $produced $final -Force

    $size = [math]::Round((Get-Item $final).Length / 1MB, 2)
    Write-Host ''
    Write-Host "完成：$final（$size MB，单文件，目标机不需要 Python）" -ForegroundColor Green
    Write-Host '自检建议：拷到别的目录，双击应看到控制台菜单。'
} finally {
    Pop-Location
}
