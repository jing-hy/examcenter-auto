# examcenter-auto · 通用技术刷题自动化助手

面向「考试中心」（`/plugin/examcenter`）通用技术学业水平考试练习系统的命令行自动刷题工具。
纯 Python 标准库实现，不依赖浏览器自动化、不装任何第三方包；打包后是免安装的 exe，
**Windows 7 / 10 / 11 都能直接运行**。

默认策略（俗称「探针 + 错题本」）：

```text
选中试卷 → 进入模考（勾选「计入积分」+「重新开始」）
→ 每题先随便提交一个答案（探针）
→ 立刻查同一账号的错题本（错题 + 正确答案）
→ 按题干把正确答案匹配回来，重新提交该题
→ 下一题 …… 每题 7~13 秒，最后自动交卷
```

服务端若在题目里或 `/answer` 接口里直接给了正确答案，会优先走捷径，省掉轮询。
答案会缓存在本地，第二个账号刷同一套卷基本一次答对。

---

## 一、快速开始

### 方式 A：下载 exe（不需要装 Python）

到 [Releases](https://github.com/jing-hy/examcenter-auto/releases) 下载 `autoexam-win64.zip`，解压后双击 `autoexam.exe`。
**整个目录一起带走**，目标电脑不用装 Python。

> ⚠️ **别只把 `autoexam.exe` 一个文件复制出去。** 这个 exe 是「目录版」（PyInstaller onedir），
> 启动时要加载同目录的 `python38.dll` / `base_library.zip` / `*.pyd` / 一堆 `api-ms-win-*.dll`。
> 单独拷走只会得到 `Error loading Python DLL ... 找不到指定的模块`，而且错误发生在 bootloader 阶段，
> 表现就是「**双击没反应、连窗口都不出现**」。想要「就一个文件」，用下面的方式 A2。

### 方式 A2：单文件版（拷一个 exe 就能跑）

`dist/autoexam-single.exe` 是 onefile 打包，**只有一个文件**，放到任意目录双击即可。
运行时它会把自身解压到 `%TEMP%\_MEIxxxx`，所以首次启动比目录版多 1~2 秒，属正常现象；
**默认不往 exe 所在目录写任何东西**：日志、状态、答案缓存都放系统临时目录
（`%TEMP%\autoexam-run-xxxx`），程序正常退出时整个目录自动删除。
只有 `config.json` 是「有就用、没有就忽略」的只读输入。

> **兼容性（这是关键）**：仓库里这个 `autoexam-single.exe` 是用 **Python 3.8.10 + PyInstaller 5.13.2**
> 打的，**Win7 SP1 / Win10 / Win11 全都能跑** —— 这是唯一能「一个 exe 通吃三个系统」的版本组合
> （3.8 是最后一个支持 Win7 的 Python，5.x 是最后一个支持 Win7 的 PyInstaller）。
> 千万别用 3.9+ 重打：那种产物的运行时用到了 Win7 没有的系统 API，在 Win7 上会直接起不来。

### 方式 B：本机有 Python（3.8+）

```bat
双击 启动.bat
rem 或
python run_autoexam.py
```

### 方式 C：便携运行时（Win7 推荐）

```powershell
powershell -ExecutionPolicy Bypass -File tools/make_portable.ps1
```

它会下载 Python 3.8 免安装版到 `runtime/`，之后双击 `启动-便携.bat`。

---

## 二、第一次使用

1. **服务器地址一般不用手填**。程序启动时会先按顺序探内置候选地址
   （默认 `http://10.42.125.67:5000`，TCP 探活，连不上就跳过），连上就直接用，
   启动横幅里会显示「地址来源：内置候选地址（已连通）」。都连不上才需要你手输，
   也可以把网址粘进项目目录下的 `刷题网址.txt`（或 `url.txt` / `地址.txt`），
   或用 `--base` 指定——优先级是 参数 > 网址 txt > 内置候选。
2. 双击 `启动.bat`（或用 exe）→ 菜单选 `1`（单账号）或 `2`（多账号多开）。
3. 输入账号、密码（默认 `123456`，可改 `config.json`）。
4. 在试卷列表里选一套：输入序号；输入 **`A` = 全部积分**。
5. 剩下的交给它。

多账号：把账号写进 `accounts.txt`（参考 `accounts.example.txt`），菜单选 `2` 自动读取；
每个账号会在自己的控制台窗口里独立跑。

---

## 三、「全部积分」模式（重点）

试卷列表里每套卷会显示：

```text
  3. 2024年A卷  id=7  20题  满分50  积分1/3  还可计分2次
```

- `积分1/3` = 这套卷已经计了 1 次积分，上限 3 次；
- `还可计分2次` = 还能再刷 2 次拿积分。

在选卷那一步输入 **`A`**（或启动时加 `--all-points`），程序会：

1. 列出所有**还能计分的卷**（跳过积分已满的、模考入口关闭的，默认跳过正式考试卷）；
2. 每套卷按剩余次数展开，例如 `2024年A卷 × 2 次`、`2024年B卷 × 1 次`；
3. 依次全部刷完，每一轮开始前都会**重新查一次最新积分次数**，
   中途被别处刷掉了也不会白跑（次数用光自动跳过）；
4. 最后给出一份合并成绩（总题数、总积分、每轮明细）：控制台直接打印；加 `--keep-files` 时
   同时写进 `status/result-acc-*.json`，否则只落在本次运行的临时目录里、退出即删。

命令行等价写法：

```bat
python run_autoexam.py --account 学号 --all-points
```

> 想只刷一套卷的多次机会？直接输序号即可——单卷模式也会按积分逻辑正常计分，
> 只是不会自动换下一套。

---

## 四、常用参数

```bat
rem 单账号，直接指定试卷，不弹菜单
python run_autoexam.py --account 学号 --paper 2

rem 多账号，每个账号一个独立窗口
python run_autoexam.py --accounts "学号1,学号2,学号3"

rem 全部积分：所有还能计分的卷一次刷完
python run_autoexam.py --account 学号 --all-points

rem 只验证流程、不交卷（安全测试）
python run_autoexam.py --account 学号 --paper 2 --no-submit

rem 调整节奏（默认每题 7~13 秒）
python run_autoexam.py --account 学号 --paper 2 --delay-min 10 --delay-max 20

rem 登录页搞不定时，手工贴 Cookie
python run_autoexam.py --account 学号 --cookie "SESSION=xxxxx; token=yyyyy"

rem 先离线自测（本地靶场）
python mock/server.py --port 8420
python run_autoexam.py --base http://127.0.0.1:8420 --account 学号 --paper 1 --delay-min 0.1 --delay-max 0.2
```

完整参数：`python run_autoexam.py --help`

| 参数 | 作用 |
|---|---|
| `--all-points` | 全部积分模式：所有还能计分的卷 + 剩余次数一次刷完 |
| `--mode exam/practice` | 模考（默认）/ 练习模式（每题立刻判对错） |
| `--no-fresh` | 不勾「重新开始」，接着上次的进度 |
| `--no-points` | 不勾「计入个人积分」 |
| `--no-submit` | 做完不交卷 |
| `--strategy batch` | 先全探针再一次回填，快但节奏不像人 |
| `--allow-formal` | 允许刷正式考试卷（默认拒绝） |
| `--no-cache` | 不用本地答案缓存 |
| `--save-raw` | 把请求/响应原始内容落到 `logs/raw`（默认关，排查接口时才开） |
| `--keep-files` | 把日志、状态、结果、答案缓存写到程序目录（默认写系统临时目录、退出即删） |

---

## 五、目录说明

```text
启动.bat / 启动-便携.bat   双击入口
run_autoexam.py            入口脚本
config.json                默认配置（改这里比记参数方便）
accounts.example.txt       账号清单示例（复制成 accounts.txt 用）

autoexam/                  主程序
  __main__.py              菜单与命令行入口
  supervisor.py            登录/选卷/全部积分计划/多开调度/汇总
  worker.py                单个账号的工作进程（支持多卷多轮）
  solver.py                刷题状态机（探针→错题本→回填→交卷）
  login.py                 主系统登录（表单自动探测 + Cookie 兜底）
  api.py                   考试中心接口封装
  netio.py                 HTTP 层（Cookie、重试、可选原始响应落盘）
  matcher.py               题干归一化、错题本匹配、答案缓存
  privacy.py               账号匿名化与日志脱敏
  ui.py                    终端输出与日志
  config.py                配置与参数解析

mock/                      本地靶场（离线验收用，不是真实服务器）
tools/                     自检、便携运行时、打包脚本
tests/                     自动化测试
logs/                      运行日志（文件名带匿名 id）—— 默认不在这里，见下
status/                    多开时的进度/结果文件 —— 默认不在这里
answer_cache.json          答案缓存 —— 默认不在这里
```

**默认零落盘**：日志、状态、结果、答案缓存全部写到系统临时目录
`%TEMP%\autoexam-run-<随机>\`，程序退出时自动删除；程序目录和当前目录不会多出任何文件。
想看/保留这些文件时加 `--keep-files`，就会落回上面的 `logs/`、`status/`、`answer_cache.json`。

---

## 六、隐私：默认不落盘

两层意思，按重要性排：

**① 默认不在磁盘上留任何文件。** 日志、状态、结果、答案缓存全部写进系统临时目录
`%TEMP%\autoexam-run-<随机>\`，程序退出时自动删除。程序目录和当前目录不会多出
`logs/`、`status/`、`answer_cache.json`。需要留档排查时加 `--keep-files`。

**② 万一落盘（`--keep-files` 或 `--save-raw`），也不会带出个人信息：**

- 日志、状态、结果文件的**文件名**用不可逆匿名 id：`logs/acc-3d3bf00b-20261120-211208.log`；
- 写进日志**文件**的账号、姓名、密码会替换成 `***`；控制台照常显示完整账号，方便你看；
- 主进程写给子进程的任务文件 `status/job-acc-*.json` **不含账号和密码**，
  账号密码通过环境变量 `AUTOEXAM_ACCOUNT` / `AUTOEXAM_PASSWORD` 传递；
- 默认**不保存**任何接口响应、登录页快照（响应里会带姓名、成绩、Cookie）；
  确实要排查接口时用 `--save-raw` 显式打开，且文件名同样匿名；
- 本地唯一的持久化是 `answer_cache.json`（只有题干与答案，没有账号信息）。

自动化测试里有一条专门的用例：跑完整套「全部积分」之后，遍历临时目录的每个文件，
断言里面找不到账号、密码、姓名。

---

## 七、常见问题

| 现象 | 处理 |
|---|---|
| 登录失败 | 程序会把失败诊断写进日志。用浏览器手工登录后，F12 → Network → 复制 Cookie，用 `--cookie "..."` 重跑 |
| 提示「连不上服务器」 | 检查是否在校内网 / 地址对不对 |
| 某几题没拿到标准答案 | 见结果里的 `unresolved`（控制台会提示有几题）。加 `--keep-files` 才会落成 `status/result-*.json`；通常这题本来就不在错题本（探针已经蒙对了） |
| 日志/结果文件在哪 | 默认在 `%TEMP%\autoexam-run-<随机>\`，横幅里的「日志目录」就是本次的真实路径；进程退出时会自动删除，要看残留就加 `--keep-files` |
| 提示「账号看起来已经有实例在跑」 | 对应 `.lock` 文件在本次运行的状态目录里（见上一条），确认没在跑后删掉即可 |
| 中文乱码 | 用 `启动.bat` 启动（会 `chcp 65001`）；或执行前先 `chcp 65001` |
| **双击 exe 没窗口 / 一闪就没** | 多半是把 `autoexam.exe` 从目录里单独拷出来了——目录版必须**整个目录**一起拷。只想要一个文件，就用 `autoexam-single.exe` |
| 单文件版首次启动慢 | 正常：onefile 启动时要解压到 `%TEMP%`，之后有系统缓存会快一些 |
| 想换个节奏 | 改 `config.json` 里的 `delay_min` / `delay_max` |
| 全部积分模式下有卷被跳过 | 积分已满（3/3）、模考入口关闭，或它是正式考试卷（需要 `--allow-formal`） |

---

## 八、开发者

```bat
python -m unittest discover -s tests -v     rem 68 个测试
python tools/check_compat.py                rem 3.7 语法兼容检查
python tools/smoke.py                       rem 靶场契约自检
python tools/check_offline.py               rem 离线依赖验证（只连本机）
python tools/verify_all.py                  rem 以上全跑
```

打包 exe（**必须在 Python 3.8 上打包**，否则 exe 回 Win7 会缺少运行时 API）：

```powershell
py -3.8 -m pip install pyinstaller==5.13.2
powershell -ExecutionPolicy Bypass -File tools/build_exe.ps1
```

产物是 `dist/autoexam/` 整个目录，压缩后可直接发给没装 Python 的电脑。
**注意这是 onedir：`autoexam.exe` 离了同目录的 `python38.dll` 等文件就起不来。**

要打成「只有一个 exe 文件」的版本：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_single.ps1
```

产物是 `dist/autoexam-single.exe`（onefile，约 6.2 MB，**Win7/10/11 通用**）。

脚本会**优先用 `runtime\python.exe`**（Python 3.8.10 + PyInstaller 5.13.2，通吃三个系统）；
找不到它才回退到系统 Python，那时产物只能跑 Win10/11。补齐 3.8 运行时：

```powershell
powershell -ExecutionPolicy Bypass -File tools/make_portable.ps1
```

它会把 3.8.10 下载到 `runtime\`，打包脚本不用改任何东西。给 onedir 版（`build_exe.ps1`）
指定同一个 3.8 解释器：`$env:PYTHON38 = "<项目>\runtime\python.exe"`。

---

## 九、协议与免责

- 许可证：AGPL-3.0（见 `LICENSE`）。
- 程序会把成绩**真实**提交到系统：会计入个人积分、教师端统计和作答记录。
- 探针答案会短暂出现在你的错题本和作答记录里（这是拿答案的手段本身决定的）。
- 默认**不会**碰正式考试卷（`formal_exam`），要刷必须显式加 `--allow-formal`。
- 这是自动化脚本，是否违反学校/平台的使用规定由你自己判断和承担。

<details>
<summary>开发备注</summary>

项目起于高二备考季：那时要准备通用技术学业水平考试，考试中心的积分按「每套卷最多计 3 次」
设计，一套一套点太慢，于是写了这个工具，顺便把培正中学通用技术学考的整套练习卷都刷满了。
接口行为、积分计次规则、错题本回填时机，都是对着那套环境一点点核出来的；
后来把账号相关的实现全部改成匿名落盘，才整理成现在这个通用版本。

</details>
