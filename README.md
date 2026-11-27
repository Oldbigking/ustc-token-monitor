# USTC Token 监测器

监测你在 **DSH 里真实的模型调用**：**平均生成速度（tok/s）**、**首 token 延迟**、
**1 小时 / 全天用量与费用**、**历史每日速度/延迟**、**3h / 12h 限额**（API 模式）。

默认数据源是**本机 DSH 会话日志**（`~/.dsh/sessions/**/session.v4.jsonl.zstd`）：
**零 API key、零网络请求、不产生任何计费**——装好就能用。
要跟网关账单对账时再加 `--source api`（读平台 `/spend/logs/v2`）或用 `--source both`。

两种用法：

1. **本地看板** `http://127.0.0.1:8790`
2. **DSH 插件**：聊天界面头部的状态条 `⚡ 70.0 tok/s · 5m 2.3M`，点击唤起桌面浮窗（Shift/右键 打开看板）

---

## 0. 前置条件

**默认的 `--source local` 只要 Python 3.9+ 和一个 DSH 会话目录**，不需要 Key，
也不用手工装依赖（Python < 3.14 读 `.jsonl.zstd` 需要一个解压库，见下）。

```powershell
pip install zstandard        # 只在 Python 3.9–3.13 需要；3.14+ 已内置 compression.zstd
```

装不上也不影响启动：`--source api` 走网关账单，完全不碰 zstd；
`python monitor.py --once` 会在 stderr 打出可操作的提示而不是 traceback。

### 只有 `--source api` 才需要 Key

`api` / `both` 模式读的是你在 USTC 平台上的账单，**没有 Key 会直接退出（码 2）**。

1. 打开平台门户 <https://llm.ustc.edu.cn/>，登录 → 「API 密钥」→ 创建 → 复制 `sk-...`
2. 三选一配置（优先级从上到下）：

   ```powershell
   $env:USTC_API_KEY = 'sk-...'        # 当前窗口有效，最简单
   setx USTC_API_KEY "sk-..."          # 永久，需新开窗口
   ```

   或写进 `~/.dsh/.credentials.yaml`（DSH 用户通常已经在那里）：

   ```yaml
   USTC_API_KEY: sk-...
   ```

3. 依赖：Python 3.9+；`api` 模式仅标准库，`local` 模式另需 `zstandard`（3.14+ 免装）。

配错时的确切行为（照此判断，不用猜）：

| 情况 | 现象 | 退出码 |
|---|---|---|
| `local` 模式找不到会话日志 | 提示 0 个文件，看板照常打开但全 0 | `--once` 退出 `0` |
| `local` 模式缺 zstandard | stderr 给出 `pip install zstandard` 提示，不动库 | `--once` 退出 `1` |
| `api` 模式完全没配 Key | stderr 打出提示，不建库、不起服务 | `2` |
| Key 无效（401） | 服务照常起来，看板顶部横幅 `采集异常：平台返回 401（密钥无效）`，数字全 0 | `--once` 退出 `1`；`--serve` 保持运行等修好 |
| 端口被占用 | 提示换个端口后退出 | `2` |
| 平台不可达 / 5xx 重试耗尽 | 看板横幅标红，人话错误 | `--once` 退出 `1` |

## 1. 获取源码

```powershell
git clone https://github.com/Oldbigking/ustc-token-monitor.git
cd ustc-token-monitor
```

没有 git：打开仓库页面 → **Code → Download ZIP** → 解压到任意目录即可。

## 2. 快速开始

```bat
start.bat                        :: 启动看板（Ctrl+C 停止；参数会透传给 monitor.py）
start.bat --port 9000            :: 换个端口
python monitor.py --once         :: 采集一轮并打印摘要（默认读本机日志，零密钥）
python monitor.py --source api --once     :: 换成读网关账单
python monitor.py --source both           :: 两个都采：账单用于展示，日志用于对账
python monitor.py --backfill     :: 回填历史账单（只有 api 模式有这概念）
python monitor.py --backfill --since 2026-11-01
```

启动后：

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8790/          # 200
curl.exe -s http://127.0.0.1:8790/api/summary | ConvertFrom-Json | Select last_err
```

**只看改 `web/` 的前端改动**：服务端按 mtime 读取页面，刷新浏览器即生效，不用重启进程。

> 端口：`--port`（默认 8790）、`--host`（默认 `127.0.0.1`，只监听本机）。
> 插件宿主默认也连 8790；换端口时给 DSH 设 `USTC_MONITOR_PORT=9000` 即可，
> 无需改插件代码（浏览器的直连回退仍写死 8790，但正常路径走同源代理）。

## 3. 停止 / 更新 / 卸载

- **停止**：前台 Ctrl+C；后台或自启的用 `taskkill /IM pythonw.exe /F`
  （或任务管理器结束 `pythonw.exe`），再删开机启动项。
- **更新**：`git pull`（或用新 ZIP 覆盖），重启 `--serve` 进程；只改前端则刷新浏览器。
- **卸载**：结束进程 → 删除整个目录 → 如需彻底清理，删掉
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\USTC-Token-Monitor.vbs`；
  装了插件再执行 `install-plugin.ps1 -Uninstall`。
- **数据**：历史在目录下的 `usage.db`（SQLite），删掉即清空，重跑 `--backfill` 可重建。

## 指标口径

| 指标 | 定义 |
|---|---|
| 平均生成速度 | `Σ completion_tokens ÷ Σ (endTime − completionStartTime)`，窗口内所有请求的加权平均 tok/s |
| 插件显示速度 | 最近一次有调用的 **5 分钟窗口**（`m5a`）：有活动时是滚动 5 分钟；空闲超过 5 分钟则回退到最后活跃的那段，并在状态条上标注 `Nm 前` |
| 首 token 延迟 | `completionStartTime − startTime` 的平均值 |
| 近 1 小时用量 | `startTime` 落在最近 3600s 内的请求 |
| 3h / 12h 限额 | 平台 `/key/info` 的 `budget_limits` + `budget_limits_usage`（权威值，带重置倒计时） |
| 全天用量 / 费用 | 北京时间 00:00 起至现在 |
| 历史总用量 | 全部已入库请求的 token 合计（含费用、天数、次数），按当前来源筛选后计算 |
| 历史 | 按本地日聚合全部已入库请求，给出每日平均 tok/s 与平均 TTFT |
| 来源 | `<provider>/<model>`（local 从日志的 `message.source` 取，api 从账单 `model_group` 取） |
| 费用（local） | `未缓存 × 单价 + (缓存读 + 缓存写) × 缓存单价 + 输出 × 输出单价`，单价见 `prices.json`；**是估算，不是账单真值**，看板上带 `≈` 前缀 |

`completion_tokens` 包含推理 token；输入里很大比例命中缓存（通常 95%+），
卡片刻意把缓存命中率单独展示。

**local 口径的两个已知差异**（不是 bug，别去"修"）：

1. **首 token 延迟比账单口径高约 470ms**（实测 1,149ms vs 680ms）：本地只有
   `step/start` 到首个 stream chunk 的时间戳，中间含 harness 装配、工具结果处理等
   开销；**tok/s 不受影响**（52.47 vs 52.70），因为它是"首个 chunk → 落盘"的纯生成区间。
2. **请求数与账单不完全相等**：账单里有约 110 条零 token 行（中断/失败请求），
   还有 17 条 11-03 之前的历史（那时 DSH 还没开始记录用量）；另外同 key 的其它
   客户端调用账单里也有、本地日志里没有。合计 token 偏差 **0.3%** 以内、
   金额偏差 **1.1%** 以内（都是本地偏少），逐日偏差见 `/api/reconcile`。

## 数据来源

`--source` 三档，默认 `local`：

| 模式 | 读什么 | 需要 Key | 金额 |
|---|---|---|---|
| `local`（默认） | 本机 DSH 会话日志 | 否 | 按 `prices.json` 单价估算（`≈`） |
| `api` | 平台账单接口 `/spend/logs/v2` | 是 | 账单真值 |
| `both` | 两个都采 | 是 | 展示用账单，日志用于对账 |

### local：本机 DSH 会话日志（默认，零密钥）

`~/.dsh/sessions/<工作区>/session-<uuid>/session.v4.jsonl.zstd`，一行一个 JSON 事件。
**只有两种事件带用量**：`assistant/message` 与 `compaction/summary`，都在 `data.usage` 里：

```json
{"inputTokens": 1100, "outputTokens": 1466, "totalTokens": 11334,
 "cacheReadTokens": 8768, "cacheWriteTokens": 0}
```

口径与几个坑（改 `sources/dsh_log.py` 前先看这里）：

- **`prompt = inputTokens + cacheReadTokens + cacheWriteTokens`**（`totalTokens` 是这四项之和，
  直接拿它当 prompt 会把输出算两遍）。
- **`assistant/attempt` 一律不用**：那是重试事件，`usage` 恒为 0，混进来会拉低均值。
- **fork / 续接会话会复制父会话的事件**，必须按 `(类型, time_ms, turn, step)` 去重，
  且先按会话 `createdAt` 升序处理（父先占位）。不去重会虚高 1.5 倍以上。
- `t0` 取 `step/start` 的时间（按 `(turn, step)` 配对），`t1` 取首个 stream chunk，
  `t2` 取事件时间；三者错位会让 TTFT / tok/s 变成负数或无穷大。
- 增量缓存按 `(size, mtime_ns)` 判定，落在 `%LOCALAPPDATA%\ustc-token-monitor\dsh-usage-cache.json`
  （可用 `USTC_TOKEN_CACHE` 覆盖）；**不要用字节偏移当游标**——zstd 是多帧追加流。

可覆盖的环境变量（换机器 / 非标准目录时用，不必改代码）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `USTC_TOKEN_DSH_ROOT` | 会话日志根目录 | `~/.dsh/sessions` |
| `USTC_TOKEN_CACHE` | 增量缓存文件 | `%LOCALAPPDATA%\ustc-token-monitor\dsh-usage-cache.json` |
| `USTC_TOKEN_PRICES` | 价目表 | 仓库根的 `prices.json` |
| `USTC_TOKEN_SOURCE` | 数据源 | `local` |

### api：平台账单（需要 Key）

- `GET /spend/logs/v2?start_date=&end_date=&page=&page_size=` — 逐请求账单
  （`startTime / completionStartTime / endTime / prompt_tokens / completion_tokens / cached_tokens / spend / model`）
- `GET /key/info` — 预算窗口、重置时间、rpm 限额

采集策略：启动时增量拉第 1 页（遇到已知 `request_id` 即停），每 10s 一轮，
每 30 分钟全量对齐一次；看板与客户端状态条每 5s 刷新一次；只读、单连接、失败退避。
**`local` 模式完全不走这段代码，也不读 `~/.dsh/.credentials.yaml`。**

### 两种源怎么共存（不重复计数）

`requests` 表加了两列：`provider`（`ustc` 等）和 `usage_source`（`billing` / `estimated`）。
local 写入的行是 `estimated`，api 写入的是 `billing`，`request_id` 前缀也不同
（`dsh:call|...` vs 网关的 uuid），所以两批数据可以同时留在库里互不覆盖。
`/api/summary` 默认**只取一份**（`local` → `estimated`，`api`/`both` → `billing`），
不会把同一次调用算两遍。

### 对账接口

```powershell
curl.exe -s "http://127.0.0.1:8790/api/reconcile?days=30"     # 逐日 + 合计的偏差
python tools\compare.py --days 30                            # 同一套逻辑的命令行版
```

返回每天的 `local`（日志估算）/ `bill`（账单）与偏差，外加 `total`、`notes`（已知对不上的项）、
`unbilled`（日志里有、但没走这个网关的 provider）。验收阈值：合计 token ≤1%、
金额 ≤2%、tok/s ≤2%。

> **当天的偏差会偏大**：`local` 模式不再访问网关，账单侧停在"最后一次 `--source api`"
> 的时刻，所以今天那行会比本地少最后几分钟的量。要看齐就用
> `--source both`，或偶尔跑一次 `--source api --once`。
>
> **金额差的来源可以精确算出来**：单价是 输入 2 / 缓存命中 0.2 / 输出 2（元/1M tokens，
> 见 `prices.json`），本地日志只覆盖 DSH 自己的调用，同 key 的其它客户端/工具调用
> 账单里有、日志里没有，而且这些请求基本都没命中缓存——所以
> **金额差 ≈ 缺失的未缓存 token × 2 元/1M**。实测 11-20～11-26 的逐日差额
> （-0.13 / +0.08 / -0.44 / -0.87 / +0.01 / -1.00 / -0.38 元）与这个预测
> （+0.12 / 0 / +0.50 / +0.98 / 0 / +0.90 / +0.38 元）逐项吻合；
> 请求数完全对上的两天（11-21、11-24）误差是 +0.16% 和 +0.06%。
> 近 30 天合计：token +0.3%、金额 -1.1%（阈值 1% / 2%）。

账单拉取遇到平台偶发 5xx / 429 / 网络抖动时，**同一轮内最多重试 3 次**
（间隔 0 / 1.5s / 4s），三次都失败才在看板标红，并把错误翻成人话
（`平台返回 503（平台暂时不可用）` `请求超时` `网络不可达：…`），不会把裸 traceback
甩在状态栏上。`/key/info` 不参与重试（失败只影响限额卡片，下次刷新再试）。
离线用例：`python tests/test-retry.py`。

> **代理坑**：Windows 的 WinINET 系统代理（注册表 `Internet Settings`）会被
> Python `urllib` 自动继承，而 `curl` / DSH 只认环境变量。开着 Clash 之类的
> 系统代理时，会出现 `curl` 能通、采集却报 `WinError 10054 连接被重置` 的怪象。
> 采集默认**绕过系统代理**直连；确实要代理时设 `USTC_PROXY=http://127.0.0.1:7890`。

## 看板图表

三张图都是**按容器像素对齐的 SVG**（`viewBox` = 实际宽高，文字不变形、不缩放），
带 y 轴刻度与虚线网格、自适应 x 轴时间标签：

- 输出 token 与平均 tok/s（**双轴合一张图**：柱＝输出 token 走左轴，折线＝tok/s 走右轴）。
  tok/s 折线**只在折轴断点处断开**（跨过空档不连）；断点之间属于同一连续时段，中间有没有
  记录的格子时，跨过去的那一段画**虚线且不填充面积**（一眼看出这段是空的），两侧有记录的
  格子仍是实线 + 淡填充。**时间轴会折轴**：
  连续 **15 格**（含）以上没有请求就压成一个斜杠断点（占 2 个格子宽，下方标 `2h13m` 这类
  时长，悬停给出精确区间），短空档照常留着，所以「上次干活 → 空了多久 → 现在」
  在同一根轴上一眼看全，且不会把几小时空白摊成几十个空格子。阈值固定按格数算，
  换算成时间是 15 × 细度：30s→7.5 分钟、1m→15 分钟、5m→1h15m、15m→3h45m、
  30m→7h30m、1h→15 小时——细度越粗，"值得折起来"的空档也越长，不用再单独调。
- **细度可自选**：右上角的下拉框切换 30 秒 / 1 分钟 / 5 分钟 / 15 分钟 / 30 分钟 /
  1 小时，窗口固定 60 格，所以细度直接决定看多久——30s≈近 30 分钟、1m≈近 1 小时、
  5m≈近 5 小时、1h≈近 60 小时。参数为 `?bucket=30|60|300|900|1800|3600`，
  同样记在 localStorage 并可分享链接。
- 纵轴刻度按「数据最大值 / 4」取最近的整步长（1 / 1.2 / 1.5 / 2 / 2.5 / 3 / 4 / 5 / 6 / 8 / 10
  ×10ⁿ），上限最多比数据高 20%：117.7 tok/s 会落到 120 而不是 200，柱子基本顶到图顶。
- 每日用量（整行宽柱状，柱顶标 token 总量）
- 每日平均 tok/s 与首 token 延迟（两条折线**分轴**：tok/s 走左轴，首 token 秒数
  走右轴，各自独立刻度；虚线是 tok/s 历史均值）。刻意不与用量柱状合图——三套
  量纲挤一张图会看不清，拆成两张后两边的纵轴都能用满高度。
- 这两张每日图**横轴逐列对齐**（共用 `pad` 留白与同一份列宽），悬停任意一张的
  某一天，两张同时高亮该列，提示框一次给出当天的 token / tok/s / 首 token /
  请求数 / 费用——不用来回两张图对日期。

鼠标悬停任意柱子/数据点会给出精确数值与当时的请求明细（双轴图一次给出两个数）。
窗口小于 980px 时自动从两列变一列。

## 桌面悬浮小窗（不用开 DSH，也不用开浏览器）

`widget.py` 是一块**无边框、始终置顶**的小卡片，用 Python 自带的 tkinter 画出来
（不依赖任何第三方库），贴在工作区角落，干别的事时瞟一眼就行：

```
┌─────────────────────────────────────────┐
│                         看板 ↗      ✕   │
│ ▍23.8 tok/s · 2m 前            5m 6k    │
│ 1h 3.5M · 23 次              首包 2.0s  │
│ 3h  ▓▓▓░░░░░░░░░░░░░░░░░░░  1.2/30      │
│ 12h ▓▓▓▓░░░░░░░░░░░░░░░░░░  4.2/70      │
│ 13:59:29 · 6s 前      全天 13.4M ¥3.65  │
└─────────────────────────────────────────┘
```

| 操作 | 效果 |
|---|---|
| 拖动 | 移位，位置记在配置里，下次启动原地出现 |
| 右上 `✕` | 关闭（hover 变红） |
| 右上 `看板 ↗` | 打开完整看板（等价于双击卡片）；**已有看板窗口就直接抬到最前并最大化**，不会每点一次多开一个标签页 |
| 右键 | 自绘圆角菜单：打开看板 / 迷你模式（200×62）/ 始终置顶 / 退出 |
| Ctrl + 滚轮 | 调透明度（0.35–1.0） |
| 采集失败 | 左上圆点变红，底部给出人话错误（如 `⚠ 平台返回 503（平台暂时不可用）`） |
| 空闲 | 速度后面标 `· 7m 前`，速度显示最后一次活跃的 5 分钟窗口 |

字体按优先级挑本机存在的：显示体 `Segoe UI Variable Display → Segoe UI Semibold`，
正文 `Segoe UI Variable Text → Segoe UI`（中文由 GDI 字体链接补，不用单独指定）。
字号全用像素单位并跟着 `S()` 缩放，高 DPI 下不糊也不撑破卡片。

启动（需要监测器在跑，小窗只是 `/api/summary` 的只读客户端，不额外请求平台）：

```bat
start-widget.vbs           :: 无控制台启动（推荐，双击即可）
pythonw widget.py          :: 等价写法
python  widget.py          :: 带控制台，便于看报错
```

**配色**：默认白底。看板顶部有一排「浮窗」色块，点一下就把底色换成对应主题
（白 / 米白 / 薄荷 / 天蓝 / 淡紫 / 浅灰 / 深色），小窗最多 5 秒内跟着变，不用重启：

```
GET  /api/summary?bucket=300      -> 折轴图细度（秒），窗口固定 60 格
POST /api/dashboard/open          -> 打开看板：已有窗口就恢复/最大化并抢到前台，否则新开
GET  /api/widget                  -> {theme, colors, themes}
POST /api/widget?theme=mint       -> 写入 %APPDATA%\ustc-token-monitor\widget.json
```

颜色是 monitor 写进配置文件的（`x/y/mini/alpha` 原样保留），小窗只读不写，
所以看板和小窗同时开着也不会互相覆盖。想加新配色只需要在 `monitor.py` 的
`WIDGET_THEMES` 里加一项，看板色块会自动多一个。

- 位置 / 迷你模式 / 透明度 / 配色存 `%APPDATA%\ustc-token-monitor\widget.json`
- 换端口：`python widget.py --api http://127.0.0.1:9000/api/summary`
- **开机自启**：把 `start-widget.vbs` 复制到
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`（`pythonw` 不在 PATH 时，
  按 vbs 里的注释改成 `pythonw.exe` 全路径）
- **高 DPI**：按屏幕缩放整块等比放大（96 DPI = 1.0），字不会糊也不会撑破卡片
- 自检：`python widget.py --selftest`（16 项布局/数值断言），
  `python widget.py --dump` 打印当前真实数据的每个文本块与坐标

## 主题与来源

- **明/暗双主题**：右上角按钮在 `自动 → 浅色 → 深色` 之间切换，选择存进
  `localStorage`；默认跟随系统。图表配色全部取自 CSS 变量，换主题会立即重绘。
  也可以用 URL 强制：`http://127.0.0.1:8790/?theme=light`。
- **来源筛选**：顶部下拉按 `model_group` 过滤（总计 / 各模型组），切换后卡片、
  图表、最近请求全部变成该来源的数据；限额卡片是 key 级的，不随筛选变化。
  来源列表由全部历史聚合而来，所以即使筛选后也能切回去。
  API 侧对应 `GET /api/summary?source=<model_group>`。

页面在 `web/index.html`，服务端按 mtime 读取，**改完刷新浏览器即生效，不用重启进程**。

## 柱顶数值的显示规则

柱子顶到纵轴天花板时（例如某天用量远超其余各天），数字放不进上方空白，
这时会自动**压进柱子内部**，并按柱子颜色亮度挑黑字或白字（深色主题的浅蓝柱配
深字、浅色主题的深蓝柱配白字），保证任何配色下都看得清；其余情况仍显示在柱顶。

纵轴刻度按最宽标签动态留左边距；刻度值、均值标注、柱顶数字、x 轴标签之间会
互相避让，放不下就跳过而不是叠在一起；均值标注固定在左端，避免和最新的柱子抢位置。

## 图表自检

`web/_selftest.html` 是浏览器内的回归用例集（**30 个场景**：空数据、单点、全零、
超大值、NaN/Infinity/负数、60 天抽稀、窄容器、明暗双主题、均值高于所有柱、
折轴断点、双轴、两图联动…），用真实 `getBBox()` 检查文字出界/重叠、矩形越界，
并在 `document.title` 里写结论。

看板自己就会提供这一页，部署完直接打开：

```
python monitor.py --serve
# 浏览器打开 http://127.0.0.1:8790/_selftest.html
```

命令行（headless Edge，取出 `<pre id="out">` 里的报告）：

```
msedge --headless=new --virtual-time-budget=20000 --dump-dom http://127.0.0.1:8790/_selftest.html
```

## DSH 插件

```
plugin/                     插件源码
  package.json              dsh.client 声明
  lib/index.js              宿主半：GET /ustc-token/summary + POST /ustc-token/widget/open 代理
  lib/client.js             浏览器半：聊天头部状态条（每 5s 刷新；点击唤起浮窗）
install-plugin.ps1          安装/更新/卸载到 DSH profile
```

- 安装：`powershell -ExecutionPolicy Bypass -File install-plugin.ps1`
- 卸载：`powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Uninstall`
  （会删插件目录，并把 `cordis.patch.yml` 里整个 `ustc-token-badge` 的
  `- insert:` 块一并移除；**不要手工只删 `- id:` 一行**，那会留下空的 `insert:` 让 YAML 失效）
- profile 补丁热加载，状态条则要刷新一次窗口（`Ctrl+R`）才出现。
- 脚本默认操作 `~/.dsh/profiles/desktop`，可用 `-Profile <名字>` / `-ProfileRoot <目录>` 指定。
  profile 不存在时脚本会直接报错，不会留半装状态。
- 新条目必须写成 `- insert:` 形式——直接写 `- id: <新条目>` 会被当成"覆盖已有条目"，
  目标不存在时整条被丢弃。
- **点击状态条 = 唤起桌面浮窗**：`POST /ustc-token/widget/open` 交给监测器——已经在跑
  就 `SetForegroundWindow` 拎到最前，没跑就用 `pythonw widget.py` 拉起来（3 秒防抖，
  连点不会开一堆）。浮窗拉不起来时自动退回打开看板；`Shift/Alt/Ctrl + 点击`、右键、
  中键直接打开看板（同样是先 `POST /api/dashboard/open`，已有窗口就抬前台）。
  宿主路由是新增的，**改完要重启 DSH**；重启前客户端会直连
  `127.0.0.1:8790/api/widget/open` 兜底。
- 数据通道优先走同源代理 `/ustc-token/summary`，失败再直连 `127.0.0.1:8790`。
  看板换端口：给 DSH 设 `USTC_MONITOR_PORT=9000`（宿主代理会跟过去）。
- 若插件导致启动异常：`install-plugin.ps1 -Uninstall` 后重启 DSH 即可恢复。

## 开机自启（可选，仓库不含该文件）

监测器的自启脚本仓库里**没有**（自己建一个）；浮窗的自启脚本仓库已经带了：`start-widget.vbs`。

监测器（自己建一个）：

`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\USTC-Token-Monitor.vbs`

```vbscript
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\path\to\ustc-token-monitor"
sh.Run """pythonw.exe"" monitor.py --serve", 0, False
```

用 `pythonw.exe` 无窗口启动（`pythonw` 不在 PATH 时写全路径）。删除该文件即可取消自启。
注意自启进程没有窗口，出错只能靠看板横幅或 `usage.db` 是否增长判断。

## 文件

```
monitor.py            采集 + 统计 + JSON API（单文件，仅标准库）
web/index.html        看板前端（明/暗主题、来源筛选、SVG 图表）
web/_selftest.html    浏览器内图表回归用例（30 个场景，由看板直接提供）
usage.db              SQLite 历史库（`requests` 表，`request_id` 主键，幂等写入）
plugin/               DSH 插件源码
install-plugin.ps1    插件安装/卸载
start.bat             手动启动看板
widget.py             桌面悬浮小窗（tkinter，仅标准库）
start-widget.vbs      无控制台启动小窗（也可复制到启动目录做自启）
tests/                离线用例：test-retry.py（重试逻辑）、test-host.mjs（宿主路由）
```

## 注意

- Key 从环境变量 `USTC_API_KEY` 读取，缺失时回退 `~/.dsh/.credentials.yaml`，
  只在内存中持有，日志里掩码显示为 `sk-...abcd`。
- 看板只监听 `127.0.0.1`（`--host` 可改）；`/api/summary` 允许任意来源读取（仅本机可访问）。
- 平台账单接口的 `start_date/end_date` 按 UTC 解释，脚本已按本地日重新过滤。
- 限额卡片用平台 `current_spend`（实际扣减值），**每 60s** 批量更新一次，会阶梯式跳变。
- 监测器不调用 `/v1/chat/completions`，不占用 `rpm_limit=20` 的对话配额。
