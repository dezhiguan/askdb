# 多智能体可靠性验收

本期覆盖取消、预算、Shadow 对比、进程强杀恢复与 Worker 超时。

## 生产低风险超时探针

镜像内已有 `data/sample.duckdb`。下列命令仅打开该只读样例库，使用固定的
`range(400000000)` 虚拟表查询、150 ms DuckDB 看门狗；不连接运行时注册的
ragforge/careermate 数据源，不调用模型，也不修改样例库。

```sh
kubectl -n askdb exec deploy/askdb -c askdb -- \
  python -m askdb.diagnostics.worker_timeout
```

预期退出码 0，JSON 中 `ok=true`、`status=FAILED`、`error` 含 `R-12` 和
`查询超时`、`source=isolated_duckdb`。`FAILED` 是探针预期的 Worker 状态：
它验证了超时被安全拦截并作为 Worker 失败产物返回。若返回成功、其他错误或
耗时达到 5 秒，探针退出码为 1。

## 预算与取消语义

- 每次模型调用前会为输入与输出预留 Token 和费用，多个并行 Worker 共用同一
  预算表；完成时按实际使用量结算。Worker 的每次尝试分别写入 Checkpoint，
  若只是其他并行调用的临时预留占满额度，后续 Worker 最多等待 60 秒结算后
  再判断，不会把尚未实际消耗的预留直接当成预算耗尽。
  返工不会覆盖前次成本。
- 已用预算达到上限，或下一次调用预留失败时，图以 `BUDGET_EXCEEDED` 收敛，
  不执行后续 SQL 或合成；接口的 `rejected_by` 为 `BUDGET`。
- 厂商实际计数可能高于调用前估算，因此单次在途调用存在超出配置值的可能。
  它会被计入实际用量并触发预算终态，之后不再发起新模型调用。
- 取消记录是跨 Pod 的持久终态；即使旧 Worker 晚于取消完成，任务详情和
  列表仍显示 `canceled`，后台结果不会覆盖取消记录。

## Shadow 对照

`mode=auto` 命中复杂问题路由时，Single 结果仍先返回，后台 Multi Shadow
完成后写入 `shadow_comparison`。登录后在审计回放中可查看 SQL、数据、结论、
Evidence 四级结果。比较记录只存数量、布尔判据与哈希，不复制结果行。
SQL 分支结构不同但数据一致时，SQL 层会标差异，数据层仍可显示一致。

## 进程强杀回归

`tests/test_supervisor_graph.py::test_sigkill_is_detected_as_interrupted_then_resumes_same_checkpoint`
会启动真正的子进程，让 Worker 进入 SQL，确认 Checkpoint 已持久化后发送
`SIGKILL`。测试随后检查任务变为 interrupted、原线程可恢复，并通过
`POST /api/resume` 完成同一线程。此测试使用隔离 SQLite Checkpoint 与裁剪
DuckDB 样例库，不触碰生产数据。
