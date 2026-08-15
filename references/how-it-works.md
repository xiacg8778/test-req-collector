# 实现原理：对标"AI 测试智能体平台"的需求采集功能

## 视频功能 → 本 skill 的映射

| 视频演示的能力 | 本 skill 实现 |
|---|---|
| AI 打开网站，提示"工具已经就绪" | Playwright Chromium 启动 + 页面加载（无头浏览器即"AI 的工具浏览器"） |
| 用户圈定功能："看登录功能、排行榜功能" | `--focus "登录,排行榜"` 关键词命中标注 |
| 采集界面截图："这是他看到的界面" | 全页截图 `screenshot.png`（视觉通道，可喂多模态 LLM） |
| 采集元素信息："这个界面里面的一些元素信息" | `elements.json` 可交互元素清单（结构通道，可喂推理 LLM） |
| 按模块归档："首页的界面、排行榜的界面" | 每页一个 `page-{n}-{slug}/` 目录（page.md + elements.json + png） |
| "这笔内容生成测试点" | `gen_test_points.py`：采集包 → LLM → test_points.md |
| "我可以同时做多个"（多智能体并行） | 可对多个站点/多个 focus 并行跑多个 collect 进程，互不干扰（目录隔离） |

## 技术架构

```
┌─ 指令层 ──────────────┐
│ collect.py <url>       │  --focus 圈定功能范围（人机协同）
│  --follow-links        │  --max-pages 控制采集规模
└──────────┬────────────┘
           ▼
┌─ 感知层 ──────────────┐
│ Playwright (headless)  │  浏览器即工具：导航/等待/截图/DOM 读取
│   ├─ 视觉通道: 全页截图  │   → 多模态模型可识别布局与界面
│   └─ 结构通道: evaluate │   → 浏览器端提取可交互元素
│       提取 DOM 元素     │      (a/button/input/select/form/role=…)
└──────────┬────────────┘
           ▼
┌─ 组织层 ──────────────┐
│ page-{n}-{slug}/       │  按页面/模块归档：page.md 人类可读
│  ├─ page.md            │  elements.json 机器可读（含 css_path）
│  ├─ elements.json      │  screenshot.png 视觉证据
│  └─ screenshot.png     │  report.md 全站总览
└──────────┬────────────┘
           ▼
┌─ 消费层（可选）────────┐
│ gen_test_points.py     │  采集包 → 上下文注入 LLM
│  → test_points.md      │  → 按模块输出测试点（优先级/步骤/预期）
└───────────────────────┘
```

## 关键设计决策

### 1. 为什么 DOM 提取放在浏览器端 evaluate？
- 一个 `page.evaluate` 调用内同步遍历，比 Python 端逐个 `locator` 查询快 1-2 个数量级
- 直接访问 `innerText/placeholder/aria-label` 等渲染后属性，拿到的是用户真实可见的信息
- 生成 CSS path 时优先 `id`（稳定），退化到 `tag:nth-of-type` 层级路径（唯一且可回放）

### 2. 为什么截图用 full_page？
- 测试场景需要看到完整页面布局（头部导航、主体、页脚），视口截图会截断
- 多模态 LLM 分析"页面有哪些模块"时，整页截图信息最全

### 3. 为什么采集与生成解耦？
- 同一采集包可多次消费：生成测试点、写用例、做 UI 自动化定位（css_path 可直接用于 Playwright 选择器）
- 采集贵在浏览器资源，生成贵在 LLM token——解耦后可分别复用

### 4. 为什么 --focus 是"命中标注"而非"只采集命中"？
- 全量元素仍保留在 elements.json（信息不丢），report/page.md 优先展示命中项
- 用户先粗采一轮看全貌，再针对 focus 精生成测试点，流程弹性最大

## 与通用开源组件的边界

| 组件 | 定位 | 本 skill 与它的关系 |
|---|---|---|
| browser-use (109K★) | AI 自主操作浏览器的通用库 | 底层引擎可选替换（collect.py 的 Playwright 调用可换成 browser-use 驱动） |
| playwright-mcp (36K★) | 浏览器 MCP server | 面向"对话式 Agent 即席操作"，本 skill 是**批处理流水线**，产物结构化落盘 |
| awesome-qa-skills | QA 提示词技能库 | 有需求分析/用例编写（吃已有文档），本 skill 补上**网页采集引擎**这一环 |
| 视频平台自研 | 商业产品 | 本 skill 是其最小可复现子集（采集→归档→测试点） |
