# SEAL

**SEAL (Self-Evolving Agentic Ingestion Loop)** 是一个面向多源异构信息（政企官网、行业平台、法律法规与公告等）的企业级自动化采集、原文不可变归档与结构化提取系统。

项目旨在解决复杂网络环境下的采集可靠性、原始证据不可篡改归档、多格式文档归一化解析，以及任务调度与离线重放等核心工程挑战。

---

## 核心功能

### 1. 统一文档归一化解析（Unified Document Pipeline）
- **多源异构格式支持**：原生支持 HTML（列表/详情页）、PDF（文本层）、DOCX、XLS/XLSX、CSV、TXT、JSON 及图片附件。
- **标准化内容模型**：将异构内容归一化为五类核心节点：
  - `article`：整篇正文内容与层级结构；
  - `table`：结构化二维表格；
  - `image`：原始图片及其在原文中的位置；
  - `json`：完整的 JSON 对象、数组或标量值；
  - `document_ref`：关联同类型文档的外键引用（共享引用且不冗余存储）。
- **业务结构化提取**：可在统一文档基础上按需配置字段抽取（如标题、正文、发布时间等），支持精确定位（Locator）与数据清洗。

### 2. 种子驱动的站点发现与采集（Seeded Site Collection）
- **自动递归发现**：从指定业务种子（Seed URL）出发，自动发现同站点入口、Sitemap、栏目、分页与正文链接。
- **原生调度与去重**：依托 Scrapy 调度引擎与请求指纹进行高效请求去重，支持深度控制与总预算管理。
- **智能抽取与兜底**：优先匹配精确规则（Specific Rules）；对于未设规则的 HTML 页面，采用通用 Fallback 解析器智能提取正文与元数据。
- **规范政策遵守**：支持针对目标站点的 `robots.txt` 规则审计与预算管控。

### 3. 不可变内容寻址归档与零网络重放（CAS Archive & Replay）
- **原始证据保真**：网络响应按原始字节（bytes）直接写入私有内容寻址存储（Content-Addressed Storage, CAS），建立不可变快照（Snapshot）与观察记录（Observation）。
- **零网络离线重放**：当解析规则迭代或代码更新时，可基于已归档的原文执行离线重放（Replay），无需重新发起网络请求，避免对源站造成额外压力。
- **敏感凭据防护**：在原始响应写入归档前，系统自动拦截并拒绝包含敏感凭据（如 Token、CSRF 等）的非公开内容，确保归档安全。

### 4. 生产级持久任务与智能调度（Persistent Runtime & Worker）
- **持久化队列**：基于 PostgreSQL 与 Procrastinate 构建的高可靠后台任务系统。
- **自动分批复查（Scheduled Recheck）**：仅针对到期目标分批发起复查，未计划的目标不盲目爬取；支持独立的故障退避与重试机制。
- **世代隔离与并发安全**：采用 Generation Fencing 机制，防止旧进程或过期任务覆盖新数据。

### 5. 可信 Recipe 与确定性血缘管理
- **不可变代码封装**：采集与解析逻辑由团队审查的可信 Python 脚本（Recipe）驱动，不执行未经审查的动态外部代码。
- **确定性绑定（Binding）**：数据源（Source）、Recipe 版本快照与执行参数（Params）组合形成唯一 Binding，保证每次运行环境和逻辑完全可重现。

---

## 架构概览

系统采用清晰的三平面逻辑架构：

- **Runtime 平面**：负责任务调度、页面抓取、原文归档、文档解析、数据校验与故障重放（当前核心）。
- **Evaluation 平面**：针对运行结果与归档原文进行独立的质量评估与对齐（规划中）。
- **Management 平面**：负责数据源治理、Recipe 版本管理、审核与发布流程（规划中）。

### 数据处理链路

```text
  [数据源配置 / Seed URLs]
             │
             ▼
   Scrapy Crawl & Fetch  ──────►  [私有内容寻址归档 (CAS)]
             │                               │
             ▼                               ▼ (支持离线 Replay)
     文档与结构化解析引擎  ◄──────────────────┘
             │
             ▼
   PostgreSQL 持久化存储  (统一文档 / Record / 任务状态)
             │
             ▼
   [CLI 工具 / 导出结果]  (inspect / export)
```

---

## 快速开始

### 1. 环境准备

- **Python**：3.12+（使用 `uv` 管理依赖）
- **PostgreSQL**：17+

```bash
# 同步并安装依赖
uv sync --frozen

# 配置核心环境变量
export SEAL_DATABASE_URL="postgresql://localhost/seal"
export SEAL_ARCHIVE="$PWD/.seal"

# 初始化/迁移数据库
uv run --frozen seal db migrate
```

### 2. 标准采集工作流

从配置到完成一次端到端文档采集：

```bash
# 1. 注册数据源（包含允许访问的域名、路径及预算等）
uv run --frozen seal source apply examples/source.yaml

# 2. 打包解析 Recipe 并生成不可变版本摘要
uv run --frozen seal recipe pack recipes/documents

# 3. 关联数据源、Recipe 与运行参数，创建执行绑定 (Binding)
uv run --frozen seal binding create SOURCE_ID --recipe RECIPE_DIGEST --params examples/params.yaml

# 4. 执行采集任务
uv run --frozen seal collect SOURCE_ID --binding BINDING_ID

# 5. 查看任务运行状态与结果
uv run --frozen seal inspect run RUN_ID

# 6. 导出统一文档或解析数据
uv run --frozen seal export SOURCE_ID --documents --run RUN_ID --output documents.json
```

### 3. 后台队列与常驻 Worker

如需启用生产级后台异步调度：

```bash
# 将采集任务推入持久化队列
uv run --frozen seal run SOURCE_ID --binding BINDING_ID --enqueue

# 启动常驻 Worker 进程消费队列任务
uv run --frozen seal worker

# 触发到期目标的定时调度检查
uv run --frozen seal schedule
```

---

## 常用 CLI 命令速查

SEAL 提供了功能完备的命令行工具（`seal`）：

| 命令分组 | 常用子命令 | 说明 |
| :--- | :--- | :--- |
| **配置与绑定** | `seal source apply <file>` | 应用或更新数据源定义 |
| | `seal recipe pack <dir>` | 校验并打包解析 Recipe，生成版本指纹 |
| | `seal binding create <src> ...` | 创建数据源、Recipe 与参数的不可变绑定 |
| | `seal source select <src> ...` | 设置数据源的默认调度版本 |
| **采集与调度** | `seal collect <src> --binding <id>` | 启动同步采集（支持整站种子扩展） |
| | `seal run <src> [--enqueue]` | 执行单次运行（支持排队模式） |
| | `seal worker` | 启动持久队列 Worker 监听与消费任务 |
| | `seal schedule` | 触发定时检查到期任务 |
| **检查与导出** | `seal inspect run <id>` | 查看任务执行概要、耗时与错误 |
| | `seal inspect document <id>` | 检查统一文档内容节点与元数据 |
| | `seal inspect snapshot <hash>` | 检索归档的原始网络响应快照 |
| | `seal export <src> [--documents]` | 导出指定 Run 或最新的结构化数据/文档 |
| **重试与恢复** | `seal replay <run_id> --binding <id>` | 使用归档的原文进行离线零网络重放 |
| | `seal retry <run_id>` | 重试暂时失败的任务 |
| | `seal pause <src> / seal recover` | 暂停数据源采集或从异常故障中恢复任务 |

---

## 安全与运行规范

1. **私有存储权限**：原文归档目录保存网络原始请求与响应，运行前需确保设置私有访问权限（如 `umask 077`），避免数据意外暴露。
2. **凭据安全**：禁止在 Source 配置、代码或公开工件中存放认证凭据；敏感 Token 需通过安全环境变量传递。
3. **环境隔离**：建议生产运行始终携带 `--frozen` 锁死依赖包，避免依赖浮动导致 Recipe 摘要失效。

---

## 文档与相关资源

- **版本发布与更新日志**：详见 [CHANGELOG.md](CHANGELOG.md)（记录各版本详细变更、数据库迁移指引与升级兼容性）。
- **设计规划文档 (Plans)**：
  - [统一文档流水线设计 Plan](docs/plans/unified-document-pipeline.md)
  - [种子站点采集设计 Plan](docs/plans/v1.3-seeded-site-collection.md)
  - [异构来源扩展设计 Plan](docs/plans/v1.2-heterogeneous-sources.md)
  - [Runtime 运行时设计 Plan](docs/plans/v1.1-runtime.md)
- **架构决策记录 (ADR)**：
  - [ADR-0004: 原生 Scrapy 种子站点采集](docs/adr/0004-seeded-collection-native-scrapy.md)
  - [ADR-0003: Runtime / Evaluation / Management 三平面边界划分](docs/adr/0003-runtime-evaluation-management.md)
- **应用案例与演示**：
  - [法院公开数据采集演示](experiments/court-demo/演示.md)
