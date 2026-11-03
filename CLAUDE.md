# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PlotPilot (墨枢) is an open-source **narrative engine kernel** for long-form AI-assisted fiction writing. It is not a chatbot — it is a systems-engineering approach to maintaining character consistency, causal chain integrity, and foreshadowing closure across hundreds of thousands of words via structured narrative state management.

The LLM is treated as a stateless prose generator; all continuity lives in engine-managed state (Bible, chapter summary chains, narrative events, story-line DAG, foreshadowing registry) assembled into a context window per chapter.

## Commands

```bash
# Backend — API server (port 8005). Run from the repo root.
uvicorn interfaces.main:app --host 127.0.0.1 --port 8005 --reload

# Autopilot daemon — standalone production entry (writes to the same DB/log)
python scripts/start_daemon.py

# Frontend — dev server (port 3000, proxies /api → 127.0.0.1:8005)
# `predev`/`prebuild` hooks auto-run the shared-taxonomy sync first
cd frontend && npm run dev

# Frontend — type-check + build (vue-tsc is the ONLY type check; there is no ESLint)
cd frontend && npm run build

# Frontend — verify generated taxonomy bundles match shared/ sources
cd frontend && npm run check:shared-config

# Tauri desktop client
cd frontend && npm run tauri:dev
cd frontend && npm run tauri:build

# Tests
pytest tests/ -v
pytest tests/unit -v              # what CI runs
pytest tests/integration -v
pytest tests/dag -v

文件数会漂，别在注释里写死。要引用当前值就用下面任一条现数
（`glob`/`wc` 是 bash 的东西，Windows 上跑不了）：

```powershell
# PowerShell（默认 shell）
(Get-ChildItem -Recurse tests/unit -Filter test_*.py).Count
# 或只数已跟踪文件。
# ⚠ 别用 `git ls-files 'tests/unit/**/test_*.py'`：`**/` 要求至少一层目录，
#   会漏掉根级那几个文件（偏低 5）。
(git ls-files 'tests/unit/test_*.py' 'tests/unit/*/test_*.py' 'tests/unit/*/*/test_*.py' 'tests/unit/*/*/*/test_*.py' | Measure-Object -Line).Lines
```

# Single test file / single test
pytest tests/unit/application/foo/test_bar.py -q
pytest tests/unit/application/foo/test_bar.py::TestBar::test_baz -q

# Coverage
pytest tests/ --cov=. --cov-report=term-missing

# Apply DB migrations (also runs automatically on startup)
python scripts/run_migrations.py
```

**There is no Python linter or formatter configured** (no ruff/black/flake8/pre-commit) and no frontend linter. Do not invent lint commands; `npm run build` is the frontend gate.

### uv equivalents

The repo ships `pyproject.toml` + `uv.lock` (requires Python ≥ 3.14; `uv` downloads it if missing). Any command above that assumes an activated venv has a 1:1 `uv run` form — no manual activation needed:

```bash
uv sync                                        # replaces `python -m venv .venv` + `pip install -r requirements.txt`
uv pip install -r requirements-local.txt       # local vector models (not in the lockfile, uses pip-compat layer)
uv run uvicorn interfaces.main:app --host 127.0.0.1 --port 8005 --reload
uv run python scripts/start_daemon.py
uv run python scripts/run_migrations.py
uv run pytest tests/unit -v
```

### Entry-point gotchas

- `python cli.py serve` and `python -m cli serve` **both fail** with `ImportError: attempted relative import with no known parent package` — `cli.py` uses relative imports because the repo root is itself a package (root `__init__.py`, `__main__.py`). The working form is `python -m PlotPilot serve` **from the parent directory** (`D:\WorkSpace\projects`). Note it kills whatever process holds port 8005 before binding.
- `python interfaces/main.py` binds **`0.0.0.0:8000`**, not 8005. Use the `uvicorn` command above.
- `scripts/setup/start_daemon.py` is a **stale duplicate** that imports the deprecated `AutopilotDaemon`, uses an old `data/novels.db`, and a simplified `ContextBuilder`. Always use `scripts/start_daemon.py`.

## Architecture

DDD four layers, plus a fifth sibling package (`engine/`) that holds the production runtime.

```
domain/           # Pure business models. Zero external deps. Changing this is rare (19/200 recent commits).
application/      # Use-case orchestration. Highest churn (622/200 recent commits).
infrastructure/   # Replaceable tech: LLM clients, vector stores, SQLite repos, exporters.
interfaces/       # FastAPI boundary + DI factories + daemon process management.
engine/           # The production runtime kernel — daemon, chapter pipeline, genre pipelines.
frontend/         # Official workbench: Vue 3 + TS + Naive UI + Tauri shell.
shared/           # Cross-cutting taxonomy YAML (SSOT for generated frontend JSON).
```

`domain/`, `application/`, `infrastructure/`, `interfaces/` are all importable as **top-level** modules (`import application.paths`) thanks to `pythonpath = .` in `pytest.ini`. A few entry points instead use package-relative imports (`PlotPilot.*`). Both styles coexist — check the file you are editing.

### `engine/` — the production kernel

This is the most important package and is separate from `application/engine/` (a normal DDD application package with no special status):

```
engine/runtime/     # EngineDaemon, StoryPipelineRunner (runner.py), daemon_host.py, daemon_loop.py,
                    # stage delegates (macro_planning / act_planning / writing / legacy_writing / audit),
                    # novel_lifecycle.py, policy_validator.py, quality_guardrails/, plot_state_machine/
engine/pipeline/    # BaseStoryPipeline (base.py) — the 12-step chapter pipeline; context, steps,
                    # beat_contracts, generation_prompt_builder, prose_composer, recovery, telemetry
engine/pipelines/   # Genre registry (registry.py → PipelineRegistry). Only "wuxia" is registered;
                    # unknown genres fall back to ThemedStoryPipeline.
engine/core/        # Engine-side entities, ports (LLMPort/PersistencePort/EventPort/TracePort), VOs
engine/infrastructure/  # event_bus, engine-side memory (character_psyche/soul, echo_recall), checkpoint & trace stores
engine/application/ # DEPRECATED shim re-exporting engine.runtime.* ("will be removed in v4.0")
```

### Production daemon chain

```
scripts/start_daemon.py :: build_daemon()
    → reuses interfaces.api.dependencies factories (so daemon and API share one config)
    → builds repos, BackgroundTaskService, ContinuousPlanningService,
      ChapterAftermathPipeline, CircuitBreaker
    → EngineDaemon(...)  [engine/runtime/engine_daemon.py]  — "统一生产入口"
            → StoryPipelineRunner [engine/runtime/runner.py]
                    → BaseStoryPipeline [engine/pipeline/base.py]
```

`interfaces/daemon_manager.py:175` does `from scripts.start_daemon import build_daemon` — the API and the standalone daemon construct the daemon through the *same* factory. When adding a dependency to the daemon, add it there.

`PLOTPILOT_USE_STORY_PIPELINE` is read in `engine/runtime/writing_delegate.py`: unset / `writing` / `1` / `full` / `engine` → StoryPipeline; `off` / `legacy` → legacy beat writing (emergency rollback path).

**The pipeline has 12 steps, not ten** (`grep 'def _step_' engine/pipeline/base.py`): `find_next_chapter`, `prepare_governance`, `build_context`, `prepare_chapter_plan`, `generate`, `generate_with_composer`, `validate_content`, `save_chapter`, `validate_voice`, `run_post_commit`, `score_tension`, `finalize`. README and `docs/ARCHITECTURE.md` already say 12 — correct as of 2026-10-18 (this note used to claim they said 十步, which was itself out of date).

**Step order ≠ definition order.** The orchestration calls `_step_find_next_chapter → _step_prepare_governance → _step_prepare_chapter_plan → _step_build_context → _step_generate` (grep the `self._step_*` call sites in `run_chapter`, not `def _step_`). This matters: `build_context` sits **after** `prepare_chapter_plan`, so anything a step writes into `ctx.outline` gets overwritten by that step's preplan result.

### AI-participation switches

Two different mechanisms, and only one of them is per-feature:

**① The master switch (the actual rollback path)** — `config/performance.yaml` → `ai_participation:`.

```yaml
ai_participation:
  master_enabled: true      # false ⇒ every registered AI capability stops calling the model
  disabled_features: []     # name individual features to disable while the master is on
```

`ai_participation_enabled(feature)` (`application/engine/services/ai_participation_settings.py`) is the gate; the 25 registered feature ids live in `AI_PARTICIPATION_FEATURES`, and every one of them has a **real short-circuit gate on the model-call path** (guards: `test_ai_participation_master_switch.py`, `test_ai_participation_master_switch_coverage.py`, `test_ai_participation_gate_dominance.py`).

⚠ The master switch has **three levels** (config < env < novel), added in the 83rd batch (N-09): ① `config/performance.yaml` → `ai_participation.master_enabled` + `disabled_features`; ② env `PLOTPILOT_AI_MASTER_ENABLED` + `PLOTPILOT_AI_DISABLED_FEATURES`; ③ this book's `generation_prefs.ai_switches[<feature_id>] = False`. The novel level may only **turn things off**, never on (the master switch is a rollback path, not a feature toggle). Two features (`plot_point_classifier`, `character_belief_check`) have **no code fallback**: with the gate closed their judgement simply does not exist (marked UNASSESSED, never faked).

**② The unified switch layer** (`application/engine/services/ai_switch_layer.py`) — priority `novel.generation_prefs.ai_switches` > `PLOTPILOT_AI_<KEY>` (name derived from the key) > default.

⚠ It registers only **5 keys** and effectively serves **3 features** (D-01 / D-02 / F-04). The other 22 features have **no per-feature switch** — only the master switch (which now *does* carry a novel level ⇒ "roll back per novel" is available for them **through the master switch**, just not through this layer).

The per-capability config sections:

| Switch | Section | Effect | Current state |
| --- | --- | --- | --- |
| D-02 context weights | `context_weights:` | AI judges which context slots this chapter needs most. **`enabled` alone does nothing** — `shadow: true` records without applying; both must change. | **ON** (grayscale: `enabled: true` + `shadow: false`) |
| D-01 chapter rhythm | `chapter_mode:` | AI decides whether this is a 余韵章. `enabled` only decides+logs; `apply` actually injects the pacing directive into the preplan prompt. | **ON** (grayscale: 89th batch — user approved opening `apply` ⇒ the pacing directive really reaches the body prompt) |
| D-05 chapter words | `chapter_target:` | AI proposes per-chapter word count within ±`max_deviation_ratio` (0.30); outside the band is rejected. | **ON** (grayscale) |
| E-06 rhythm quota | `rhythm_quota:` | AI assigns the chapter's pacing quota (replaces the hardcoded golden-three-chapters table). | **ON** (grayscale) |
| E-01b chapter plan shape | `chapter_plan_structure:` | AI may choose its own paragraph structure instead of the fixed seven-section script. | **ON** (grayscale: 89th batch — user approved opening it ⇒ every chapter's outline shape and body anchoring change; rollback = set `enabled: false`) |
| F-04 voice drift | `analyst.voice_analysis.use_llm_mode` | LLM-based voice analysis instead of statistics. ~500 extra tokens per chapter. | **ON** (grayscale: default switched to LLM mode — the statistics mode has **no baseline** in the writing pipeline ⇒ nothing was ever actually evaluated; each book can still switch it off) |
| F-04 voice drift **thresholds** | `analyst.voice_analysis.drift_alert_threshold` + `voice_rewrite_threshold` | similarity = 1 − mean(deviation ÷ tolerance), tolerance = `max(0.1, std×1.5)` ⇒ a **healthy chapter's neutral point is ≈0.5, not 1.0** (measured over 12 healthy chapters of 2 books: min 0.468 / median 0.554 / max 0.671) ⇒ thresholds were **recalibrated** (86th batch): drift alert 0.75 → **0.50**, rewrite trigger 0.68 → **0.40**. Every threshold on this scale must move together (judgement layer / display layer / config / examples / constants) or the two sides give different answers. | **0.50 / 0.40** |
| F-04 voice drift **judgement** | `drift_relative_drop_alert` + `voice_rewrite_relative_drop` | ⚠ **89th batch (N-24)**: the judgement is now **relative to the book** — "how far below *this book's recent norm* is this chapter" (norm = median of the last N chapters' similarity). Absolute thresholds only apply as a **fallback** when there is not enough history yet. ⚠ Why: a long book's healthy band itself moves down (opening 0.54~0.66 ⇒ ch528+ 0.40~0.53) ⇒ absolute thresholds fire on *every* chapter ⇒ false alerts + paid rewrites. One shared implementation: `application/analyst/services/voice_drift_assessment.py`. | **0.15 / 0.25** |

⚠ **Which channels are ON is a registry, not a memory** — `tests/unit/test_ai_participation_grayscale_state.py` pins the current state, because a grayscale switch that is silently flipped is the easiest thing to miss. Change the config **and** that table together.
⚠ When a channel is ON the behavior is **no longer** byte-identical to pre-switch; degradation is always "cannot decide ⇒ do not inject" (never a fallback value passed off as a decision). The dataclass baselines stay OFF — they are the fallback when a config section is missing.

## Engine Subsystems

Each is a bounded context in `application/` with one public service. Cross-importing internals is **convention only — not enforced** (there is no import-linter config or boundary test; cross-engine imports do occur).

| Engine | Path | Public entry point |
|--------|------|--------------------|
| **Evolution** | `application/evolution/` + `domain/evolution/` | `EvolutionGateService` (gate_service.py); domain side has `contracts.py`, `models.py`, `reducer.py` |
| **Governance** | `application/governance/` | `NarrativeGovernanceService` (service.py) |
| **Memory** | `application/memory/` + `domain/memory/` | `CharacterContextCompiler` (character_context_compiler.py) |
| **Chronicles (Codex)** | `application/codex/chronicles_service.py` | Module-level functions, no class: `infer_chapter_from_texts`, `build_chronicles_rows` |
| **Snapshot** | `application/snapshot/services/snapshot_service.py` | `SnapshotService` |
| **Checkpoint** | `application/checkpoint/services/unified_checkpoint_service.py` | `UnifiedCheckpointService` |
| **DAG Engine** | `application/engine/dag/` | **只有** models / registry / nodes / plan —— **没有执行器**。后端 DAG 路由在 2026-05-10（`fc5de029`）被**主动**砍成**纯展示层**（run/stop/status/runs 全删）⇒ `POST /dag/{id}/run` 返回 **501 是有意的**（宁可报错也不谎称成功）；**生产写作走 `engine/runtime` 的 StoryPipeline**（12 步），DAG 画布只是它的状态可视化 |
| **Narrative Engine** | `application/narrative_engine/` | `NarrativeEngineReadFacade` (read_facade.py) — a **read-only BFF facade**, not a generator. `NarrativeLens` enum (catalog.py) defines the 10 lens dimensions. |

`application/narrative_engine/` route nuance: only `GET /api/v1/narrative-engine/surface-catalog` lives under that prefix (`surface_router`). The other endpoints hang off `GET /api/v1/novels/{novel_id}/narrative-engine/...`.

## Cross-Cutting Mechanisms

These require reading several files to understand — get them right before touching related code.

### Write Dispatch (single-writer router)

SQLite writes are serialized through a queue. This is **transparent at the connection layer**: repositories call `db.execute()` / `db.transaction()` normally and never import the dispatcher.

- `infrastructure/persistence/database/write_dispatch.py` — the dispatcher.
- `infrastructure/persistence/database/connection.py` is where routing happens: `execute()` checks `sql_is_mutating(sql) and not allow_direct_sqlite_writes() and not is_sqlite_writer_thread()` and enqueues onto the `persistence_queue`; `transaction()` from a non-writer thread collects SQL in a `TxnCollectingConnection` and flushes it as one `EXECUTE_SQL_TXN_BATCH`.

**Rule:** never open a raw `sqlite3` connection in application/interfaces code — always go through the `DatabaseConnection` wrapper so writes reach the queue. `write_dispatch.py` must not read `os.getenv` (a test enforces this). Escape hatches are narrow and intentional: `sqlite_writes_bypass_queue()`, `startup_sqlite_writes_bypass_queue()`, and `PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES=1` — which `tests/conftest.py::pytest_configure` sets for the whole test suite.

### Dependency injection

There is no DI container. Dependencies are module-level factory functions in **`interfaces/api/dependencies.py`** (plus `interfaces/api/container.py` for store selection), wrapped with caching. Both FastAPI routes and `scripts/start_daemon.py` call these same factories — that is the mechanism keeping the daemon and API on one configuration. Add new wiring there, not ad hoc.

### LLM providers

- Port: `domain/ai/services/llm_service.py` (`LLMService`, `GenerationConfig`, `GenerationResult`).
- Implementations: `infrastructure/ai/providers/` — `anthropic_provider.py`, `openai_provider.py`, `gemini_provider.py`, `mock_provider.py`.
- Selection is by **protocol string**, not class name, in `infrastructure/ai/provider_factory.py`: `"anthropic"` → `AnthropicProvider`, `"gemini"` → `GeminiProvider`, **anything else → `OpenAIProvider`**. Ark/Doubao is *not* a separate provider class — it is the OpenAI-compatible path. A missing key or model falls back to `MockProvider`.
- Runtime entry is `DynamicLLMService` (same file), which caches providers per profile and hot-swaps on profile change.
- To add a provider: implement `LLMService`, add a branch in `LLMProviderFactory.create_from_profile`, register a profile with the new `protocol`.

### Prompt strategy (CPMS)

"Prompt injection points" are CPMS **node keys**. The real count is **102** (`prompt_keys.ALL_KEYS`), not "20+", and not "82" (that number was the count as of 2026-10).

- `infrastructure/ai/prompt_keys.py` — the single source of truth. Business code must import constants from here rather than hardcoding strings.
- `infrastructure/ai/prompt_packages/nodes/<node_key>/` — seed content: `package.yaml` (metadata + `variables`), `system.md`, `user.md`, optional `extras.json`. `bundle_meta.yaml` carries the version.
- `PromptManager.ensure_seeded()` (`infrastructure/ai/prompt_manager.py`) imports the YAML into SQLite (`prompt_templates` / `prompt_nodes` / `prompt_versions`). A `bundle_meta.yaml` **version bump** triggers incremental sync; rows with `created_by=user` are never overwritten.
- **The YAML directory is the release/seed source; per-user overrides live in the DB** (the 提示词广场 UI), not in YAML. Built-in node **content** is synced **content-driven**, not version-driven: `ensure_seeded()` overwrites any built-in version whose body differs from YAML (`_repair_corrupt_builtin_versions`), so editing YAML does reach an existing install without a version bump. The bump's real job is **bookkeeping** (`prompt_templates.version` + `version_nodes` + changelog) and driving the incremental path.
- Rendering goes through `infrastructure/ai/prompt_gateway.py` (`PromptGateway` — "CPMS-only prompt rendering entry"). Callers hold a `PromptContract` (`infrastructure/ai/prompt_contract.py`) defined in `infrastructure/ai/prompt_contracts/*.py` and call `get_prompt_gateway().render(CONTRACT, {...})` then `.validate_output(...)`.

### Vector retrieval (two parallel indexes)

Both live in `application/analyst/services/` and are wired in `interfaces/api/dependencies.py`:

1. **Chapter content index** — `chapter_indexing_service.py` (`ChapterIndexingService`), collection `novel_{novel_id}_chunks`; payload `kind` is `chapter_summary` or `bible_snippet`.
2. **Triple index** — `triple_indexing_service.py` (`TripleIndexingService`), queried via `sync_search(...)`; consumed by `application/world/services/knowledge_service.py::search_knowledge` using vector-first with fuzzy-text fallback and auto-indexing when empty.

Store selection is `interfaces/api/container.py::get_vector_store`: `VECTOR_STORE_TYPE=qdrant` → `infrastructure/ai/qdrant_vector_store.py`, otherwise `chromadb_vector_store.py` (default). A store init failure degrades to disabled rather than crashing. **There is no FAISS implementation** despite README/`docs/ARCHITECTURE.md` mentioning it. Embeddings: `local_embedding_service.py` (sentence-transformers) or `openai_embedding_service.py`.

### Database migrations

Plain `.sql` files in `infrastructure/persistence/database/migrations/`, applied idempotently by `infrastructure/persistence/database/migration_runner.py` and tracked in a `migrations_applied` table. Newest files use `NNN_name.sql`; older ones use `add_*.sql`. Add a migration by dropping in a new `.sql` file — do not edit applied ones. The PR template requires a migration note whenever you add a table or column.

### Frontend taxonomy sync

`shared/taxonomy/*.yaml` is the **authoritative** source; `frontend/scripts/sync-builtin-taxonomy.mjs` compiles it to JSON bundles (`frontend/src/domain/taxonomy/builtin_cn_v1.bundle.json`, `frontend/src/domain/worldbuilding/contract.bundle.json`) for static TS import. It also enforces a banned-phrase list (anti-cliché guard) and fails if any leak in.

`predev`/`prebuild` regenerate the bundles automatically, so **drift is silently repaired at build time** — `npm run check:shared-config` is the only thing that fails on drift, and it is **not wired into GitHub CI**. Run it manually when you touch `shared/taxonomy/`. Edit the YAML, never the generated JSON.

## Conventions

- **Layer placement is a review gate.** The PR template asks which layer a new file belongs to (`domain` / `application` / `infrastructure` / `interfaces` / `frontend` / `scripts`) and why. Adding a DB table/field requires a migration note; changing an API contract must be called out.
- **Import boundaries are aspirational, not enforced.** `domain/` is meant to have zero external deps, but `domain/novel/value_objects/story_phase.py` already imports from `engine.core.entities.story` (a domain→upper-layer inversion). Don't add more. (The previously-cited example, `domain/engine/dag/repositories/dag_version_repository.py`, was deleted in `577202c4`.)
- `docs/ARCHITECTURE.md` is itself out of date (its `domain/` tree omits `cast/`, `character/`, `evolution/`, `engine/`, `structure/`, `worldbuilding/`; its `application/` tree lists 9 of ~30 packages). Trust the code.
- Tests mirror the layer structure: `tests/unit/{application,domain,engine,infrastructure,interfaces}/`. CI runs only `pytest tests/unit -q --tb=short` with placeholder API keys.
- Frontend uses the `@/` alias for `frontend/src/`. Vite manual chunks: `naive-ui`, `echarts`, `vue-runtime`, `vendor`. The `/api` proxy sets `timeout: 0` for SSE — keep it.

## Environment Variables

Copy `.env.example` to `.env` (loaded by `load_env.py` / `python-dotenv`) and set at least one LLM key.

| Variable | Notes |
|---|---|
| `LLM_PROVIDER` | Optional default provider |
| `ANTHROPIC_API_KEY` / `ARK_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | At least one required |
| `<PROVIDER>_BASE_URL` / `<PROVIDER>_MODEL` | Per-provider endpoint and model |
| `EMBEDDING_SERVICE` | `openai` (default) or `local` (needs `requirements-local.txt`) |
| `EMBEDDING_API_KEY` / `_BASE_URL` / `_MODEL` | Falls back to `OPENAI_API_KEY` when empty |
| `EMBEDDING_MODEL_PATH` / `EMBEDDING_USE_GPU` | Local model path / GPU toggle |
| `VECTOR_STORE_TYPE` | `chromadb` (default) or `qdrant` |
| `PLOTPILOT_USE_STORY_PIPELINE` | `off`/`legacy` rolls back to legacy writing |
| `PLOTPILOT_PROD_DATA_DIR` | Overrides the data directory (legacy alias `AITEXT_PROD_DATA_DIR` still works); `PLOTPILOT_FORCE_PROD_DATA` forces production mode |
| `PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES` | `1` bypasses the write queue — tests set this; don't use it in app code |
| `PLOTPILOT_DEFAULT_CHAPTERS` / `PLOTPILOT_DEFAULT_WORDS_PER_CHAPTER` / `PLOTPILOT_SUMMARY_MAX_CHAPTERS` | Default planning knobs |
| `DISABLE_AUTO_DAEMON` | `1` = do not auto-start the daemon when the API boots |
| `LLM_PROVIDER` / `CORS_ORIGINS` / `LOG_LEVEL` / `LOG_FILE` | Read from code, not shown in `.env.example` (`LLM_PROVIDER` in `infrastructure/ai/llm_environment.py`, `CORS_ORIGINS` in `interfaces/api/settings.py`) |

## Data

- SQLite: `data/plotpilot.db` (path resolved by `application.paths.DATA_DIR`; `get_db_path()` falls back to the legacy `aitext.db`)
- Vector store: `data/chromadb/`
- Logs: `logs/plotpilot.log`
- `.env` is gitignored. Never commit `data/`, `logs/`, databases, or secrets — see the security section in README.md.

### ⚠ 开发阶段：**老数据问题一律不处理**

用户 2026-11-03 定的规矩：**还在开发阶段 ⇒ 存量数据没有价值 ⇒ 凡是「历史数据引发的问题」都不修**。

| 类别 | 处置 |
|---|---|
| **老数据问题**（迁移 / 回填 / 兼容层 / 清理旧行 / 把老分数重算成新口径 / 历史索引被清空后重跑） | **不做** —— 发现就在台账里**登记一行**即可，不写实现 |
| **代码缺陷**（影响**现在**的行为：解析器不剥围栏、缓存键缺内容身份、判定口径不对） | **照做** |
| **产品口味**（要用户拍板的） | 列出来等决定 |

⇒ 推论：**不要**为了「让老数据也正确」而给新逻辑加兼容分支 —— 那会让新口径长期背着旧包袱。
⇒ 例外：若某条「老数据」已经**影响当下**的正确性（例如旧分数与新分数同列会让面板显示错），那是**展示层口径问题**（算代码缺陷），不是老数据问题。

## Runtime gotchas

- **Don't run `python interfaces/main.py`** — it binds `0.0.0.0:8000` and conflicts with nothing but confuses the 8005 default.
- The API can **auto-start the autopilot daemon** at boot; use `DISABLE_AUTO_DAEMON=1` when you want a clean API-only process.
- `application/engine/services/autopilot_daemon.py` is a **deprecated shim** emitting `DeprecationWarning`; it re-delegates into `engine.runtime.*`. Same for `engine/application/`. Write new code against `engine/runtime/`.
- The repo root is a package (`__init__.py` + `__main__.py`), so both `import application.x` and `PlotPilot.application.x` resolve — importing the same module under two names creates duplicate module state. Prefer the top-level form used by the file you're editing.
- Always call `.validate_output(...)` on gateway renders rather than trusting raw LLM JSON — that is what the `PromptContract.output_schema` is for.
- Windows is the primary dev platform (`tools/plotpilot.bat` launcher, Tauri desktop, `SIGBREAK` handler in `interfaces/main.py`).
