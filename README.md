# USTC Token 监测器

监测 `https://api.llm.ustc.edu.cn`（大模型公共服务平台的 LiteLLM 网关）上的
**平均生成速度（tok/s）**、**首 token 延迟**、**1 小时用量**、**3h / 12h 限额**、
**全天用量与费用**、**历史每日速度/延迟**。
数据全部来自平台自己的账单接口，**不消耗任何 token**。

两种用法：

1. **本地看板** `http://127.0.0.1:8790`
2. **DSH 插件**：聊天界面头部的状态条 `⚡ 70.0 tok/s · 5m 2.3M`，点击打开看板

## 快速开始

```bat
start.bat                     :: 启动看板（默认已设为开机自启）
python monitor.py --once      :: 采集一轮并打印摘要
python monitor.py --backfill  :: 回填全部历史（一次性）
```

依赖：Python 3.9+，**仅标准库**。

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

平台偶发 5xx / 429 / 网络抖动时，**同一轮内最多重试 3 次**（间隔 0 / 1.5s / 4s），
三次都失败才在看板标红，并把错误翻成人话（`平台返回 503（平台暂时不可用）`
`请求超时` `网络不可达：…`），不会再把裸 traceback 甩在状态栏上。离线用例：
`python tests/test-retry.py`。

> **代理坑**：Windows 的 WinINET 系统代理（注册表 `Internet Settings`）会被
> Python `urllib` 自动继承，而 `curl` / DSH 只认环境变量。开着 Clash 之类的
> 系统代理时，会出现 `curl` 能通、采集却报 `WinError 10054 连接被重置` 的怪象。
> 采集默认**绕过系统代理**直连；确实要代理时设 `USTC_PROXY=http://127.0.0.1:7890`。

## 看板图表

四张图都是**按容器像素对齐的 SVG**（`viewBox` = 实际宽高，文字不变形、不缩放），
带 y 轴刻度与虚线网格、自适应 x 轴时间标签：

- 每分钟输出 token 与平均 tok/s（**双轴合一张图**：柱＝输出 token 走左轴，
  折线＝tok/s 走右轴，空闲区间会断开而不是连成假趋势）
- 每日用量（整行宽柱状，柱顶标 token 总量）
- 每日平均 tok/s / 每日平均首 token 延迟（柱顶标数值，虚线是历史均值）

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

`web/_selftest.html` 是浏览器内的回归用例集（18 个场景：空数据、单点、全零、
超大值、NaN/Infinity/负数、60 天抽稀、窄容器、明暗双主题、均值高于所有柱…），
用真实 `getBBox()` 检查文字出界/重叠、矩形越界：

```
cd web && python -m http.server 8899
# 浏览器打开 http://127.0.0.1:8899/_selftest.html 看报告
```

命令行版（headless Edge，`document.title` 里带结论）：

```
msedge --headless=new --virtual-time-budget=9000 --dump-dom http://127.0.0.1:8899/_selftest.html
```

## DSH 插件

```
plugin/                     插件源码
  package.json              dsh.client 声明
  lib/index.js              宿主半：注册 GET /ustc-token/summary 代理本地监测器
  lib/client.js             浏览器半：聊天头部状态条（每 10s 刷新）
install-plugin.ps1          安装/更新到 DSH profile
```

安装：`powershell -ExecutionPolicy Bypass -File install-plugin.ps1`。profile 补丁会热加载，
状态条则要刷新一次窗口（`Ctrl+R`）才出现。新条目必须写成 `- insert:` 形式——
直接写 `- id: <新条目>` 会被当成"覆盖已有条目"，目标不存在时整条被丢弃。
插件会出现在会话头部右侧，与"在应用中打开""定时任务"按钮同一排。

- 数据通道优先走同源代理 `/ustc-token/summary`，失败再直连 `127.0.0.1:8790`。
- 卸载：删除 `~/.dsh/profiles/desktop/node_modules/dsh-plugin-ustc-token`
  以及 `~/.dsh/profiles/desktop/cordis.patch.yml` 末尾的 `ustc-token-badge` 条目。
- 若插件导致启动异常：删掉上述条目即可恢复，DSH 会跳过加载失败的条目。

## 开机自启

`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\USTC-Token-Monitor.vbs`
用 `pythonw.exe` 无窗口启动 `monitor.py --serve`。删除该文件即可取消自启。

## 文件

```
monitor.py        采集 + 统计 + JSON API（单文件，仅标准库）
web/index.html    看板前端（明/暗主题、来源筛选、SVG 图表）
usage.db          SQLite 历史库（`requests` 表，`request_id` 主键，幂等写入）
plugin/           DSH 插件源码
install-plugin.ps1
start.bat         手动启动看板
tests/            插件与宿主路由的冒烟测试（node --test 风格的手写断言）
```

## 注意

- Key 从环境变量 `USTC_API_KEY` 读取，缺失时回退 `~/.dsh/.credentials.yaml`，
  只在内存中持有，日志里掩码显示为 `sk-...abcd`。
- 看板只监听 `127.0.0.1`；`/api/summary` 允许任意来源读取（仅本机可访问）。
- 平台账单接口的 `start_date/end_date` 按 UTC 解释，脚本已按本地日重新过滤。
- 限额卡片用平台 `current_spend`（实际扣减值），约半分钟批量更新一次，会阶梯式跳变。
- 监测器不调用 `/v1/chat/completions`，不占用 `rpm_limit=20` 的对话配额。
