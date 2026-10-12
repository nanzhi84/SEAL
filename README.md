# SEAL

SEAL 从公开来源采集信息、归档原文，并将 HTML、文本 PDF、DOCX、XLS/XLSX、CSV、TXT、JSON 和图片保存为统一 Document。内容仅包含 `article`、`image`、`table`、`json`、`document_ref` 五类节点。PDF 要求文本层，不使用 OCR；JSON 保留原始值。

```text
Source YAML → Recipe / Binding / Run → Scrapy 发现与下载
  → Archive bytes / Snapshot / Observation → 格式 Parser
  → Document / Revision / Version / Emission → PostgreSQL SQL / COPY
```

Recipe 负责来源访问、分页、表单、链接与解析配置；Runtime 统一归档、解析、校验和保存。解析失败保留原文与错误，仍可继续发现。相同输入和规则复用 Version，各 Run 独立保存观察血缘；成功 Run 推进当前文档指针。在线 Recheck 检查变化，后台队列支持有限重试、超时、并发隔离与中断恢复。

## 快速开始

需要 Python 3.12、PostgreSQL 17、`uv`；旧库升级另需 `pg_dump`、`pg_restore`。

```sh
uv sync --frozen
export SEAL_DATABASE_URL='postgresql://localhost/seal'
export SEAL_ARCHIVE="$PWD/.seal"
umask 077
uv run --frozen seal init
# 编辑 examples/source.yaml：设置公开范围、预算、Recipe，并确认 archive_approved
uv run --frozen seal collect examples/source.yaml
uv run --frozen seal recheck examples/source.yaml
```

配置中的 `recipe.path` 相对 YAML 所在目录解析。Source、不可变 Recipe/Binding 和 Run 自动注册，无需手动管理 ID。仅 `init` 修改数据库 Schema。

CLI 只有四个命令：`init`、`collect source.yaml`、`recheck source.yaml`、`worker`。执行命令可用 `--enqueue` 入队，`worker` 消费持久任务；周期采集由外部 Cron 调用。入队回执表示 pending，最终状态以 Run 为准。

```sh
uv run --frozen seal collect examples/source.yaml --enqueue
uv run --frozen seal worker
```

前台 stderr 为 TTY 时显示轻量实时面板；`--no-progress` 可关闭。Known URL progress 仅反映当前已知集合，队列耗尽不代表全站完整。stdout 最终输出单份 JSON；成功退出 0，Run 失败 1，参数/操作错误 2，中断 130。

## 数据访问与升级

自建数据位于 PostgreSQL `seal` Schema，第三方队列保持 `public.procrastinate_*`。当前、历史与指定 Run 的文档通过 SQL 查询；批量导出使用 PostgreSQL 原生 COPY。原文在私有 Archive，以 Snapshot/body hash 引用；数据库导出不包含原文文件。

```sql
SELECT d.id, d.url, v.content
FROM seal.document d
JOIN seal.document_version v ON v.id = d.latest_document_version;

COPY (SELECT id, content FROM seal.document_version) TO STDOUT;
```

0.2.0 为破坏性升级：移除 Record、字段提取/Locator、Replay、Inspect/Export 和旧 CLI。旧库运行 `seal init` 会先返回对象/行数清单；停止所有数据库客户端、确认备份与目标后，显式执行 `seal init --backup /private/before.dump --acknowledge-data-loss`。迁移删除 Record 历史数据，保留 Document、Run 与 Archive；回滚依赖完整备份恢复。不要将认证凭据放入配置、URL、代码或工件。

详细合同与验收范围见 [Document-only Plan](docs/plans/unified-document-pipeline.md) 和 [Generated](docs/generated/unified-document-pipeline.html)。独立子 agent 按真实用户路径验收，规则见 [AGENTS](AGENTS.md)；CI 只做静态检查。发布与迁移历史见 [CHANGELOG](CHANGELOG.md)。
