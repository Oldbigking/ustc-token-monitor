# USTC Token 监测器

监测 `https://api.llm.ustc.edu.cn`（大模型公共服务平台的 LiteLLM 网关）上的
**平均生成速度（tok/s）**、**首 token 延迟**、**1 小时用量**、**3h / 12h 限额**、
**全天用量与费用**、**历史每日速度/延迟**。
数据全部来自平台自己的账单接口，**不消耗任何 token**。

两种用法：

1. **本地看板** `http://127.0.0.1:8790`
2. **DSH 插件**：聊天界面头部的状态条 `⚡ 70.0 tok/s · 5m 2.3M`，点击打开看板

---

## 0. 前置条件：一个 USTC 平台的 API Key

监测器读的是你自己的账单，**没有 Key 什么都跑不起来**（会直接退出，退出码 2）。

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

3. 依赖：**Python 3.9+，仅标准库**，不需要 `pip install`。

配错时的确切行为（照此判断，不用猜）：

| 情况 | 现象 | 退出码 |
|---|---|---|
| 完全没配 Key | stderr 打出提示，不建库、不起服务 | `2` |
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
python monitor.py --once         :: 采集一轮并打印摘要（失败退出码 1）
python monitor.py --backfill     :: 回填历史（默认从平台开通日 2026-10-29 起）
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
| 来源 | 平台账单的 `model_group`（老数据没有该字段时退回 `model`） |

`completion_tokens` 包含推理 token；输入里很大比例命中缓存（通常 95%+），
卡片刻意把缓存命中率单独展示。

## 数据来源

- `GET /spend/logs/v2?start_date=&end_date=&page=&page_size=` — 逐请求账单
  （`startTime / completionStartTime / endTime / prompt_tokens / completion_tokens / cached_tokens / spend / model`）
- `GET /key/info` — 预算窗口、重置时间、rpm 限额

采集策略：启动时增量拉第 1 页（遇到已知 `request_id` 即停），每 10s 一轮，
每 30 分钟全量对齐一次；只读、单连接、失败退避。

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

- 每分钟输出 token 与平均 tok/s（**双轴合一张图**：柱＝输出 token 走左轴，
  折线＝tok/s 走右轴，空闲区间会断开而不是连成假趋势）。**时间轴会折轴**：
  单段空档超过 30 分钟就压成一个斜杠断点（占 2 个格子宽，下方标 `2h13m` 这类
  时长，悬停给出精确区间），短空档照常留着，所以「上次干活 → 空了多久 → 现在」
  在同一根轴上一眼看全，且不会把几小时空白摊成几十个空格子。阈值可在卡片右上角
  切换（10m / 30m，默认 30m；时间轴只覆盖约 1 小时，比 1 小时还长的阈值没有意义），
  选择记在 localStorage 并同步到 URL（`?gap=10`），可直接分享某个口径的视图。
- 每日用量（整行宽柱状，柱顶标 token 总量）
- 每日平均 tok/s 与首 token 延迟（两条折线**分轴**：tok/s 走左轴，首 token 秒数
  走右轴，各自独立刻度；虚线是 tok/s 历史均值）。刻意不与用量柱状合图——三套
  量纲挤一张图会看不清，拆成两张后两边的纵轴都能用满高度。
- 这两张每日图**横轴逐列对齐**（共用 `pad` 留白与同一份列宽），悬停任意一张的
  某一天，两张同时高亮该列，提示框一次给出当天的 token / tok/s / 首 token /
  请求数 / 费用——不用来回两张图对日期。

鼠标悬停任意柱子/数据点会给出精确数值与当时的请求明细（双轴图一次给出两个数）。
窗口小于 980px 时自动从两列变一列。

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
  lib/index.js              宿主半：注册 GET /ustc-token/summary 代理本地监测器
  lib/client.js             浏览器半：聊天头部状态条（每 10s 刷新）
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
- 数据通道优先走同源代理 `/ustc-token/summary`，失败再直连 `127.0.0.1:8790`。
  看板换端口：给 DSH 设 `USTC_MONITOR_PORT=9000`（宿主代理会跟过去）。
- 若插件导致启动异常：`install-plugin.ps1 -Uninstall` 后重启 DSH 即可恢复。

## 开机自启（可选，仓库不含该文件）

仓库里**没有** `.vbs`，也没有创建它的脚本——需要自启时自己建一个：

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
tests/                离线用例：test-retry.py（重试逻辑）、test-host.mjs（宿主路由）
```

## 注意

- Key 从环境变量 `USTC_API_KEY` 读取，缺失时回退 `~/.dsh/.credentials.yaml`，
  只在内存中持有，日志里掩码显示为 `sk-...abcd`。
- 看板只监听 `127.0.0.1`（`--host` 可改）；`/api/summary` 允许任意来源读取（仅本机可访问）。
- 平台账单接口的 `start_date/end_date` 按 UTC 解释，脚本已按本地日重新过滤。
- 限额卡片用平台 `current_spend`（实际扣减值），**每 60s** 批量更新一次，会阶梯式跳变。
- 监测器不调用 `/v1/chat/completions`，不占用 `rpm_limit=20` 的对话配额。
