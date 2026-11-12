# 公交清分数仓 · 票价口径分批上线最小联发名单分析

公交清分数仓要分批上线新的票价口径。值班工程师需要先确认**哪些模型必须同批切换**，
避免留在旧版的结算模型读取已不存在的列。

本工具用 [SQLGlot](https://github.com/tobymao/sqlglot) **静态解析** DuckDB SQL
（只解析、不执行、不连接数仓），在网页上传同一批模型的**线上版**与**候选版** JSON，
选择一个拟上线的候选模型后，给出：

- 包含所选模型的**最小联发名单**：名单内切到候选版，其余沿用线上版；
- 每个被追加模型的**具体原因**：因哪一处 SQL 引用、缺了哪一列而必须同批；
- 混用后所有跨版本模型边的**双向复核**结果，以及可直接粘贴进发布单的文本。

## 分析口径

### 前置校验（任一不满足即拒绝分析并指出原因）

1. 线上版、候选版**各自通过完整 SQL 校验与引用解析**（支持的 SQL 范围见下）；
2. 两版**源表字段清单一致**：源表名集合相同，且每张表的列集合相同；
3. 两版**模型名集合一致**；
4. 每个模型的**直接上游模型集合一致**（允许 SQL 内部实现变化，但模型级血缘边不能变）。

### 最小联发闭包（在模型 DAG 上双向检查）

设名单内模型使用候选版、名单外模型使用线上版。对每条上游 `U -> 下游 D` 的模型边检查
两类“混用即读不到列”的冲突，并把被牵连的模型加入名单，从拟上线模型出发反复扩散到收敛：

| 类型 | 情形 | 触发条件 | 追加模型 |
| --- | --- | --- | --- |
| A 类 | 候选下游 `D` 已切换、上游 `U` 仍是线上版 | 候选版 `D` 读取了线上版 `U` **不输出**的列 | 追加切换上游 `U`（候选模型读旧上游缺列） |
| B 类 | 候选上游 `U` 已切换、下游 `D` 仍是线上版 | 线上版 `D` 读取了候选版 `U` **已移除**的列 | 追加切换下游 `D`（线上模型读新上游缺列） |

闭包结果唯一且最小：任何可行的联发方案都必须包含闭包中的全部模型，而闭包内切换、
闭包外留旧版时不存在缺列边（由终态复核保证）。

### 支持的 SQL 范围

- 普通 `SELECT`、**非递归** CTE（`WITH`）、`INNER JOIN`、`WHERE`；
- 禁止星号（`SELECT *` / `t.*`），所有列引用必须带表名或别名；
- 支持 `WHERE ... IN (SELECT ...)` / `WHERE EXISTS (SELECT ...)` 形式的非关联谓词子查询、
  FROM 派生表（需别名）；投影中允许任意函数、运算、`CASE`、类型转换等表达式；
- 不支持递归 CTE、关联子查询、投影中的标量子查询、`UNION` 等集合运算、
  `GROUP BY`、窗口函数、`LEFT/RIGHT/FULL/CROSS JOIN`、逗号隐式连接、DML 等，
  遇到即报错指出所在模型并停止分析。

## 目录结构

```
.
├── docker-compose.yml            # 一键启动
└── app/
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py                    # Flask 路由（/、/api/sample、/api/validate、/api/analyze）
    ├── lineage.py                # SQLGlot 解析、SQL 校验、输出列与引用位置提取
    ├── release.py                # 两版一致性前提、最小联发闭包、双向混用复核
    ├── data/
    │   └── sample_clearing.json  # 清分样例（online/candidate 两版，各含 4 源表 + 4 模型）
    ├── templates/
    │   └── index.html            # 单页前端（无外部 CDN 依赖）
    └── tests/
        ├── test_lineage.py       # 解析、前提校验、A/B 闭包、多轮扩散测试
        └── test_api.py           # 路由、错误码与样例端到端测试
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>：

1. 点击「载入清分样例（含两版）」，或分别上传线上版、候选版 JSON；
2. 两版校验与一致性前提通过后，在左侧选择一个拟上线模型；
3. 点击「计算最小联发名单」，右侧展示联发轮次、逐模型缺列原因、
   混用边界双向复核，并可一键复制发布单文本。

停止：`docker compose down`。

## 清分样例说明（上下游都要同批切换）

样例为票价口径改造，模型链路：

```
dwd_card_transaction_detail → dws_line_settlement_detail → dws_route_fare_summary → ads_company_settlement_report
```

候选版相对线上版的关键变化：

- `dwd_card_transaction_detail` 关联票价规则维表，**新增** `base_fare / fare_amount / net_amount` 输出列；
- `dws_line_settlement_detail` 改读上游新列 `fare_amount / net_amount`，并把输出列
  `settlement_amount` **改名**为 `fare_settlement_amount`；
- `dws_route_fare_summary` 改读 `fare_settlement_amount`（线上版仍读 `settlement_amount`）；
- `ads_company_settlement_report` 读取列两版无变化。

选择拟上线模型 **`dws_line_settlement_detail`** 时：

- A 类：候选版该模型读取 `dwd_card_transaction_detail` 的 `fare_amount / net_amount`，
  线上版上游没有这两列 → **上游 `dwd_card_transaction_detail` 必须同批**；
- B 类：线上版 `dws_route_fare_summary` 仍读取该模型的 `settlement_amount`，
  候选版已移除该列 → **下游 `dws_route_fare_summary` 必须同批**；
- `ads_company_settlement_report` 与已切换上游之间无缺列，可继续沿用线上版。

最终同批切换 3 个、留旧版 1 个，双向复核 0 缺列。选择 `dwd_card_transaction_detail`
（纯新增列）则可单独上线，闭包只有它自己，可对照体会最小性。

## 输入 JSON 格式

请求体同时携带两版，每版都是含 `source_tables` 与 `models` 的对象；
`/api/sample` 返回的样例用 `online` / `candidate` 两个字段包装：

```json
{
  "online": {
    "source_tables": [
      { "table": "ods_card_transaction", "columns": ["transaction_id", "amount"] }
    ],
    "models": [
      { "name": "dwd_card_transaction_detail",
        "description": "可选说明",
        "sql": "SELECT t.transaction_id AS transaction_id, t.amount AS amount FROM ods_card_transaction t" }
    ]
  },
  "candidate": { "source_tables": [ "...与线上版一致..." ], "models": [ "...同模型名、同直接上游..." ] }
}
```

前端也接受直接上传单版本 JSON（含 `source_tables` 与 `models`），分别放入线上版/候选版槽位。

- `source_tables[].table` 为源表名，`columns` 为该表全部字段；
- `models[].name` 为模型名，模型 SQL 中 `FROM/JOIN` 同名关系即视为读取该上游模型；
- 其余名字按源表处理，必须在 `source_tables` 中声明。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sample` | 返回内置清分样例（含 online/candidate 两版） |
| POST | `/api/validate` | 两版各自完整校验 + 一致性前提，返回源表清单、模型上游与两版输出列差异 |
| POST | `/api/analyze?model=<拟上线模型名>` | 返回最小联发名单、逐模型缺列原因与混用边界复核 |

错误统一返回 HTTP 400 与 `{"ok": false, "error": "中文原因（含模型名）"}`。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r app/requirements.txt
python app/app.py                       # 监听 8000
python app/tests/test_lineage.py        # 解析与闭包测试
python app/tests/test_api.py            # API 端到端测试
```
