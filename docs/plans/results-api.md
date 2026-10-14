---
kind: plan
lang: zh
---

# 只读结果服务（Issue #22）

目标与范围依据 [Issue #22](https://github.com/nanzhi84/SEAL/issues/22)。
[同步可视化](../generated/results-api.html) 展示同一合同。
仅消费已有 Source、Version 与 MinIO 对象，不启动采集、不增加结果副本、不迁移数据库。
当前状态：实现完成，独立端到端验收161 PASS / 0 FAIL；尚未部署生产。

## 边界与数据流

```text
Bearer Key → 调用方与来源许可 → 登记入口唯一匹配
  → Source → Frontier → Resource.current_version_id → Version.result_ref
  → Objects（S3 原始字节与 SHA-256 校验）→ 严格字段白名单
  → JSON 数组、Link 下一页 / 绑定结果的附件下载
```

HTTP 层仅处理认证、参数、限流和响应；查询层只读事务选择内容；转换层验证归档并
输出对外模型；认证模块封装配置和不透明令牌。采集层不依赖服务层。
现有 aiohttp 提供服务，显式依赖 cryptography 的 Fernet，复用成熟的认证加密算法，
不自制加密协议。新入口 `seal serve --config <私有 JSON> --host 127.0.0.1 --port 8080`。
init、collect、recheck、worker 保持原合同。serve 不初始化或修改数据库、对象桶。

## HTTP 与内容合同

`GET /results?source_url=<登记入口>&limit=20&cursor=<不透明游标>`。
成功响应直接为数组，每项固定包含 url、markdown、metadata、links、media、tables、
attachments。认证先于查询；入口通过既有 public_url 规范化匹配 config.entry_urls，
不解析 DNS、不请求网站。同域名不同入口仍为不同来源；重复入口报歧义。

字段合同根据安装的 Crawl4AI 0.9.4 models.py、content_scraping_strategy.py、
table_extraction.py 与当前 12 份成功原生归档的字段类型核对；归档样本的媒体和表格为空，
这些集合的合同以当前 SDK 的内容模型为依据；独立验收已通过真实 HTML 采集产生非空
images、videos、audios 和 tables，并逐字段核对公开结果与原生 SDK 归档一致。

| 字段 | 固定白名单与类型 |
| --- | --- |
| url | Resource URL，字符串 |
| markdown | 仅 markdown.raw_markdown，字符串；无 markdown 时为空字符串 |
| metadata | title、author、description、keywords：字符串或 null，缺失为 null |
| links | internal、external：数组；每项 href、text、title：字符串或 null |
| media | images、videos、audios：数组；每项 src、alt、desc、type、format：字符串或 null；width：非负整数或 null；SDK 数字字符串显式转换为整数 |
| tables | 数组；每项 headers：字符串数组，rows：字符串二维数组，caption、summary：字符串或 null |
| attachments | 数组；仅外层 attachment_refs；每项 url：本服务绝对下载地址，name、media_type：null |

空集合为 []，不存在的标量为 null。未知字段移除，白名单字段类型不合法则整个请求
失败（archive_invalid），不跳过项目。正文不清洗、不改链接、不补提取。媒体 score、
group_id、data、表格 metadata、depth、parent_url、HTML、诊断和配置不对外返回。
当前归档没有可靠原始文件名和 MIME 元数据，因此附件固定使用通用下载，不推断名称
或类型。网页上的未下载文件链接仍只存在于 markdown/links。

## 当前成功与分页

第一页在同一 REPEATABLE READ、READ ONLY 事务中选取 current_version_id，并记录
该来源已提交成功 Version 的最大 cursor（水位）。按 Resource.id 的 C 字节序升序，
最多取 limit+1 项，limit 默认20，范围1–100。
后续页面按同一来源、水位和最后 Resource.id 查找水位之前各 Resource 的最后成功
Version，等价于首次查询时的成功版本快照。新资源首次成功及旧资源的新版本只在重新
从第一页查询时出现；快照不会因采集并发更新漏项或重复。最近失败与 unchanged 不推进
成功 Version，从未成功的资源排除。不可变 Version 和全局 cursor 提交锁复用既有合同。
历史数据或来源被外部删除不在快照保证范围内。

响应头 `Link: <绝对下一页地址>; rel="next"`，最后一页没有 Link。
游标通过 Fernet 认证加密，绑定用途、调用方、来源、水位、位置和首次签发时间，
有效期3600秒，不因翻页刷新；limit 可在合法范围内调整。跨来源/调用方或篡改均400
invalid_cursor。入口配置改变后再次匹配入口和许可，不绕过当前授权。

## 附件与授权

`GET /attachments/<不透明 ID>` 使用同一 Bearer 权限。
ID 为 Fernet 认证加密令牌，绑定用途、Source、Version 及外层附件列表的位置，有效期
86400秒。每次下载重新验证 Source 许可、SQL Version 归属、result_ref、外层列表及
对象哈希；只读已有原始字节。最近版本变化不使旧令牌的原始附件失效。
ID 不是对象哈希，无法读取内部版本标识；无附件映射表或磁盘物化副本。
未授权来源403；伪造、篡改、过期或不存在 ID 为404 attachment_not_found。
下载固定 application/octet-stream、attachment; filename="attachment"、nosniff。
API 响应 Cache-Control: no-store，使用配置的 public_base_url，不信任 Host/转发头。

服务私有配置通过 --config 或 SEAL_API_CONFIG 读取，字段严格校验：

```json
{
  "public_base_url": "https://api.example.com",
  "token_key": "<Fernet.generate_key() 产生的独立密钥>",
  "callers": [
    {"id": "consumer-a", "key_sha256": "<随机 API Key 的 SHA-256>", "sources": ["dd-017"]}
  ],
  "workers": 4,
  "request_timeout_seconds": 30,
  "result_max_bytes": 16777216,
  "attachment_max_bytes": 52428800,
  "page_max_bytes": 67108864
}
```

默认值如上，workers 1–8，超时1–60秒。
result_max_bytes 范围1024–67108864，attachment_max_bytes 范围1024–52428800，
page_max_bytes 范围1024–134217728，均为整数，超出配置范围拒绝启动。
Key 应为至少32字节随机值；配置只保存摘要，HTTP 常数时间匹配摘要，禁止通配来源授权。
配置和密钥不入库、不打印；轮换/撤权需要
重启服务，替换 token_key 使已有令牌失效。数据库复用 SEAL_DATABASE_URL；对象存储
复用 SEAL_S3_*，部署使用只读 DB 用户与限定桶的 S3 Get/List 权限。
对外部署由反向代理提供 HTTPS，默认仅监听 loopback。

### 本地启动与调用

先按已有采集 Plan 初始化数据库、配置 SEAL_DATABASE_URL 与 SEAL_S3_*，并完成至少
一次来源采集；serve 不代做这些步骤。下面只为 dd-017 创建一个本地调用方，实际使用
前将 sources 改为明确允许的已登记来源。文件以0600写入，已有文件不会覆盖，Key不输出：

```sh
uv sync --frozen
uv run --no-sync python - <<'PY'
import hashlib, json, os, secrets
from pathlib import Path
from cryptography.fernet import Fernet
root = Path("work/results-api")
root.mkdir(parents=True, exist_ok=True, mode=0o700)
key = secrets.token_urlsafe(32)
config = {"public_base_url": "http://127.0.0.1:8080",+          "token_key": Fernet.generate_key().decode(),
          "callers": [{"id": "local-consumer",+                       "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                       "sources": ["dd-017"]}]}
for name, body in (("config.local.json", json.dumps(config)),
                   ("headers.local.txt", "Authorization: Bearer " + key + "\n")):
    with os.fdopen(os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(body)
PY
uv run --no-sync seal serve --config work/results-api/config.local.json --port 8080
```

另一个终端请求（先将占位入口替换为 dd-017 登记入口）：

```sh
curl --silent --show-error --get 'http://127.0.0.1:8080/results' \
  --header @work/results-api/headers.local.txt \
  --data-urlencode 'source_url=<已登记入口 URL>' \
  --data-urlencode 'limit=20' --dump-header work/results-api/response-headers.txt
```

读取 Link 响应头中的下一页地址，使用同一认证头继续请求；返回的附件 URL 同样需要
此认证头。配置仅保存 Key 摘要，headers.local.txt 保存原始调用凭据，二者均不入库。

## 错误与资源限制

错误统一 `{"error":"稳定代码"}`，不包含输入、凭据、路径或堆栈。

| 状态 | 错误码与条件 |
| --- | --- |
| 400 | invalid_parameters、invalid_cursor |
| 401 | unauthenticated；带 WWW-Authenticate: Bearer |
| 403 | source_forbidden |
| 404 | source_not_found、attachment_not_found、not_found |
| 405 | method_not_allowed |
| 409 | source_ambiguous |
| 500 | archive_corrupt（哈希不符）、archive_invalid（JSON/信封/字段/引用不合法）、archive_missing（已有引用的对象缺失）、archive_too_large（对象或页超限）、internal_error |
| 503 | database_unavailable、object_storage_unavailable、request_timeout、server_busy |

数据库连接5秒，语句15秒、锁5秒；S3连接3秒、读取10秒、最多2次。
同步 SQL/S3 在固定 workers 线程内执行，最多 workers 个进行中请求，等候槽位1秒。
每个请求顺序读取对象（没有无界对象任务）；整页转换/JSON大小及单对象读取均设上限。
超时/客户端取消后仍在运行的线程占用槽位至实际完成，避免取消产生无限后台工作。
HTTP 与排队期限同时到达时，根据当前阶段区分错误：尚未取得槽位返回 server_busy，
已执行查询或归档读取返回 request_timeout，不能由计时器触发顺序决定错误码。
对象按块读取并在块间检查截止时间，关闭 Body；Hash校验仍通过既有 Objects。
日志仅包含固定路由名称、HTTP状态、稳定错误码和异常类别，不记录 URL、令牌或 Key。

## 实施与验收

实施顺序：固定合同 → 认证和公开模型 → 只读查询 → HTTP/CLI入口 → 独立端到端验收
→ 同步文档和静态检查。完全 fresh 子 agent 仅依据业务合同写根 tests/ 验收，
使用隔离 PostgreSQL/MinIO 与合成来源，不使用当前本地库/桶。测试提前列出失败方式，
稳定输入保存在 [验收边界清单](../../tests/issue22_failure_boundaries.json)；
主 agent 只实现/修复产品代码，不补单元测试。

验收涵盖真实 HTTP→SQL→S3→数组，入口/深度/失败/unchanged/版本变化，跨页并发更新，
空/未知/歧义来源，参数、认证、越权、游标/附件篡改，附件字节一致，缺失/损坏/依赖故障，
只读副作用与有界超时。工件记录命令、环境前提、断言、脱敏响应和代码哈希；放根
work/、artifacts/，不入库。

2026-10-14 最终独立验收：161 PASS / 0 FAIL，105个真实HTTP请求，命令退出0。
验收使用 DB SELECT/默认只读与 S3 Get/List 账号；全部临时容器已清理。
源码与测试哈希对照无漂移，工件随机凭据扫描通过。

```sh
uv sync --frozen
uv run --no-sync python tests/issue22_e2e.py
```

环境前提：Python3.12、可用Docker、postgres:17-alpine、
seal-minio:RELEASE.2025-10-15T17-29-55Z，以及
minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727
镜像。测试自动创建随机端口、凭据和合成HTML/PDF/SQL/S3数据，不读取现有本地库/桶。

最终工件：
[独立审计](../../artifacts/issue22-20261014-113746-46619/independent-acceptance-audit.json)、
[断言](../../artifacts/issue22-20261014-113746-46619/assertions.json)、
[HTTP回执](../../artifacts/issue22-20261014-113746-46619/http.json)、
[环境与代码哈希](../../artifacts/issue22-20261014-113746-46619/environment.json)、
[原生SDK结果](../../artifacts/issue22-20261014-113746-46619/native-sdk-index.json)、
[只读前后快照](../../artifacts/issue22-20261014-113746-46619/readonly-snapshots.json)。
复跑会在根work/和artifacts/创建新的独立运行名。

最终命令已在一个全新本地checkout再次运行；启动前无work/、artifacts/、.env、.venv，
独立 `uv sync --frozen` 后执行同一验收入口，结果仍161 PASS / 0 FAIL。
[启动前状态](../../artifacts/issue22-20261014-113746-46619/fresh-checkout-before-run.json)
与审计记录了原始运行位置及镜像回主仓库的工件位置。测试自动创建临时根目录，不依赖
任何历史artifacts文件；稳定清单、历史冻结清单与运行工件的SHA相同。

测试发现并修复了HTTP/排队期限同时到达时的错误码竞态：实际阶段决定server_busy或
request_timeout。真实S3慢体、一个worker及三个竞争请求严格复测通过；
实际后端并发最大1，后台结束后槽位正常释放。此前产品失败工件保留在
[第三轮断言](../../artifacts/issue22-20261014-113018-adcea/assertions.json)；
前两轮夹具故障也保留，未改写历史失败结论。

已验证入口/深度HTML/PDF、版本变化与unchanged/失败保留旧内容、105→107资源期间
的完整快照分页、未知/空/歧义来源、权限隔离、令牌篡改/过期与撤权、附件原始字节、
非法字段/缺失/损坏对象、三类大小上限、依赖停机恢复、无采集/数据库/对象/磁盘副作用
以及配置拒绝和日志脱敏。生产部署、HTTPS反向代理、外站可用性和生产负载未验证；
过期测试使用隔离测试密钥合法重签，并真实跨越剩余6秒验证游标翻页不续期，未实际等待
3600/86400秒。外部删除历史数据不在快照保证范围内。

静态检查使用项目 CI 的 `uvx --from ruff==0.16.10 ruff check src`、
`uvx --from ruff==0.16.10 ruff format --check src`；文档运行 `seiso check`。
回滚只需停止服务并还原新增服务代码/配置，既有数据库、版本和对象无需恢复或迁移。
