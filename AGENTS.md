# AGENTS.md

PlotPilot（墨枢）— 开源剧情引擎内核，用结构化叙事状态管理在数十万字规模上保住人物一致性、因果链完整性与伏笔闭环。LLM 仅作 prose 生成器，续读由 `engine/runtime/` 下的运行时承载。

> 这份文件是面向所有 AI 编程 Agent 的项目总览。具体到本机 Claude Code 的工作约定见同目录 `CLAUDE.md`（比 `AGENTS.md` 更细，包括 entry-point 陷阱、运行时坑位、单一写者派发器等）。

## Setup commands

### 后端（Python 3.11+，SQLite，向量库默认 ChromaDB）

- 装核心依赖（秒级）：`pip install -r requirements.txt`；用 uv 则 `uv sync`（按 `pyproject.toml` + `uv.lock` 一步建 `.venv` 并装依赖，要求 Python ≥3.14，uv 会自动下载）
- 装本地向量模型（按需，不在 `uv.lock` 里）：`pip install -r requirements-local.txt`；用 uv 则 `uv pip install -r requirements-local.txt`
- 复制环境变量：`cp .env.example .env`，至少填一个 LLM key（`ANTHROPIC_API_KEY` / `ARK_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`）
- 启动 API 服务（端口 8005）：`uvicorn interfaces.main:app --host 127.0.0.1 --port 8005 --reload`；用 uv 则 `uv run uvicorn interfaces.main:app --host 127.0.0.1 --port 8005 --reload`；加 `DISABLE_AUTO_DAEMON=1` 可禁止 API 启动时自动拉起守护进程
- 启动独立守护进程（与 API 同 DB/日志）：`python scripts/start_daemon.py` / `uv run python scripts/start_daemon.py`（`scripts/setup/start_daemon.py` 是过期副本，别用）
- 应用数据库迁移（启动时也会自动跑）：`python scripts/run_migrations.py` / `uv run python scripts/run_migrations.py`
- **uv 通用规则**：项目根下任何 `python` / `uvicorn` / `pytest` 命令加 `uv run` 前缀即等价切换，无需手动激活 `.venv`
- **Entry-point 陷阱**：`python cli.py serve` / `python -m cli serve` 会因相对导入失败（正确姿势是在上级目录 `python -m PlotPilot serve`）；`python interfaces/main.py` 绑的是 `0.0.0.0:8000` 而非 8005

### 前端（Vue 3 + TS + Naive UI + Vite，可选 Tauri 桌面壳）

- 装依赖：`cd frontend && npm ci`
- 开发服务器（端口 3000，`/api` 代理到 8005）：`npm run dev`
- 构建（**这是唯一的前端门禁**，包含 `vue-tsc` 类型检查）：`npm run build`
- Tauri 桌面：`npm run tauri:dev` / `npm run tauri:build`
- 校对前端 taxonomy 与 `shared/taxonomy/*.yaml` 不漂移：`npm run check:shared-config`（未接入 CI，改了 `shared/taxonomy/` 要手动跑）
- `predev`/`prebuild` 会自动重新生成 taxonomy JSON，漂移会被静默修复——只改 YAML 源，**绝不改生成的 JSON**

## Project layout

按 DDD 分层 + 一块独立的生产运行时内核：

- `domain/` — 纯业务模型，零外部依赖（**改这里最稀有**，约 19/200 次提交）
- `application/` — 用例编排（**最高频修改**，约 622/200 次提交）；内含 Evolution / Governance / Memory / DAG / Narrative Engine 等子系统
- `infrastructure/` — 可替换技术：LLM 客户端、向量库、SQLite 仓、导出器、CPMS
- `interfaces/` — FastAPI 边界 + DI 工厂 + 守护进程管理；新依赖都加在 `interfaces/api/dependencies.py`
- `engine/` — **生产运行时内核**，独立包；包含 `engine/runtime/`、`engine/pipeline/`、`engine/pipelines/`、`engine/core/`
- `frontend/` — 官方工作台：Vue 3 + TS + Naive UI + Tauri 壳
- `shared/taxonomy/` — 跨端分类学的 YAML（前端 JSON 是从这里生成的产物）
- `scripts/` — 工具脚本（守护进程、迁移、评估、安装器）
- `tests/` — 单测 / 集成 / DAG / E2E，镜像分层结构
- `docs/` — 长文档（`ARCHITECTURE.md`、`BUILD_INSTALLER.md` 等）；**部分内容已过时**（如章节 pipeline 实为 12 步而非"十步"、并无 FAISS 实现），以代码为准
- `tools/` — 启动器（`plotpilot.bat`）、嵌入模型下载说明

## Code style

- **没有 Python linter/formatter**（无 ruff/black/flake8/pre-commit）；不要发明 `python -m lint`
- **没有前端 ESLint**；`npm run build` 是前端唯一门禁
- **依赖边界是"约定"，不是强制的**：`domain/` 不应引入外部依赖；现有的越界（`domain/novel/value_objects/story_phase.py` 引入 `engine.core.entities.story`）不要新增
- **分层位置是 PR 评审项**：新建文件先想清楚属于哪一层；加表/字段 PR 必须写迁移说明；改 API 契约必须显式说明
- **SQLite 写盘统一走单一写者派发器**：业务代码不要直接 `import sqlite3`，经 `infrastructure/persistence/database/connection.py` 的 `db.execute()` / `db.transaction()`；底层 `write_dispatch.py` 负责串行化
- **Prompt 注入字符串用常量**：`infrastructure/ai/prompt_keys.py` 是 CPMS 82 个节点键的唯一来源；新增模板放在 `infrastructure/ai/prompt_packages/nodes/<node_key>/` 并 bump `bundle_meta.yaml` 版本号
- **测试镜像分层**：`tests/unit/{application,domain,engine,infrastructure,interfaces}/`；CI 只跑 `pytest tests/unit -q --tb=short`
- **两种 import 风格并存**：仓库根自身是包（根 `__init__.py` + `__main__.py`），`import application.x` 与 `PlotPilot.application.x` 都能解析；跟随所改文件的既有风格，别混用（同模块双名加载会产生重复模块状态）
- **弃用 shim 不要续命**：`engine/application/` 与 `application/engine/services/autopilot_daemon.py` 都是转发到 `engine/runtime/` 的弃用层，新代码一律写向 `engine/runtime/`

## Testing instructions

- 单测（**CI 默认**）：`pytest tests/unit -v`（uv 环境：`uv run pytest tests/unit -v`，下同）
- 集成：`pytest tests/integration -v`
- DAG 专项：`pytest tests/dag -v`
- 全量：`pytest tests/ -v`
- 跑单个用例：`pytest <path>::<Class>::<test> -q`
- 前端类型 + 构建：`cd frontend && npm run build`
- **新行为必须有测试；与新文件同包的现有 `test_*.py` 是模板**
- 测试需要占位 LLM key（已在 CI 配 `ANTHROPIC_API_KEY=test-placeholder`）

### 改测试时的两条硬纪律

这两条来自本项目真实付出过代价的教训（`5c1da09d` 那一轮补回了两条被静默删除的守卫）。

**1. 改写测试时不得静默删除既有守卫。**

重构语义（例如把「值 + 独立标记」改成「值本身三态」）时，很容易把为**旧语义**写的用例整段删掉。那些用例当时正挡着真实的回归：删掉之后，`audit` 两分支方向对调、D-02 主路径 `slot_weights` 改成 `None` 都变回全绿 —— 而且没有任何信号提示你少了保护。

- 用例失效时**改写**它并在注释里写明「为什么旧断言不再适用、新断言是什么」，不要 `rm`。
- 删任何一条 `def test_` 前，先确认它挡的是哪一类回归（用变异验证确认：删了它、打坏实现，还能不能转红）。

**2. 变异验证必须覆盖「本次新增的修复」本身，不只是既有测试。**

只对既有测试打变异，会漏掉「我修了但没测」的那部分 —— 实测中连续三轮都是新修复没有守卫，靠独立审查才抓回来。

- 本次改动的每一条修复，都要有一条**专门为它**的变异能打红。
- **守卫自身的非空性要单独验证**：写完先确认它在「实现被改坏」时真的会红。踩过的三种假守卫：
  - `assert "X" not in inspect.getsource(mod)` — 注释里出现 `X` 就误报，换个调用写法又绕过。改判 AST。
  - 条件恒 False 的复合断言（例如同时要求两个只可能同时不成立的名字出现）—— 永远通过。写完用一次变异确认它能红。
  - 扫描器把节点类型判错而**永远收集不到任何候选**（如遍历只产出语句、却去找 `ast.Name` 叶子）—— 「没报出来」与「没问题」在断言里长得一模一样。给扫描器本身配正向用例：造一个确实有问题的样本，确认它报得出来。
- 变异脚本要备份 + SHA256，还原后**分别**断言「目标特征已恢复」与「工作区回到基准」—— 只看「文件存在」这类弱信号，校验通过不等于还原到了正确状态。

### 防「假数据」的三条判定

本项目的核心红线是「降级路径不得返回看起来正常的数值」，代码里最容易复发的三处：

- **别用 `or` / `or 0` 兜底可选值**：`x or 0` 会把 `None`、`0`、`""`、`[]` 压成同一个值。需要「没有」这个状态时用显式 `is None` 判断。
- **代码改写了取值就别标成「AI 给的」**：模型给 8、代码抬到 11，落库的 source 应是 `ai_adjusted` 而不是 `ai`——否则下游会当可信 provenance 消费。
- **别用 `str()` 把非字符串伪装成信号**：`str(None)` 得到 `"None"`，复盘时看起来像模型说了什么。脏项要么丢掉，要么用显式 schema 归一化。


## PR & commit conventions

- 默认分支是 `master`；**永远不要直接 push 到 master**，先 fork 或拉分支
- 提交风格自由（项目本身不强制 conventional commits）
- PR 模板强制回答：新增文件在哪一层、为什么；新增表/字段的迁移说明；变更 API 契约的影响范围
- CI 绿后用 `gh pr create` 开 PR；远程：fork 是 `git@github.com:uwakeme/PlotPilot.git`、上游是 `git@github.com:shenminglinyi/PlotPilot.git`（本机 HTTPS 443 不通，SSH 22 通）

## Security

- **绝不要提交密钥**：`.env` 在 `.gitignore`；`data/`、`logs/`、数据库、向量库目录也全部 gitignore
- LLM key 至少配一个；缺 key 会落到 `MockProvider`，不代表能上线
- 写盘测试一律设 `PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES=1`；**应用代码不要设**，会绕开单一写者派发器
- Windows 是主要开发平台；桌面端走 Tauri；`tools/plotpilot.bat` 是统一启动器
- **本机访问边界（`interfaces/api/local_access.py` 是唯一事实来源）**：后端没有登录态，
  唯一边界就是"只有本机进程与本机前端能调 API"。**不要把 CORS 改回 `*`**，也不要把高危端点的
  守卫退回"只看 `request.client.host`"——网页发来的跨域请求 client host 同样是 `127.0.0.1`。
  - `DEFAULT_CORS_ORIGINS` 必须同时包含 Tauri v2 两个平台的 webview origin：
    `http://tauri.localhost`（Windows/Android/WebView2）与 `tauri://localhost`（macOS/Linux）。
    缺一个就有一端直接不可用，且失败表现为静默的 "Failed to fetch"。
  - 桌面模式 webview origin → `127.0.0.1:<动态端口>` 是跨域的，所以 CORS 仍然必需；
    浏览器开发模式走 Vite 代理是同源，不需要 CORS。
  - 改完跑 `tests/unit/interfaces/api/test_local_access.py` 验证没有放宽。
- **已存密钥不得外发**：`/llm-control/models` 复用已存 key 时会校验请求 base_url 与激活 profile 同源；
  错误响应不回显上游响应体（上游可能把 Authorization 回显出来）。改动前先读该文件的注释。

## 已知的有意偏离（**别当成 bug 修**）

这几条是**产品决策**，不是没做完。改动前先看这里，否则会把决策改回缺陷。

- **写前连续性闸门（演化闸门）有意「告警但不阻断」** ——
  `engine/pipeline/base.py` 的 `_step_prepare_governance` 只发 `logger.warning`，
  **不**返回 `StepResult.fail`，**不**拦章。
  - 为什么：闸门的补救建议是「改为回忆/遗物/传闻」或「添加 [TimeSkip]/[HardCut]」——
    那是**改大纲**的动作，而管线在这一步没有改大纲的能力（大纲来自上游幕级规划）；
    真拦下来只会让章节凭空失败。
  - 匹配规则本身误报率也高（≥2 字裸串即命中，「大纲里提到某人」被当成
    「让其以活跃人物出现」）。
  - 后果要说清：登记表那条不变量「**死亡角色不得实际登场**」**目前没有代码保证** ——
    闸门只负责**报出来**（报告在 `ctx.evolution_continuity_report`）。
    要真正拦章，得先给管线一个「改大纲」的能力。
  - **也不许让上下文降级**（H-01a，2026-10-21 补）：同一条链路上还有**第二处**闸门判定
    （`prepare_chapter_generation` 内部，对七段剧本再判一次）。它遇到 blocking 违规会
    `raise "evolution_gate_blocked:"`，而 `base.py` 的 `except Exception` 会把它当成
    「上下文准备失败」⇒ **整份 bundle 被丢掉**、预算从 35000 掉到 20000
    ⇒ 上下文被**静默削薄 43%**。管线的修法是给那次调用传
    `allow_evolution_gate_bypass=True`：闸门仍然跑、结论仍然进 bundle
    （`evolution_gate` / `evolution_gate_blocked`）、blocking 违规仍然以
    「=== EVOLUTION GATE ===」块**前置进上下文文本**给模型看，
    但**不中断上下文组装**。守卫：
    `tests/unit/engine/test_evolution_gate_context_integrity.py`。
  - 记录位置：登记表 `docs/superpowers/specs/2026-10-16-ai-participation-decision-registry.md`
    的「偏离登记」小节；ledger
    `.superpowers/sdd/2026-10-16-ai-participation-batch0-plan/progress.md`。
  - 用户已拍板（2026-10-21）：**接受这个偏离**，但要求落显式记录，别只留在 commit message 里。