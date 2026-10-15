# SEAL

SEAL 以 Crawl4AI 采集来源并维护 URL 资源版本库。每个 Source 拥有独立 Frontier，输入或显式 PDF 文字合同变化且目标有效、解析成功时追加不可变 Version；私有 MinIO 仅保存完整 CrawlResult JSON 与原始下载附件，PostgreSQL 保存来源、运行、资源版本和对象引用。下游通过 result_ref 读取并重建本轮结果，文本原始 HTTP 字节不再独立归档。

当前支持 HTML/TXT、文本层 PDF 与动态 DOM；使用 `init`、`collect`、`recheck`、`worker` 操作采集，`snapshot create / inspect / publish` 控制全局人工发布；`serve` 从已发布 Snapshot 提供来源发现、成功内容和原始附件。来源配置采用[地图稳定编号](docs/reference/尽职调查核验入口地图.md)，接口与配置设计详见[全局 Snapshot 合同](docs/plans/global-snapshots.md)。

## 全链路执行图

```text
[0] ENTRY & DISPATCH
cli.py: main() → dispatch(args)
├── seal serve → api.py: serve() → 私有配置、S3桶与当前schema校验 → 进入阶段 [5]
├── [CHECK] 采集命令校验 S3 桶与当前 schema；snapshot 命令校验 schema
├── seal init → migration.py: initialize()
│   ├── [CHECK] S3 桶可用后再查数据库对象清单、历史升级前停止客户端与校验备份
│   ├── [DB] migrations/schema.sql → 顺序执行未登记的0002–0013，校验已登记摘要；旧归档升级仅接受空内容，0012→0013保留全部内容
│   └── [DB] Procrastinate schema；对象桶由部署初始化；新/升级发布指针为空
├── seal snapshot create / list / inspect / publish → snapshots.py → 进入阶段 [4]
├── seal worker [--once] → queue.py: worker()
│   ├── recover_stalled() / recover_unqueued() → 任务心跳恢复与补入队
│   └── [ASYNC] Procrastinate → crawl_job() → 进入阶段 [2]
└── seal collect / recheck source.yaml → configuration.py: prepare_run()
    └── 进入阶段 [1]

[1] PREPARE
configuration.py: prepare_run()
├── config.py: load_file() → [CHECK] 闭合 Source/Crawl4AI YAML、归档许可与范围
├── [DB] Source 行锁 → 配置代际/config_hash → Source 唯一 Frontier
├── runs.py: create_run()
│   ├── [CHECK] 暂停/冷却；collect 使用入口，recheck.py: plan_recheck() 冻结到期资源
│   └── [DB] Source run_seq → Run 不可变配置快照、seeds、deadline
└── [CHECK] 执行方式
    ├── --enqueue → queue.py: enqueue() 事务内入队 → pending JSON
    └── foreground → progress.py: ProgressPanel → 进入阶段 [2]

[2] EXECUTE
runs.py: execute_run()
├── [DB] Run 与 Source session advisory lock；同 Frontier 单执行者
├── [LOOP] _execute_attempt() → begin_attempt()
│   ├── [CHECK] 冻结config_hash、generation、write_seq、deadline、max_attempts
│   ├── [CHECK/DB] 同Run保留checkpoint；新Run替换前校验未交付附件，将证据留原Run.report；重建visited
│   ├── [DB] epoch递增、status=running
│   └── [SPAWN] 独立进程组 python -m seal.crawl；SDK缓存/log隔离到 work 的 run/epoch目录；输出不泄露到CLI
├── [PARALLEL]
│   ├── CHILD: crawl.py: run()
│   │   ├── SDK AsyncWebCrawler；collect: BFSDeepCrawlStrategy / recheck: arun_many
│   │   ├── sdk.py: HttpAdapter / BrowserAdapter
│   │   │   ├── [CHECK] scope、robots、重定向/子请求、次数/时间/字节预算
│   │   │   │   └── robots_redirect_urls仅许可robots政策获取的精确跳转，不扩大内容范围
│   │   │   ├── [IO] HTTP bytes / Browser DOM → 内容指纹与临时输入；原始附件 → S3 CAS + checkpoint引用
│   │   │   └── InputScraper → SDK HTML/TXT或同次PDF bytes在 work/ 的临时副本解析；不再请求另一份输入
│   │   ├── [LOOP] streaming CrawlResult → outcome() → archive.py: serialize_result() → 结果JSON CAS
│   │   │   └── resources.py: persist() → 进入阶段 [3]
│   │   ├── State.checkpoint_changed() → 补当前层未交付URL → SDK pending持久化
│   │   └── [STATE] status=finishing；watch_parent()父亡终止子进程组
│   └── PARENT: 无写锁轮询owner连接、generation/epoch/暂停/预算；stderr TTY只读进度
├── [CLEANUP] stop_process() → SIGTERM / 有界等待 / SIGKILL
├── [ERROR] 前台Ctrl+C / Worker显式cancel_event → 子进程停止、checkpoint保留、Run=retryable
├── [RETRY] 进程失败retryable，有界attempt恢复；Worker按30秒心跳窗口回收，旧epoch不能提交
└── completion.py: finish_run() → manifest.py: terminal_report()
    ├── [STATE] complete / failed；资源已各自独立提交
    ├── [DB] 运行统计/错误/配置留关系库；覆盖/质量始终有界，无manifest对象
    └── cli.py: main() → stdout单份JSON（collect/recheck为receipt.v3）；退出0/1/2，中断退出130

[3] RESPONSE & VERSION COMMIT
resources.py: persist()
├── [IO/CHECK] 结果JSON与全部原始附件可读取、SHA256复算；验证后才引用
├── [DB] Source → Run → Resource锁；fenced()检查代际/epoch/状态
├── [CHECK] frontier_id+URL唯一资源；run+resource交付幂等
├── [DB] 不可变Observation：状态/模式/输入指纹/可空附件引用/result_ref/实际URL
├── [DB/IO] Observation.result_ref → 本次成功/失败SDK结果CAS；同事务关联
├── [CHECK] 失败、登录或挑战 → 保留实际SDK结果和已取得附件；不生成内容版本
├── [CHECK] 返回结果前中断/异常 → checkpoint或失败Observation，result_ref可空，不伪造结果
├── [CHECK] 解析成功且输入body_hash与PDF文字合同等于当前成功版本 → Observation关联旧Version
└── [CHECK] 首次解析成功或输入body_hash/PDF文字合同变化且解析成功
    ├── [DB] 全局事务锁保护稳定增量cursor；连续Version+result_ref（与原始Observation一致）
    ├── [DB] 复合FK保护Resource/Observation/Version归属；推进current_version_id
    └── [DB] next_check → recheck；current_resource_version / version_delta → 下游SQL取result_ref → MinIO读取CrawlResult

[4] SNAPSHOT & MANUAL PUBLICATION
snapshots.py: create() / listing() / inspect() / publish()
├── create() → [DB] REPEATABLE READ事务冻结全局来源入口、Resource URL与当前成功Version
│   ├── [DB] snapshot_source / snapshot_item复合FK验证来源、资源、版本归属
│   └── [STATE] 统计核对后sealed；提交后快照和映射不可变；不改变发布指针
├── listing() / inspect() → [DB] 只读快照统计、发布指针和有界分页映射
└── publish() → verify() → [IO/CHECK] 原结果/附件读取并复算SHA256；不复制对象
    ├── [DB] 唯一publication行锁 → 原子指针切换 + 不可变snapshot_publication记录
    ├── [CHECK] 已发布当前ID幂等；无效快照/坏归档/故障失败保留原指针
    └── [RETURN] 切换前后ID、顺序与时间；可切回任意旧Snapshot，内部新Version保留

[5] READ-ONLY RESULTS API
api.py: application() → HTTP /sources、/results 或 /attachments/{id}
├── [CHECK] Service.authenticate() → 每请求重读私有callers → Bearer摘要与实时Source许可
├── [DB] result_query.py: 同一只读事务读取一次published_snapshot_id；为空503 snapshot_not_published
├── [DB] /sources → 获授权Snapshot来源的无歧义入口 → 下游选择source_url
├── [DB] /results → Snapshot入口与固定snapshot_item → Version.result_ref
│   └── [CHECK] 加密游标绑定Snapshot/Caller/Source/C序位置/首次时间；续页保持原快照及期限
├── [IO/CHECK] Objects.get() → 结果/原始附件读取、大小/超时上限、SHA256复算
├── [RETURN] public_results.py: 白名单 → JSON数组 + Link下一页；坏归档整页报错
└── [CHECK/IO] 加密附件ID → 实时Caller许可 + Snapshot/Resource/Version成员资格 → 原始字节
    └── 发布切换仍可读原快照；只读，不采集、不新增对象
```

## 数据模型 ER 图

```mermaid
erDiagram
 SOURCE ||--|| FRONTIER : owns
 SOURCE ||--o{ RUN : schedules
 FRONTIER ||--o{ RESOURCE : contains
 RESOURCE ||--o{ OBSERVATION : observes
 RUN ||--o{ OBSERVATION : delivers
 RESOURCE ||--o{ VERSION : versions
 OBSERVATION ||--o| VERSION : originates
 VERSION o|--o{ OBSERVATION : reused_by
 SNAPSHOT ||--o{ SNAPSHOT_SOURCE : freezes
 SOURCE ||--o{ SNAPSHOT_SOURCE : referenced_by
 SNAPSHOT_SOURCE ||--o{ SNAPSHOT_ITEM : contains
 RESOURCE ||--o{ SNAPSHOT_ITEM : frozen_resource
 VERSION ||--o{ SNAPSHOT_ITEM : fixed_version
 SNAPSHOT o|--o| PUBLICATION : published
 SNAPSHOT ||--o{ SNAPSHOT_PUBLICATION : switch_target
 SNAPSHOT {
  text id PK
  timestamptz created_at
  boolean sealed
  bigint source_count
  bigint resource_count
  bigint version_count
 }
 SNAPSHOT_SOURCE {
  text snapshot_id PK,FK
  text source_id PK,FK
  text frontier_id FK
  jsonb entry_urls "冻结登记入口"
 }
 SNAPSHOT_ITEM {
  text snapshot_id PK,FK
  text source_id FK
  text frontier_id FK
  text resource_id PK,FK
  text version_id FK
  text url "冻结Resource URL"
 }
 PUBLICATION {
  boolean singleton PK
  text published_snapshot_id FK "可空，只有人工切换"
 }
 SNAPSHOT_PUBLICATION {
  bigint sequence PK
  text before_snapshot_id FK
  text after_snapshot_id FK
  timestamptz published_at
 }
 RESOURCE {
  text id PK
  text frontier_id FK
  text url "来源内URL唯一，保留语义查询参数"
  text current_version_id FK
  timestamptz next_check
 }
 SOURCE {
  text id PK "地图dd-xxx"
  jsonb config
  bigint generation
  bigint write_seq
 }
 FRONTIER {
  text id PK
  text source_id FK,UK
  text run_id FK
  jsonb checkpoint "SDK状态与未交付窗口"
 }
 RUN {
  text id PK
  text source_id FK
  jsonb config
  text config_hash
  text status
  int attempt_epoch
 }
 OBSERVATION {
  text id PK
  text resource_id FK
  text run_id FK
  text delivery_key UK
  text body_hash "输入SHA256，文本不能当对象读取"
  text archive_ref "原始附件SHA256，可空"
  text_array attachment_refs "已取得原始文件引用，无结果时仍可查询"
  text result_ref "本次SDK结果SHA256，可空"
  text input_mode "http或rendered_dom"
  text outcome
  text version_id FK
 }
 VERSION {
  text id PK
  text resource_id FK
  int sequence "来源URL内连续"
  text observation_id FK
  bigint cursor UK "稳定内容增量"
  text body_hash "版本输入SHA256"
  text input_mode "http或rendered_dom"
  text result_ref "MinIO完整SDK结果SHA256"
  text framework_version
  jsonb parser_config
 }
```

## 运行示例：从公开来源采集到 API 查询

以下示例在仓库根目录执行，演示从公开来源抓取、存储持久化、人工发布到只读 API 查询的完整流程：

```text
sources/dd-xxx.yaml → seal collect (抓取与解析)
  → PostgreSQL (元数据与版本) + MinIO (结果 JSON 与附件)
  → seal snapshot create → inspect → publish (人工发布全局集合)
  → seal serve (只读 API) → /sources 发现来源 → /results 查询内容
```

### 1. 选择验证来源

选用 4 个采用轻量 HTTP 模式（无需浏览器环境）的已跑通来源作为示例：

| Source / 来源地图                                                          | 公开入口                                                           | 配置文件                           | 历史成功资源数 |
| :------------------------------------------------------------------------- | :----------------------------------------------------------------- | :--------------------------------- | -------------: |
| [dd-017 · 最高法指导案例](docs/reference/尽职调查核验入口地图.md#dd-017)   | [指导案例](https://www.court.gov.cn/shenpan/gengduo/77.html)       | [dd-017.yaml](sources/dd-017.yaml) |            547 |
| [dd-040 · 最高检公报](docs/reference/尽职调查核验入口地图.md#dd-040)       | [最高检公报](https://www.spp.gov.cn/spp/gjgb/index.shtml)          | [dd-040.yaml](sources/dd-040.yaml) |             31 |
| [dd-041 · 最高检指导性案例](docs/reference/尽职调查核验入口地图.md#dd-041) | [指导性案例](https://www.spp.gov.cn/spp/jczdal/index.shtml)        | [dd-041.yaml](sources/dd-041.yaml) |             31 |
| [dd-448 · 电影备案公示](docs/reference/尽职调查核验入口地图.md#dd-448)     | [电影备案立项公示](https://www.chinafilm.gov.cn/xxgk/gsxx/dybalx/) | [dd-448.yaml](sources/dd-448.yaml) |             16 |

> 注：成功资源数为历史实测参考值，重跑结果受站点当前状态与配置预算影响。

### 2. 准备本地存储与运行环境

启动本地 PostgreSQL 与 MinIO 容器，并初始化数据库 schema：

```bash
uv sync --frozen
uv run --no-sync python -m seal.local_storage

# 导出数据库与 S3 配置
set -a
source .env.local
set +a

# 初始化 schema 并创建临时目录
uv run --no-sync seal init
mkdir -p work/readme-demo artifacts/readme-demo
```

> 注：对象读取统一走 S3 API，不要直接操作根目录 `minio/`。

### 3. 执行采集与入库验证

在前台执行采集任务，回执 JSON 保存至 `artifacts/readme-demo/`：

```bash
set -o pipefail
uv run --no-sync seal collect sources/dd-017.yaml | tee artifacts/readme-demo/dd-017-run.json
uv run --no-sync seal collect sources/dd-040.yaml | tee artifacts/readme-demo/dd-040-run.json
uv run --no-sync seal collect sources/dd-041.yaml | tee artifacts/readme-demo/dd-041-run.json
uv run --no-sync seal collect sources/dd-448.yaml | tee artifacts/readme-demo/dd-448-run.json
```

核验关系库状态，并通过 S3 API 真实拉取结果对象以验证连通性与 SHA-256 CAS：

```bash
uv run --no-sync python - <<'PY' | tee artifacts/readme-demo/storage-check.json
import json
from seal.db import connect
from seal.objects import Objects

ids = ["dd-017", "dd-040", "dd-041", "dd-448"]
with connect() as conn:
    conn.execute("SET TRANSACTION READ ONLY")
    rows = conn.execute("""
        SELECT s.id,
          (SELECT status FROM seal.run WHERE source_id=s.id
           ORDER BY created_at DESC LIMIT 1) AS latest_run_status,
          (SELECT count(*) FROM seal.frontier f
           JOIN seal.resource r ON r.frontier_id=f.id
           WHERE f.source_id=s.id AND r.current_version_id IS NOT NULL)
           AS successful_resources
        FROM seal.source s WHERE s.id=ANY(%s) ORDER BY s.id
    """, (ids,)).fetchall()
    sample = conn.execute("""
        SELECT r.url, v.result_ref FROM seal.frontier f
        JOIN seal.resource r ON r.frontier_id=f.id
        JOIN seal.version v ON v.id=r.current_version_id
        WHERE f.source_id='dd-017' ORDER BY r.id LIMIT 1
    """).fetchone()

assert {row["id"] for row in rows} == set(ids), "source_not_registered"
assert all(row["latest_run_status"] == "complete" for row in rows), "latest_run_not_complete"
assert all(row["successful_resources"] > 0 for row in rows), "source_has_no_successful_content"
assert sample is not None, "no_successful_version"

# 通过 S3 API 读取对象并复算校验 SHA-256
raw = Objects().get(sample["result_ref"])
archive = json.loads(raw)
assert isinstance(archive["result"], dict), "invalid_result_archive"
print(json.dumps({"sources": rows, "archive_sample": {
    "url": sample["url"], "result_ref": sample["result_ref"],
    "bytes": len(raw), "archive_verified": True
}}, ensure_ascii=False, indent=2))
PY
```

### 4. 创建、核对和手动发布

完成采集与存储核验后冻结全局集合；create 不影响对外内容。inspect 分页核对来源、资源和固定 Version，操作者自行判断内容是否适合发布。尚无发布指针时 API 返回 503。

```bash
uv run --no-sync seal snapshot create | tee artifacts/readme-demo/snapshot.json
demo_snapshot_id=$(uv run --no-sync python -c 'import json; print(json.load(open("artifacts/readme-demo/snapshot.json"))["snapshot_id"])')
uv run --no-sync seal snapshot inspect "$demo_snapshot_id" --limit 100
# 按 next_after / source_next_after 继续核对剩余映射，再显式发布
uv run --no-sync seal snapshot publish "$demo_snapshot_id"
```

再次采集或创建快照不会改变对外集合；必须再次 publish 才更新。回切使用同一 publish 命令传入旧 Snapshot ID。内部成功资源数、complete 与存储核验不代表全站覆盖或业务质量。

### 5. 生成配置并启动 API 服务

`seal serve` 是只读 HTTP 服务。生成测试授权配置与 Bearer 请求头后启动服务：

```bash
uv run --no-sync python - <<'PY'
import hashlib, json, os, secrets
from pathlib import Path
from cryptography.fernet import Fernet

root = Path("work/readme-demo")
root.mkdir(parents=True, exist_ok=True)
key = secrets.token_urlsafe(32)
config = {
    "public_base_url": "http://127.0.0.1:8090",
    "token_key": Fernet.generate_key().decode(),
    "callers": [{
        "id": "demo",
        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "sources": ["dd-017", "dd-040", "dd-041", "dd-448"]
    }]
}
for name, body in (
    ("api-config.local.json", json.dumps(config, indent=2)),
    ("headers.local.txt", "Authorization: Bearer " + key + "\n")
):
    path = root / name
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(body)
    path.chmod(0o600)
PY

# 在当前终端启动服务（保持运行）
uv run --no-sync seal serve \
  --config work/readme-demo/api-config.local.json --host 127.0.0.1 --port 8090
```

### 6. 客户端查询与结果消费

另开一个终端进入项目根目录，调用 API 查询可用来源及解析内容：

```bash
mkdir -p artifacts/readme-demo

# 1. 发现已发布且获授权的来源
curl --fail-with-body --silent --show-error http://127.0.0.1:8090/sources \
  --header @work/readme-demo/headers.local.txt \
  --output artifacts/readme-demo/sources.json
cat artifacts/readme-demo/sources.json

# 2. 按来源入口 URL 查询已发布 Snapshot 的固定成功内容（取前 5 条）
query_source() {
  curl --fail-with-body --silent --show-error --get http://127.0.0.1:8090/results \
    --header @work/readme-demo/headers.local.txt \
    --data-urlencode "source_url=$2" --data-urlencode 'limit=5' \
    --dump-header "artifacts/readme-demo/$1-headers.txt" \
    --output "artifacts/readme-demo/$1-results.json"
}

query_source dd-017 'https://www.court.gov.cn/shenpan/gengduo/77.html'
query_source dd-040 'https://www.spp.gov.cn/spp/gjgb/index.shtml'
query_source dd-041 'https://www.spp.gov.cn/spp/jczdal/index.shtml'
query_source dd-448 'https://www.chinafilm.gov.cn/xxgk/gsxx/dybalx/'

cat artifacts/readme-demo/dd-017-results.json
```

- `/sources` 返回快照内获当前授权的来源 ID 及冻结入口；
- `/results` 返回规范化内容数组（包含 `markdown`、`metadata`、`attachments` 等），响应头 `Link` 包含下一页游标。

#### 停止服务与清理

在服务终端按 `Ctrl+C` 停止 API。停止本地存储容器（保留数据）：

```bash
docker compose --env-file .env.local down
```
