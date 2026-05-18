# Any Auto Register 项目分析文档

本文档基于当前仓库代码结构生成，用于快速理解项目定位、技术栈、核心模块、业务流程和扩展方式。

## 1. 项目定位

Any Auto Register 是一个多平台账号自动注册与管理系统。项目围绕 AI 平台账号注册、账号有效性检测、Token 刷新、代理池、邮箱服务、验证码服务、接码服务和账号导出构建，提供 Web UI、Docker 部署和 Electron 桌面壳。

当前 README 中描述的主要能力包括：

- 支持 ChatGPT、Cursor、Kiro、Grok、Windsurf、Trae、Tavily、Cerebras、Blink、OpenBlockLabs 等平台。
- 支持协议注册、无头浏览器注册、有头浏览器注册 3 种执行模式。
- 支持邮箱、验证码、接码、代理等 provider 插件化配置。
- 支持账号生命周期检测、注册任务日志、统计面板和多格式导出。
- 支持 Any2API、CPA、Sub2API、Kiro-Go 等外部系统联动。

## 2. 技术栈

| 层级 | 技术 |
| --- | --- |
| 后端服务 | FastAPI、Uvicorn |
| 后端数据 | SQLite、SQLModel、SQLAlchemy |
| 注册执行 | curl_cffi、requests、Playwright、Patchright、Camoufox |
| 验证码 Solver | Quart、Camoufox、本地 Solver 服务 |
| 前端 | React 19、TypeScript、Vite、Tailwind CSS、Radix UI、Lucide React |
| 桌面端 | Electron、electron-builder、electron-updater |
| 测试 | pytest、httpx |
| 部署 | Docker、docker-compose、Xvfb、x11vnc、noVNC |

## 3. 顶层目录结构

| 路径 | 作用 |
| --- | --- |
| `main.py` | FastAPI 主入口，注册 API 路由、静态前端和生命周期任务。 |
| `api/` | HTTP 路由层，负责请求参数、响应和异常映射。 |
| `application/` | 应用服务层，承载账号、任务、配置、统计等业务用例。 |
| `domain/` | 领域数据结构，主要是账号、任务、平台能力、代理等 dataclass。 |
| `infrastructure/` | 数据访问层，封装 SQLModel 查询和配置持久化。 |
| `core/` | 核心能力层，包含平台注册表、数据库模型、执行器、账号图谱、认证、生命周期等。 |
| `core/registration/` | 注册流程抽象，统一协议邮箱、浏览器、OAuth 注册流程。 |
| `platforms/` | 平台插件，每个平台通过 `plugin.py` 注册到平台 registry。 |
| `providers/` | provider 插件，包括 mailbox、captcha、sms、proxy。 |
| `services/` | 后台服务，如任务运行时、Solver 管理、Turnstile Solver。 |
| `frontend/` | React Web UI。 |
| `electron/` | Electron 桌面端壳与打包配置。 |
| `customer_portal_api/` | 独立客户门户 API 服务。 |
| `tests/` | pytest 测试集合，覆盖 API、provider、平台任务和恢复逻辑。 |
| `tools/` | HAR 抓包与分析辅助工具。 |

## 4. 后端架构

后端采用分层结构：

```text
api -> application -> domain / infrastructure -> core -> platforms / providers
```

### 4.1 FastAPI 入口

`main.py` 创建 `FastAPI(title="Account Manager", version="2.0.0")`，启动时执行：

1. 初始化数据库：`init_db()`。
2. 自动加载平台插件：`core.registry.load_all()`。
3. 自动加载 provider 插件：`providers.registry.load_all()`。
4. 启动调度器：`core.scheduler.scheduler.start()`。
5. 启动任务运行时：`services.task_runtime.task_runtime.start()`。
6. 启动验证码 Solver 管理器：`services.solver_manager.start_async()`。
7. 启动生命周期管理器：`core.lifecycle.lifecycle_manager.start()`。

关闭时依次停止生命周期管理器、调度器、任务运行时和 Solver 管理器。

### 4.2 API 路由

`main.py` 将以下路由挂载到 `/api` 前缀：

| 模块 | 主要职责 |
| --- | --- |
| `api/accounts.py` | 账号增删改查、导入、导出、统计。 |
| `api/account_checks.py` | 账号有效性检测任务。 |
| `api/actions.py` | 平台扩展操作。 |
| `api/auth.py` | Web UI 访问密码认证。 |
| `api/config.py` | 系统配置读写。 |
| `api/health.py` | 健康检查。 |
| `api/lifecycle.py` | 账号生命周期相关接口。 |
| `api/platforms.py` | 平台列表和平台动作。 |
| `api/platform_capabilities.py` | 平台能力配置。 |
| `api/provider_definitions.py` | provider 定义。 |
| `api/provider_settings.py` | provider 运行配置。 |
| `api/proxies.py` | 代理池管理。 |
| `api/sms.py` | 接码服务相关接口。 |
| `api/stats.py` | 注册与账号统计。 |
| `api/tasks.py` | 任务列表、详情和事件查询。 |
| `api/task_commands.py` | 创建、取消任务等命令。 |
| `api/task_logs.py` | 历史任务日志。 |
| `api/system.py` | 系统状态与版本信息。 |

### 4.3 数据模型

核心数据库模型集中在 `core/db.py`，默认使用 SQLite。数据库地址由 `ACCOUNT_MANAGER_DATABASE_URL` 控制，未设置时使用仓库根目录下的 `account_manager.db`。

主要表：

| 表 | 说明 |
| --- | --- |
| `accounts` | 账号主表，保存平台、邮箱、密码、用户 ID 等基础字段。 |
| `account_overviews` | 账号生命周期、有效性、套餐状态和展示摘要。 |
| `account_credentials` | 账号凭据，支持多 scope、多 provider、多 key。 |
| `provider_accounts` | provider 账号信息，例如邮箱服务账号。 |
| `provider_resources` | provider 资源信息，例如具体邮箱地址。 |
| `provider_definitions` | provider 定义元数据。 |
| `provider_settings` | provider 配置、认证信息和默认启用状态。 |
| `platform_capability_overrides` | 平台能力覆盖配置。 |
| `tasks` | 持久化任务状态。 |
| `task_events` | 任务事件和实时日志。 |
| `task_logs` | 注册结果历史日志。 |
| `proxies` | 代理池记录和成功/失败计数。 |

`init_db()` 还承担历史 schema 迁移、provider key 迁移、空配置清理和账号图谱同步。

## 5. 核心业务流程

### 5.1 注册任务流程

注册任务由 `application/tasks.py` 持久化，并由 `services/task_runtime.py` 轮询执行。

```text
前端提交注册参数
  -> api/task_commands.py 创建 register task
  -> application.tasks.create_register_task 写入 tasks 表
  -> TaskRuntime 轮询 pending 任务
  -> claim_next_runnable_task 抢占任务
  -> execute_task 分派到 _execute_register_task
  -> 构建平台实例、邮箱 provider、代理和验证码 solver
  -> platform.register 执行注册
  -> save_account 保存账号图谱
  -> 写入 task_events、task_logs
  -> 可选自动推送 Any2API / CPA
```

任务运行时具备以下限制：

- 全局默认最多并行 3 个任务。
- 单个平台默认最多并行 1 个任务。
- 对账号检测和平台动作任务，会用 account key 避免同一账号并发操作。
- 服务重启后，未完成任务会被标记为 `interrupted`。

### 5.2 平台注册流程

平台基类是 `core/base_platform.py` 中的 `BasePlatform`。每个平台实现自己的 `plugin.py`，通过 `@register` 注册。

`BasePlatform.register()` 根据执行器和身份模式分派到 3 类流程：

| 流程 | 类 | 适用场景 |
| --- | --- | --- |
| 协议邮箱注册 | `ProtocolMailboxFlow` | 使用邮箱 provider 收验证码或验证链接，直接调用目标平台接口。 |
| 浏览器注册 | `BrowserRegistrationFlow` | 使用 Playwright/Patchright/Camoufox 自动化页面注册。 |
| 协议 OAuth | `ProtocolOAuthFlow` | 复用浏览器 OAuth 会话完成登录或注册。 |

`core/registration/flows.py` 统一处理：

- preflight 校验。
- 邮箱身份校验。
- OAuth 执行器限制。
- 验证码 solver 创建。
- OTP callback 和 verification link callback。
- 手机验证 callback 与清理。
- 注册结果到统一 `RegistrationResult` 的映射。

### 5.3 provider 流程

provider 注册表位于 `providers/registry.py`，按类型分为：

- `mailbox`
- `captcha`
- `sms`
- `proxy`

provider 模块通过 `register_provider(provider_type, driver_type)` 注册。运行时根据数据库中的 provider definition 和 setting 创建实例。

邮箱创建入口是 `core/base_mailbox.py:create_mailbox()`，它会：

1. 检查 provider 是否存在且启用。
2. 从 provider settings 解析运行时配置。
3. 根据 driver type 找到工厂函数。
4. 支持 fallback mailbox，主 provider 失败后尝试其他启用 provider。
5. 把 provider account 和 provider resource 信息写入账号图谱。

### 5.4 账号生命周期与检测

账号检测由 `application/tasks.py` 中的 `_run_single_account_check()` 执行：

1. 从数据库加载账号。
2. 根据账号平台创建平台插件实例。
3. 调用平台 `check_valid(account)`。
4. 将检测结果写回 `account_overviews`。
5. 若账号有效，尝试恢复 lifecycle status。

批量检测通过 `TASK_TYPE_ACCOUNT_CHECK_ALL` 顺序执行，支持平台过滤和数量限制。

## 6. 前端架构

前端位于 `frontend/`，基于 React + Vite。入口文件为 `frontend/src/main.tsx` 和 `frontend/src/App.tsx`。

### 6.1 页面路由

`App.tsx` 使用 `react-router-dom` 定义页面：

| 路径 | 页面 | 作用 |
| --- | --- | --- |
| `/` | `Dashboard` | 总览和统计。 |
| `/history` | `TaskHistory` | 任务历史和日志。 |
| `/accounts` | `Accounts` | 全部账号。 |
| `/accounts/:platform` | `Accounts` | 指定平台账号。 |
| `/register` | `Register` | 创建注册任务。 |
| `/proxies` | `Proxies` | 代理管理。 |
| `/settings` | `SettingsPage` | 通用、注册、邮箱、验证、接码、代理、ChatGPT、高级等设置。 |

### 6.2 API 调用

`frontend/src/lib/utils.ts` 中定义：

- `API = import.meta.env.VITE_API_BASE || '/api'`
- `apiFetch()`：自动加 JSON header 和 Bearer Token。
- `apiDownload()`：处理导出文件下载。
- `getAuthToken()` / `setAuthToken()`：基于 localStorage 保存 Web UI token。

当后端要求访问密码时，前端会先访问 `/auth/check`，未认证则展示登录页。

## 7. 插件扩展机制

### 7.1 新增平台

新增平台通常需要：

1. 在 `platforms/<platform_name>/` 下创建模块。
2. 编写 `plugin.py`，继承 `BasePlatform`。
3. 使用 `@register` 注册平台类。
4. 声明 `name`、`display_name`、`supported_executors`、`supported_identity_modes`、`capabilities`。
5. 至少实现 `check_valid()`。
6. 按需要实现 `build_protocol_mailbox_adapter()`、`build_browser_registration_adapter()` 或 `build_protocol_oauth_adapter()`。
7. 如需平台动作，实现 `execute_action()` 或标准 capability handler。

平台插件在服务启动时由 `core.registry.load_all()` 自动扫描 `platforms/*/plugin.py`。

### 7.2 新增 provider

新增 provider 通常需要：

1. 在 `providers/<type>/` 下创建实现模块。
2. 继承对应基类，例如 mailbox provider 继承 `BaseMailbox`。
3. 用 `register_provider("<type>", "<driver_type>")` 注册。
4. 在 provider definition 中配置 driver type、字段、认证模式和默认值。
5. 在设置页启用并配置 provider。

provider 在服务启动时由 `providers.registry.load_all()` 自动扫描。

## 8. 部署与运行

### 8.1 本地后端

```bash
pip install -r requirements.txt
python main.py
```

默认监听：

- Backend / Web UI: `http://0.0.0.0:8000`
- API 前缀：`/api`

### 8.2 前端开发

```bash
cd frontend
npm install
npm run dev
```

如需指定后端地址，可以通过 `VITE_API_BASE` 配置。

### 8.3 Docker

`docker-compose.yml` 暴露：

| 端口 | 作用 |
| --- | --- |
| `8000` | FastAPI 和 Web UI。 |
| `6080` | noVNC，有头浏览器预览。 |
| `8889` | Turnstile Solver。 |

默认挂载：

```text
./data:/app/data
```

容器内数据库使用：

```text
sqlite:////app/data/account_manager.db
```

### 8.4 Electron

`electron/package.json` 提供：

- `npm run dev`：启动 Electron。
- `npm run build:backend`：构建后端。
- `npm run build:mac` / `build:win` / `build:linux`：按平台打包。
- `npm run build:all`：全平台打包。

## 9. 测试覆盖

项目使用 pytest，配置位于 `pytest.ini`：

```ini
[pytest]
testpaths = tests
pythonpath = .
```

测试目录覆盖重点包括：

- API 健康检查、账号、平台、代理、统计、生命周期。
- Any2API 同步。
- ChatGPT OAuth 参数要求。
- HeroSMS、SMS provider、代理 provider。
- Windsurf 平台行为。
- 注册手机回调和有效性恢复。

运行方式：

```bash
pytest
```

## 10. 关键设计特点

### 10.1 插件化

平台和 provider 都通过自动扫描加注册表完成加载。核心逻辑不直接硬编码具体平台或服务，业务扩展主要落在 `platforms/` 和 `providers/`。

### 10.2 任务持久化

注册、检测、平台动作都被抽象为持久化任务，任务状态和事件存入数据库。这样可以让前端查询历史、查看日志，并在服务重启后明确标记中断状态。

### 10.3 账号图谱

账号不只保存邮箱和密码，还拆分保存 overview、credentials、provider accounts、provider resources。这个设计适合表达一个平台账号背后的邮箱资源、验证码资源、Token、套餐信息和外部系统关联。

### 10.4 多执行器

同一平台可以同时支持：

- `protocol`：速度快，依赖协议实现和验证码能力。
- `headless`：适合需要浏览器但不需要人工观察的流程。
- `headed`：适合调试、人工介入或复杂风控页面。

### 10.5 可观测性

任务执行会写入 `task_events` 和 `task_logs`。前端可按任务拉取事件，后端也会打印 task 日志，便于排查注册失败、provider 异常和代理问题。

## 11. 维护注意事项

- 修改平台能力时，需要关注 `platform_capability_overrides` 的初始化和合并逻辑。
- 修改 provider key 或 auth mode 时，需要同步迁移逻辑，避免旧数据库升级失败。
- 新增注册流程时，优先复用 `core/registration/` 中的 flow 和 adapter。
- 新增任务类型时，需要同时更新任务创建、执行分派、序列化和前端展示。
- 涉及浏览器自动化时，需要同时考虑本地运行、Docker 的 Xvfb/noVNC 和 PyInstaller 打包。
- 涉及中文内容时，应保持 UTF-8 编码，避免从终端复制乱码文本。

## 12. 快速阅读路线

第一次接手项目，建议按以下顺序阅读：

1. `README.md`：了解产品能力和使用方式。
2. `main.py`：了解服务启动和路由挂载。
3. `core/db.py`：了解数据模型和迁移逻辑。
4. `application/tasks.py`：了解注册、检测和平台动作任务。
5. `services/task_runtime.py`：了解任务调度执行。
6. `core/base_platform.py`：了解平台插件抽象。
7. `core/registration/flows.py`：了解注册流程抽象。
8. `providers/registry.py` 和 `core/base_mailbox.py`：了解 provider 创建与 fallback。
9. `platforms/chatgpt/plugin.py`：阅读一个完整平台插件样例。
10. `frontend/src/App.tsx`：了解前端页面结构。

