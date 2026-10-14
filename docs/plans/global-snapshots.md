---
kind: plan
lang: zh
---

# 全局不可变 Snapshot 与手动发布（Issue #23）

业务合同来自 [Issue #23](https://github.com/nanzhi84/SEAL/issues/23)，
[唯一可视化](../generated/global-snapshots.html) 随本计划更新。
本合同替换 [结果服务 Plan](results-api.md) 的内部当前版本、水位分页与重启撤权行为；
HTTP 路由、认证、字段白名单、归档检查和资源限制继续沿用。
当前状态：实现完成，fresh 独立端到端验收411 PASS / 0 FAIL；未部署生产。

## 目标、职责与数据流

```text
collect / recheck / worker → Observation → Version → Resource.current_version_id
  → 操作者 snapshot create → 冻结 Source 入口 + Resource URL + 固定 Version 映射
  → snapshot inspect → 人工核对 → snapshot publish
  → 单一 published_snapshot_id → HTTP 当前许可 → 固定 Snapshot → Version.result_ref
  → S3 完整 CrawlResult / 原始附件 → 原有白名单内容 / 下载字节
```

采集链路不依赖发布模块。Snapshot 只存关系及必要来源元数据，不复制 SDK JSON 或附件。
快照不是采集恢复 checkpoint，也不证明覆盖完整性或业务质量。
init、升级、采集、复查、worker、serve 均不创建或发布快照。
本次不提供删除、对象 GC、自动发布、逐来源发布或质量审核。

## 数据与事务合同

追加 `0013_global_snapshots.sql`，不更改历史 SQL。
历史 schema 并无 `seal.snapshot` 表；旧 snapshot_id 是原文引用字段，保留历史记录。
若外部自建同名表造成冲突，迁移整笔回滚，禁止覆盖。

- `seal.snapshot` 保存 UUID、创建时间、来源/资源/版本数量及 sealed 标志。
- `seal.snapshot_source` 固定 Source id、Frontier 归属与 entry_urls，包括暂无成功版本的来源。
- `seal.snapshot_item` 固定 Source、Frontier、Resource、Version 与公开 Resource URL。
- 复合外键验证来源→Frontier→Resource→Version 的真实归属，阻止映射错配及引用被删除。
- `seal.publication` 只有一个 singleton 行，初始 published_snapshot_id 为 NULL。
- `seal.snapshot_publication` 以递增 sequence 保存切换前后 ID 和实际切换时间。

创建在单一 REPEATABLE READ 事务中执行，不锁采集资源、不等待在途 Run。
固定首个数据库视图中的全部来源和已提交 current_version_id；未成功资源不入映射，
最近复查失败仍采用之前成功版本。事务内先写映射和统计再 sealed，事务外从未暴露草稿。
数据库延迟约束禁止未 sealed 快照提交；sealed 后标题、来源、映射和发布记录禁止增删改。
映射写入与封存通过快照行锁串行，统计与实际映射必须一致。任何失败回滚整个创建。

发布先校验快照已封存及其结果和附件可读且哈希正确，只读取既有对象。
然后锁唯一 publication 行，原子更新指针并追加日志；相同 ID 幂等，无重复日志。
日志时间在取得行锁后生成，sequence 与真实切换顺序一致。并发发布串行化；
无效 ID、坏对象、数据库或 S3 故障均不改指针。归档此后仍可能损坏，API 每次独立检查。
校验不构成业务质量验收；只能由显式 publish 命令切换。空快照无引用对象，无归档读取。

## CLI 与有界输出

```sh
seal snapshot create
seal snapshot list --limit 20 --after '<上一页最后 ID>'
seal snapshot inspect '<snapshot_id>' --limit 20 --after '<上一页最后 Resource ID>'
seal snapshot publish '<snapshot_id>'
```

create 返回 snapshot_id、时间与三个统计，不隐含发布。
list 按 id 的 C 字节序分页，inspect 按 Resource.id 分页；limit 为1–100，默认20。
inspect 来源同样独立分页（`--source-after`），固定映射与来源入口供人工核对。
返回 published_snapshot_id、items/sources 及 next_after/source_next_after，不输出完整 Source
配置、凭据或原始结果。来源入口总大小和单页 JSON 设上限；不存在/格式非法 ID 明确报错。
publish 返回 before_snapshot_id、published_snapshot_id、changed 与 published_at。
重复发布不产生采集、快照、映射、对象副本或额外切换日志。

## HTTP、分页、附件与当前权限

新请求在同一只读事务读取一次发布指针。无发布时，认证通过后的三个数据接口统一503
snapshot_not_published，不回退内部数据。发布空快照后 /sources 和 /results 返回正常空集合；
空快照中未登记入口仍404 source_not_found。已有但无成功资源的来源列出入口且结果为 []。
歧义判定只用所选快照的全部入口，/sources 排除歧义入口，/results 为409 source_ambiguous。

/sources 使用 Snapshot 来源，/results 使用固定映射；每次许可来自当前私有配置。
服务每个请求重读并严格校验配置，仅允许 callers 热更新；token_key、服务URL、限额等
启动合同变化需重启，未重启时503 invalid_api_config。配置原子替换即可撤权，
无来源许可403；当前 Key 被移除则401。无效配置拒绝请求，不继续用旧许可。

游标 Fernet 认证加密绑定用途、Snapshot、调用方、Source、最后 Resource.id 与首次
issued_at；3600秒期限不因翻页延长。后续翻页解析快照内原入口及映射，不重读当前指针
选择内容；发布或回切后仍沿原快照，权限实时验证。跨调用方、入口、快照篡改拒绝。

附件令牌绑定用途、Snapshot、调用方、Source、Resource、Version、附件索引；有效86400秒。
下载只认该快照真实成员，实时授权并复算结果及附件哈希；切换发布不使旧合法链接失效。
不加入新的公开响应字段。旧内部水位游标与旧附件令牌不兼容，无双读或 shim。

## 升级、保留与回滚

0012 的已有内部内容、来源、历史表、对象均保留。0013 是追加迁移，可对有内容库执行
`seal init`；0012 及更早的归档合同升级仍保留原空内容约束。
升级后指针为空，操作者须 create → inspect → publish 才对外提供内容。
部署前停服务及 worker，升级程序与 schema 后再启动；服务启动不代做迁移或发布。
生产升级按现有运维要求备份；日常隔离库不强制额外备份。
回滚先停止新服务；可回退旧代码并保留0013追加表和对象（旧程序只检查0012已登记）。
如需恢复升级前 schema，使用运维保存的升级前副本；不得删除 Snapshot 引用对象或版本。
旧 API 会暴露内部当前版本，回滚前须确认这一发布语义变化。

## 实施与验收

先完成本合同，再实现迁移与操作模块、CLI、API 查询/令牌和实时许可，更新 README 两图、
CHANGELOG 和本可视化。按 [AGENTS](../../AGENTS.md) 由完全 fresh 子 agent 只根据业务目标
在根 tests/ 编写独立验收，隔离 PostgreSQL17 与 MinIO，通过真实 CLI、HTTP、SQL 和 S3。
失败边界在测试代码前冻结；主 agent 不补单元测试。

验收覆盖 Issue 九项：初始化/升级不发布；A创建与发布；内部变化和来源漂移；B与回切；
历史游标/附件；即时撤权和篡改；一致创建与并发/幂等/失败发布；空/歧义/坏归档；
SQL、HTTP、原始附件字节及无复制/无采集写入对照。
根 work/ 与 artifacts/ 保存命令、前提、断言、映射、发布日志、对象清单与哈希，均不入库。
静态检查使用 CI Ruff 命令和 `seiso check`。生产部署与负载不在本次验收范围。

### 最终独立验收（2026-10-14）

完全 fresh 子 agent 依据业务目标独立编写验收；最终完整运行411 PASS / 0 FAIL、退出0。
使用真实 CLI 采集 HTML/PDF、暂停中的采集、queued collect → worker --once，以及
SELECT/默认只读数据库与仅 S3 Get/List 的 HTTP 服务；同时用合成持久输入覆盖全部边界。
升级使用前一版 `994c8adfebbe9facdd3e74a964f89d32e91fe74d` 的实际 init/collect，
再以本版 init 升级，逐行对照历史字段、S3对象和原始PDF字节，不以手造旧 schema 代替。

```sh
uv sync --frozen
uv run --no-sync python tests/issue23_e2e.py
uvx --from ruff==0.16.10 ruff check src
uvx --from ruff==0.16.10 ruff format --check src
seiso check
```

环境与数据前提：Python3.12、Docker、`postgres:17-alpine`、
`seal-minio:RELEASE.2025-10-15T17-29-55Z` 及
`minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727`。
Git对象库须包含上述旧提交；数据、端口与凭据自动随机生成，不读取现有本地库、桶或
root minio/。当前测试源码入库，运行数据和工件不入库；复跑生成新运行名。

| 业务合同 | 实际结果 |
| --- | --- |
| 初始化/升级，无发布边界 | 不自动创建或发布，三个接口503；空快照及空来源有确定结果 |
| A创建、发布、内部变化与B创建 | A来源/版本/附件与SQL及S3一致；变化、新来源、失败复查和B创建均不改变A |
| B发布及回切A | 新请求对应固定B，回切恢复A；内部较新版本仍保留 |
| 原分页/附件、实时权限与篡改 | 原A分页集合无漏项/重复，原字节跨B及回切保持；即时撤权及跨调用方/成员篡改拒绝 |
| 并发一致性与幂等 | 实际在途采集不等待整轮；8次来源入口/双版本事务并发创建一致；12次并发发布日志与指针一致，无请求混合A/B |
| 异常保护 | 无效ID、对象损坏/缺失与S3停机发布失败，原指针和日志不变；API坏对象报错且不回退内部版本 |
| 无采集/对象副作用 | 创建/发布无S3副本；HTTP全业务表与对象清单前后相同；队列Worker新增成功Version但不创建/发布快照 |

最终工件根目录为 `artifacts/issue23-20261014-150910-8244e0/`：
[汇总](../../artifacts/issue23-20261014-150910-8244e0/summary.json)、
[独立审计](../../artifacts/issue23-20261014-150910-8244e0/audit.json)、
[全部断言](../../artifacts/issue23-20261014-150910-8244e0/assertions.json)、
[命令回执](../../artifacts/issue23-20261014-150910-8244e0/commands.json)、
[环境与文件哈希](../../artifacts/issue23-20261014-150910-8244e0/environment.json)、
[最终SQL映射](../../artifacts/issue23-20261014-150910-8244e0/final-sql-mappings.json)、
[并发创建](../../artifacts/issue23-20261014-150910-8244e0/consistent-concurrent-create.json)、
[发布中HTTP对照](../../artifacts/issue23-20261014-150910-8244e0/http-during-publication.json)、
[发布记录](../../artifacts/issue23-20261014-150910-8244e0/publication-records.json)、
[升级](../../artifacts/issue23-20261014-150910-8244e0/upgrade.json)、
[队列Worker](../../artifacts/issue23-20261014-150910-8244e0/queued-worker.json)。
各 `sql-api-*.json` 保存固定版本、完整公开内容及附件SHA对照。

28个源码、13个迁移、13个测试/辅助输入的受测哈希与最终工作区相同；凭据和不透明
令牌匹配扫描均为0；两个临时容器均已清理。此前失败轮次仅为测试夹具错误，保留历史
工件，不改写原结果。
验收汇总SHA-256：`95168fe9e847510c57dcfbd7f0796fa66d7a08660ca35677d17536e4b896a2ea`；
独立审计SHA-256：`cb216bc2ac7e367795cfbd8f15ff20a6e22a543c55cd211fddaf63e31ca1f3ca`。

本地Ruff、格式、diff检查与seiso全部通过；架构检查未发现新循环依赖或采集→发布依赖。
生产部署、公网来源可用性及生产负载未验证；TTL使用真实签名的过期令牌测试，未等待
完整1小时/24小时壁钟。快照内容仍需人工业务核对，不保证全站覆盖或质量。

## 运行示例：从公开来源采集到 API 查询

以下示例在仓库根目录执行，演示公开来源抓取、存储、人工发布和只读 API 查询。
来源及资源数沿用历史示例，不是本次隔离验收结果；重跑需核对当前站点和运行回执。
全局快照冻结全部已入库来源，本示例只给四个来源的调用许可。

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
| [dd-017 · 最高法指导案例](../reference/尽职调查核验入口地图.md#dd-017)   | [指导案例](https://www.court.gov.cn/shenpan/gengduo/77.html)       | [dd-017.yaml](../../sources/dd-017.yaml) |            547 |
| [dd-040 · 最高检公报](../reference/尽职调查核验入口地图.md#dd-040)       | [最高检公报](https://www.spp.gov.cn/spp/gjgb/index.shtml)          | [dd-040.yaml](../../sources/dd-040.yaml) |             31 |
| [dd-041 · 最高检指导性案例](../reference/尽职调查核验入口地图.md#dd-041) | [指导性案例](https://www.spp.gov.cn/spp/jczdal/index.shtml)        | [dd-041.yaml](../../sources/dd-041.yaml) |             31 |
| [dd-448 · 电影备案公示](../reference/尽职调查核验入口地图.md#dd-448)     | [电影备案立项公示](https://www.chinafilm.gov.cn/xxgk/gsxx/dybalx/) | [dd-448.yaml](../../sources/dd-448.yaml) |             16 |

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

完成采集与存储核验后冻结全局集合；create 不影响对外内容。inspect 分页核对来源、
资源和固定 Version，操作者自行判断内容是否适合发布。尚无发布指针时 API 返回503。

```bash
uv run --no-sync seal snapshot create | tee artifacts/readme-demo/snapshot.json
demo_snapshot_id=$(uv run --no-sync python -c 'import json; print(json.load(open("artifacts/readme-demo/snapshot.json"))["snapshot_id"])')
uv run --no-sync seal snapshot inspect "$demo_snapshot_id" --limit 100
# 按 next_after / source_next_after 继续核对剩余映射，再显式发布
uv run --no-sync seal snapshot publish "$demo_snapshot_id"
```

再次采集或创建快照不会改变对外集合；必须再次 publish 才更新。回切使用同一 publish
命令传入旧 Snapshot ID。内部成功资源数、complete 与存储核验不代表全站覆盖或业务质量。

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
