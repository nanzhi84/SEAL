# SEAL V1.1 Runtime Golden Fixtures

用途：工程回归与执行契约的小样本验证。它**不属于 Evaluation 模块，不产生质量评分或发布批准**。

**注意：本包无法直接证明 SEAL 仓库通过测试。** 未提供 SEAL 仓库代码，因此包含测试输入、人工定义的语义期望，以及可运行的本地 HTTP fixture 服务。具体如何注册 Source/Binding、如何运行现有 CLI、以及如何将字段映射到实际 Schema，需由 CLI Agent 先检查仓库，再在现有测试目录编写适配测试。不要为了让 Golden 通过而修改生产 Schema 或预期值。

## 文件

- `golden_expected.json`: 8 组人工定义的解析参考字段与每份输入的 SHA256。字段是与 SEAL 实际 JSON Schema 无关的语义投影。
- `runtime_cases.json`: 12 个 Runtime 契约验收场景，包括分页、归档、A→B→A、gzip、503、Replay、Binding 隔离、Worker 任务。
- `fixtures/*.html`: 本地静态样本，不从真实网站抓取。
- `server.py`: 只监听 127.0.0.1 的 Python 标准库 HTTP 服务，具有受控版本变化及一次失败后恢复的响应。
- `verify_pack.py`: 校验本夹具包内容、动态 HTTP 行为，**不测试 SEAL 实现本身**。

## 自检

```bash
python verify_pack.py
python server.py --port 8765
```

另一个终端：

```bash
curl -i http://127.0.0.1:8765/notice/simple
curl -i http://127.0.0.1:8765/compressed
curl -i http://127.0.0.1:8765/redirect
curl -i http://127.0.0.1:8765/flaky
curl -s http://127.0.0.1:8765/__control/reset
curl -s 'http://127.0.0.1:8765/__control/revision?value=B'
curl -i http://127.0.0.1:8765/changing
curl -s http://127.0.0.1:8765/__control/stats
```

## 校验建议

1. 先确认仓库已有 `tests/`、acceptance 与技术契约，避免重复造一个测试平台。
2. 使用隔离 PostgreSQL + 归档目录 + Procrastinate 队列。测试只能操作本地 fixture 服务和测试数据库。
3. 用专用的、可信的测试 Recipe 解析合成 HTML；不要把测试专用 Recipe 用作正式业务来源。
4. 对 `golden_expected.json` 的人工预期逐项做语义字段映射并严格对比，只允许有文档说明的空白标准化。忽略技术 ID、运行时间戳等不确定值，不忽略字段内容错误。
5. G05 日期缺失是测试预期：如果现有 Schema 强制要求日期，则应期待技术校验失败，不要擅自放宽 Schema。
6. 版本 A→B→A 需要在相同 `/changing` URL 上先后运行三次。不能分别抓取不同 URL 再声称测到了相同文档的修订。
7. 重放时禁用网络访问，不能以重新下载冒充 Replay。
8. 对不能可靠自动化的场景输出 `UNVERIFIED`，禁止以跳过或打印 PASS 替代断言。

## 测试范围限制

这些 fixtures 只证明给定样本的技术合同和确定性的语义提取。它们不能证明真实网站的覆盖率、法律业务正确性、生产规模承载力或对抗恶意代码的执行安全。所有导出仍标注 `quality_status=not_evaluated`。
