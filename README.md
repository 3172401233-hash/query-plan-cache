# query-plan-cache

依赖感知的 Python 查询计划缓存核心。缓存键不再是完整 SQL 字符串，而是
基于 SQL AST 计算的**规范化 fingerprint + 参数敏感形状 + 计划 hint**；DDL 或
统计信息变化时只淘汰依赖该表/列的计划，绝不清空整个缓存。

纯标准库实现，无第三方依赖。

## 模块结构

- `plancache/ast.py`：不可变的 SQL AST 节点（SELECT/INSERT/UPDATE/DELETE、
  表达式、hint）以及 DDL/统计事件（建表、删列、重命名列、ANALYZE 等）。
- `plancache/fingerprint.py`：AST 规范化、fingerprint、表/列依赖提取。
- `plancache/params.py`：绑定参数摘要与参数敏感规则。
- `plancache/schema.py`：表级 schema version 与列级 stats version 目录。
- `plancache/cache.py`：缓存核心，输出命中、拒绝理由和淘汰集合。
- `tests/`：`python -m unittest` 测试套件。

## Fingerprint 规范化规则

`analyze(node)` 把 AST 渲染为 canonical 文本并取 SHA-256 前 16 位：

1. **字面量占位化**：所有 `Literal` 归一为 `?`，因此 `amount = 10` 与
   `amount = 999` 共享同一 fingerprint；绑定参数保留身份（`$1`、`$name`）。
2. **别名归一化**：用户别名按 FROM/JOIN 顺序重写为 `T0`、`T1` …，
   `FROM orders o` 与 `FROM orders ord` 完全等价。
3. **IN 列表形状化**：列表元素全部占位为 `IN (?+)`，长度通过分桶标签进入
   参数形状键（见下），极端长度不会复用普通列表的计划。
4. **Hint 隔离**：optimizer hint 不进入基础 fingerprint，而是独立的键组件；
   同一 SQL 带不同 hint 会得到不同计划条目，且查找时返回 `hint_mismatch`
   而非普通 miss。
5. **依赖提取**：同时输出计划依赖的表集合和 `(表, 列)` 集合，列上的表名
   始终解析为真实表名而非别名，供精确失效使用。

完整缓存键由 `entry_key(analysis, shape_key)` 计算：
`fingerprint + 参数形状 + hint 签名` 三者全等才命中。

## 参数敏感规则

普通参数值永远不会进入缓存键；只有会改变优化器计划选择的**形状**才参与：

- **NULL 自动检测**：参数绑定值为 `None` 时打上 `null` 形状（IS NULL、
  可空 join key 常导致计划切换）。
- **IN 列表长度分桶**：默认桶边界 `(1, 5, 25, 100)`，桶标签为
  `1-1`、`2-5`、`6-25`、`26-100`、`>100`。同桶复用，跨桶拒绝
  （`parameter_shape`），可用 `in_list_bucket()` 自定义边界。
- **自定义标签**：调用方通过 `ParameterSummary(tags={...})` 声明选择性
  等级等敏感特征（如 `selectivity:high`），只传标签、不传真实值。

## 版本与精确失效

`SchemaCatalog` 维护两套独立版本：

- `schema_version`（每表一个）：DDL 改变结构时递增。
- `stats_version`（每表每列一个，整表统计用列 `*`）：ANALYZE 时递增。

写入计划时对依赖快照存档；查找时逐表/逐列比对版本：

- schema 版本漂移 → 拒绝理由 `stale_schema`，该条目进入淘汰集合。
- 统计版本漂移 → 拒绝理由 `stale_stats`，同样懒淘汰并在结果中返回。
- 事件驱动失效 `cache.apply_event(event)` 立即淘汰并返回被淘汰键集合：
  - 列重命名/删除/加列/删表：只淘汰依赖该表的计划；
  - 列级 ANALYZE：只淘汰引用该列的计划，引用其他列的计划保留；
  - 整表 ANALYZE：淘汰该表上的全部计划，其他表不受影响。

容量超限时按 LRU 淘汰，`put()` 返回被容量淘汰的键集合。

## 快速示例

```python
from plancache import (
    SchemaCatalog, PlanCache, ParameterSummary,
    Select, Table, Column, BinaryOp, Literal, Parameter,
    Analyze, RenameColumn,
)

catalog = SchemaCatalog()
cache = PlanCache(catalog, capacity=500)

query = Select(
    columns=(Column("id", "o"),),
    from_=(Table("orders", "o"),),
    where=BinaryOp("=", Column("amount", "o"), Parameter(1)),
)

result = cache.lookup(query, ParameterSummary(values={1: 10}))
result.hit      # False
result.reason   # "miss"
cache.put(query, plan={"op": "index_scan"}, cost=12.0)

hit = cache.lookup(query, ParameterSummary(values={1: 88}))
hit.hit         # True —— 不同参数值复用同一计划
hit.plan

# 只刷新 amount 列统计：依赖该列的计划被淘汰，其他表计划保留
evicted = cache.apply_event(Analyze("orders", ("amount",)))
evicted         # 非空集合，包含被淘汰的缓存键

# 重命名列：精确失效，返回淘汰键集合
evicted = cache.apply_event(RenameColumn("orders", "amount", "total"))
```

## 测试

```bash
python -m unittest
```

覆盖：字面量占位化与复用、IN 列表长度分桶、列/表别名重命名、列级与整表
统计过期、计划 hint 隔离、NULL 与自定义参数敏感标签、LRU 容量淘汰，以及
命中/拒绝理由（`hit`、`miss`、`stale_schema`、`stale_stats`、
`parameter_shape`、`hint_mismatch`）和淘汰集合输出。
