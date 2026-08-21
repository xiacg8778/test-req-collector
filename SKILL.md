---
name: test-req-collector
description: 测试需求采集 skill —— 当用户要求"采集某个网站/项目的需求信息"、"没有文档怎么做测试"、"生成测试点"、"分析网页元素并产出测试用例"、"AI 实地学习一个项目"时使用。输入目标 URL（可带关注功能点），自动用浏览器双通道采集（页面截图 + DOM 可交互元素），按模块归档为可复用采集包，可选调用 LLM 生成测试点文档。对标"AI 测试智能体平台"的需求采集能力。纯 CLI，无 API 服务。
agent_created: true
---

# test-req-collector · 测试需求采集

把"AI 测试智能体"视频中演示的需求采集功能落成本地 CLI skill：
**输入 URL（+ 关注功能）→ 浏览器双通道采集（截图 + DOM 元素）→ 按模块归档 → 可选 LLM 生成测试点**。

## 何时使用

- 用户拿到一个网站/系统但没有需求文档，需要 AI 实地采集信息来做测试
- 用户要求"生成测试点 / 测试点分析 / 从页面生成测试用例"
- 用户要求"采集这个网站的元素 / 分析页面结构"

## 工作流

```
输入: URL + --focus "登录,排行榜" (关注功能点，可选)
  │
  ▼ ① 浏览器渲染 (Playwright + Chromium, headless)
  ▼ ② 双通道采集
  │    ├─ 视觉通道: 全页截图 (screenshot.png)
  │    └─ 结构通道: DOM 可交互元素 (elements.json: 按钮/链接/输入框/表单,
  │                含 text/type/name/id/placeholder/aria-label/href/css路径)
  ▼ ③ 按页面归档: page-{n}-{slug}/ (page.md + elements.json + screenshot.png)
  ▼ ④ 汇总: report.md (所有页面 + 元素统计 + 关注点命中)
  ▼ ⑤ (可选) gen_test_points.py: 采集包 → LLM → 测试点文档 test_points.md
```

## 用法

### 0. 环境

在当前 WorkBuddy macOS 托管环境中使用隔离 Python：
```bash
PY=/Users/xiacg/.workbuddy/binaries/python/envs/default/bin/python3
$PY -m pip install -r requirements.txt
$PY -m playwright install chromium
```

不要使用系统 `python`/`pip`，也不要把依赖安装到全局环境。其他机器需将 `PY` 替换为已安装 Playwright 与 Chromium 的隔离 Python 绝对路径。

### 1. 采集（核心命令）

```bash
# 基本用法：采集一个站点首页
$PY scripts/collect.py "https://example.com" -o outputs/demo

# 指定关注功能点（命中元素会优先标注）
$PY scripts/collect.py "https://example.com" --focus "登录,排行榜,注册" -o outputs/demo

# 跟随同域导航链接采集多页（模拟"浏览首页/排行榜等模块"）
$PY scripts/collect.py "https://example.com" --follow-links --max-pages 3 -o outputs/demo

# 其他选项
#   --wait-ms 5000       页面加载后额外等待（SPA 用）
#   --timeout-ms 30000   页面加载超时
#   --no-screenshots     跳过截图（纯元素采集）

# v2: 认证态采集（登录后页面）
$PY scripts/collect.py "https://app.example.com/orders" \
  --storage-state auth.json --environment test \
  --allowed-origin "https://app.example.com" \
  --auth-success-marker "欢迎" -o outputs/authed

# v2: 受控 L2 行为采集（journey runner）
$PY scripts/run_journey.py journey.json --storage-state auth.json --allow-write -o outputs/journeys
# 门禁: risk=destructive 一律拒绝; risk=write 需 --allow-write 且 recipe 必须有 data_namespace
# 产物: {journey_id}_behavior_events.json (逐步 ui_diff/快照引用/错误) + {journey_id}_network_events.json (仅元数据, 正文不保存)
```

产物（`outputs/demo/` 下）：
- `report.md` — 采集总览：每页标题/URL/元素统计/表格/关注点命中
- `manifest.json` — schema v2：run_id、认证态状态、每页 evidence_id 与 content_hash
- `site-{slug}/page-{n}-{slug}/` — 每页：`page.md`（页面摘要）、`elements.json`（v1 兼容元素清单）、`static_inventory.json`（v2 增强证据：字段约束/下拉选项/表格结构/locator 候选/静态指纹，fact_level=OBSERVED）、`screenshot.png`（全页截图）

### 2. 生成测试点（可选，需 LLM）

```bash
# 使用明确提供的环境变量文件（不要在文档或命令中写入密钥）
$PY scripts/gen_test_points.py "outputs/demo" --env-file "/absolute/path/to/llm.env"

# 或用标准环境变量: VIDEO_SUMMARY_LLM_BASE_URL / VIDEO_SUMMARY_LLM_API_KEY / VIDEO_SUMMARY_MODEL
# 或 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL

# 当前 gen_test_points.py 只读取 elements.json；screenshot.png 仅作为人工或其他多模态分析器的证据，不会被该脚本消费。
```

产物：`test_points.md` — 按页面/模块分组的测试点清单（含优先级、前置条件、预期结果）。

## 采集了什么（字段说明）

| 字段 | 说明 |
|---|---|
| `elements[].tag` | 标签名 (a/button/input/...) |
| `elements[].text` | 可见文本（截断 60 字） |
| `elements[].type` | input 的 type |
| `elements[].name/id` | 表单字段名/元素 id |
| `elements[].placeholder` | 占位提示 |
| `elements[].aria_label` | 无障碍标签 |
| `elements[].href` | 链接目标 |
| `elements[].css_path` | 唯一 CSS 定位（id > 层级 nth-of-type） |
| `elements[].focus_hit` | 是否命中用户关注关键词 |
| `headings[]` | h1-h3 标题（页面结构） |
| `meta` | title/description/URL |

## 异常与边界条件

| 场景 | 处理 |
|---|---|
| 页面加载超时/失败 | 默认超时 30s（`--timeout-ms` 调整），失败页跳过并记入 report.md；全部失败则报错退出 |
| 需要登录/验证码的站点 | v2 支持 `--storage-state` 加载登录态并校验：被重定向到登录页或 `--auth-success-marker` 未出现时阻断采集并写入 manifest `auth.status=blocked`；不尝试绕过验证码 |
| 反爬拦截（429/滑块） | 加 `--wait-ms` 放慢节奏；仍失败则如实报告，**不编造页面内容** |
| SPA 动态渲染 | 页面加载后额外 `--wait-ms 5000` 再提取 DOM，避免拿到空壳 |
| 关注点 0 命中 | report.md 标注"0 命中"，提示调整 `--focus` 关键词或检查页面结构 |
| LLM 空响应 | `gen_test_points.py` 自动重试 3 次（推理模型 max_tokens 耗尽场景）；仍空则报错退出，**不写 0 字节文件** |
| LLM 输出截断 | 检测 `finish_reason=length` 并警告，保留已生成部分；可缩小 `--focus` 后重试 |
| LLM key 未配置 | gen_test_points.py 报错并给指引；采集本身不依赖 LLM，可正常完成 |

## 检查点（人在回路）

执行前与用户确认关键决策，防止自主失控：
1. **采集前**：确认目标 URL、关注功能点（`--focus`）、预计采集页数（`--max-pages`）、输出目录
2. **生成前**：展示采集包摘要（页面数/元素数/关注点命中），确认后再调用 LLM（有 token 消耗）
3. **异常时**：页面大面积失败或命中率 0，先报告再继续，不静默降级

## 设计要点

- **双通道采集**：同时产出截图和结构化 DOM 元素；当前内置 `gen_test_points.py` 只消费 DOM，截图需由人工或外部多模态分析器单独使用
- **采集与生成解耦**：采集包可作为测试点和用例设计的证据输入；当前 CSS 路径可能退化到 `nth-of-type`，未经稳定性校验不得直接冻结为长期自动化定位资产
- **聚焦而非全爬**：`--focus` 让用户圈定功能范围，控制采集成本、贴合测试重点
- **安全边界**：只读目标站点页面、只写输出目录；不执行页面内任意代码；LLM key 仅从 env 文件/环境变量读取，不落盘不打印
- **实现原理**：双通道采集的浏览器端 DOM 提取策略、归档格式、LLM 调用细节 → 见 `references/how-it-works.md`

## 命令参考

| 命令 | 说明 |
|---|---|
| `collect.py <url>` | 双通道采集（截图+DOM+schema v2 inventory），支持 --focus/--follow-links/--max-pages/--storage-state |
| `run_journey.py <journey.json>` | 受控 L2 行为采集；destructive 硬拒、write 需 --allow-write；输出行为/网络证据 |
| `gen_test_points.py <采集目录>` | 采集包 → LLM → 测试点文档 |

## 验证

```bash
# 冒烟：本地起测试站
cd examples && $PY -m http.server 8765 &
$PY scripts/collect.py "http://127.0.0.1:8765/demo/index.html" --focus "登录,排行榜" -o outputs/smoke
```

## 维护注意

- 采集脚本只依赖标准库 + playwright（无第三方依赖）
- DOM 提取在浏览器端 evaluate 完成（快、准），Python 端只做组织与落盘
- 修改涉及网络请求/子进程时保持"URL 校验 + 不执行任意代码"的安全边界
