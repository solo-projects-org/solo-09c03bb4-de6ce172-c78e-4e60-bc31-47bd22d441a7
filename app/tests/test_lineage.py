"""lineage.py / release.py 测试：解析校验、一致性前提与最小联发闭包。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lineage import AnalysisError, build_models  # noqa: E402
from release import build_version, check_versions, plan_co_release  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def sources():
    return [
        {"table": "t_a", "columns": ["id", "flag", "val"]},
        {"table": "t_b", "columns": ["id", "name", "amt"]},
    ]


def payload(models):
    return {"source_tables": sources(), "models": models}


def model(name, sql):
    return {"name": name, "sql": sql}


# 1. 基本解析：输出列、直接上游、引用位置 -----------------------------------------------------
p = payload([
    model("m1", "SELECT a.id AS id, a.flag AS flag FROM t_a a WHERE a.val > 0"),
    model("m2", "SELECT x.id AS id, x.flag AS flag FROM m1 x INNER JOIN t_b b ON x.id = b.id"),
])
_, infos, order = build_models(p)
check("解析-拓扑序", order == ["m1", "m2"], str(order))
check("解析-m1输出列", infos["m1"].outputs == ["id", "flag"], str(infos["m1"].outputs))
check("解析-m2直接上游", infos["m2"].upstream == {"m1"}, str(infos["m2"].upstream))
clauses = {r.clause for r in infos["m2"].refs}
check("解析-引用子句齐全", clauses == {"SELECT", "JOIN ON"}, str(clauses))
check("解析-引用带片段", all(r.snippet for r in infos["m2"].refs))
check("解析-模型读取只含model类",
      all(r.source_kind == "model" for refs in infos["m2"].model_reads().values()
          for rs in refs.values() for r in rs))
check("解析-model_reads按上游列归集",
      set(infos["m2"].model_reads()["m1"]) == {"id", "flag"})

# 2. 旧有 SQL 校验规则保持有效 ----------------------------------------------------------------
for bad_name, bad_sql, kw in [
    ("未限定列", "SELECT flag FROM t_a a", "未带表名"),
    ("星号", "SELECT * FROM t_a a", "星号"),
    ("LEFT JOIN", "SELECT a.id AS id FROM t_a a LEFT JOIN t_b b ON a.id = b.id", "INNER"),
    ("逗号连接", "SELECT a.id AS id FROM t_a a, t_b b WHERE a.id = b.id", "INNER"),
    ("GROUP BY", "SELECT a.id AS id FROM t_a a GROUP BY a.id", "GROUP BY"),
    ("递归CTE", "WITH RECURSIVE r AS (SELECT a.id AS n FROM t_a a) SELECT r.n AS n FROM r", "递归"),
    ("未知表", "SELECT x.id AS id FROM t_x x", "t_x"),
    ("幽灵列", "SELECT a.ghost AS g FROM t_a a", "ghost"),
]:
    try:
        build_models(payload([model("m_bad", bad_sql)]))
        check(f"校验-{bad_name}报错", False)
    except AnalysisError as e:
        check(f"校验-{bad_name}报错", "m_bad" in str(e) and kw in str(e), str(e))

# 3. 上游模型幽灵列与循环依赖 ----------------------------------------------------------------
try:
    build_models(payload([
        model("m1", "SELECT a.id AS id FROM t_a a"),
        model("m2", "SELECT x.ghost AS g FROM m1 x"),
    ]))
    check("上游幽灵列-报错", False)
except AnalysisError as e:
    check("上游幽灵列-报错", "m2" in str(e) and "ghost" in str(e), str(e))

try:
    build_models(payload([
        model("m1", "SELECT y.id AS id FROM m2 y"),
        model("m2", "SELECT x.id AS id FROM m1 x"),
    ]))
    check("循环依赖-报错", False)
except AnalysisError as e:
    check("循环依赖-报错", "循环" in str(e), str(e))


# 4. 一致性前提：源表清单 / 模型名 / 直接上游 -------------------------------------------------
def two_chain_payloads():
    online = payload([
        model("m1", "SELECT a.id AS id FROM t_a a"),
        model("m2", "SELECT x.id AS id FROM m1 x"),
    ])
    candidate = payload([
        model("m1", "SELECT a.id AS id, a.val AS val FROM t_a a"),
        model("m2", "SELECT x.id AS id, x.val AS val FROM m1 x"),
    ])
    return online, candidate


online, candidate = two_chain_payloads()
check("前提-一致版本通过", check_versions(build_version(online, "线上版"),
                                          build_version(candidate, "候选版")) is None)

import copy  # noqa: E402

bad = copy.deepcopy(candidate)
bad["source_tables"][0]["columns"].append("unused_col")
try:
    plan_co_release(online, bad, "m2")
    check("前提-源表不一致报错", False)
except AnalysisError as e:
    check("前提-源表不一致报错", "源表" in str(e) and "不一致" in str(e), str(e))

bad = copy.deepcopy(candidate)
bad["models"][1]["name"] = "m2_renamed"
try:
    plan_co_release(online, bad, "m2")
    check("前提-模型名不一致报错", False)
except AnalysisError as e:
    check("前提-模型名不一致报错", "模型名集合不一致" in str(e), str(e))

bad = copy.deepcopy(candidate)
bad["models"][1]["sql"] = ("SELECT x.id AS id, x.val AS val, b.name AS name "
                           "FROM m1 x INNER JOIN t_b b ON x.id = b.id")
try:
    plan_co_release(online, bad, "m2")
    check("前提-直接上游(源表)不变仍通过", True)
except AnalysisError as e:  # 源表 JOIN 不改变模型级上游，应当通过
    check("前提-直接上游(源表)不变仍通过", False, str(e))

# 三模型无环结构下改写 m3 的模型级上游（新增 m1 -> m3 边）
online3 = payload([
    model("m1", "SELECT a.id AS id FROM t_a a"),
    model("m2", "SELECT x.id AS id FROM m1 x"),
    model("m3", "SELECT y.id AS id FROM m2 y"),
])
cand3 = payload([
    model("m1", "SELECT a.id AS id, a.val AS val FROM t_a a"),
    model("m2", "SELECT x.id AS id, x.val AS val FROM m1 x"),
    model("m3", "SELECT y.id AS id, x.val AS xval FROM m2 y "
                 "INNER JOIN m1 x ON y.id = x.id"),
])
try:
    plan_co_release(online3, cand3, "m2")
    check("前提-模型上游不一致报错", False)
except AnalysisError as e:
    check("前提-模型上游不一致报错", "直接上游不一致" in str(e), str(e))


# 5. A 类闭包：候选下游读旧上游缺列 -> 上游被迫同批 -------------------------------------------
online, candidate = two_chain_payloads()
r = plan_co_release(online, candidate, "m2")
check("A类-名单含上下游", set(r["release_order"]) == {"m1", "m2"}, str(r["release_order"]))
check("A类-追加数=1", r["counts"]["co_release_added"] == 1)
a = next(x for x in r["reasons"] if x["violation"] == "candidate_reads_missing_online")
check("A类-缺列名为val", a["missing_column"] == "val" and a["added_model"] == "m1", str(a))
check("A类-原因含引用位置", any("SELECT" in ref["clause"] for ref in a["references"]))
check("A类-复核通过", r["verified"] is True and not r["residual_errors"])


# 6. B 类闭包：线上下游读新上游缺列 -> 下游被迫同批 -------------------------------------------
online_b = payload([
    model("m1", "SELECT a.id AS id, a.flag AS flag FROM t_a a"),
    model("m2", "SELECT x.id AS id, x.flag AS flag FROM m1 x"),
])
candidate_b = payload([
    model("m1", "SELECT a.id AS id FROM t_a a"),  # 移除 flag
    model("m2", "SELECT x.id AS id FROM m1 x"),   # 同步不再读 flag
])
r = plan_co_release(online_b, candidate_b, "m1")
check("B类-名单含上下游", set(r["release_order"]) == {"m1", "m2"}, str(r["release_order"]))
b = next(x for x in r["reasons"] if x["violation"] == "online_reads_missing_candidate")
check("B类-缺列名为flag", b["missing_column"] == "flag" and b["added_model"] == "m2", str(b))
check("B类-复核通过", r["verified"] is True)


# 7. 纯新增列可单独上线（最小性） -------------------------------------------------------------
online_c = payload([model("m1", "SELECT a.id AS id FROM t_a a")])
candidate_c = payload([model("m1", "SELECT a.id AS id, a.val AS val FROM t_a a")])
r = plan_co_release(online_c, candidate_c, "m1")
check("最小性-仅自身切换", r["release_order"] == ["m1"] and r["counts"]["co_release_added"] == 0)
check("最小性-无追加原因", r["reasons"] == [])
check("最小性-无边界边", r["edge_checks"] == [])


# 8. 多轮扩散：链式三层，目标在中间，一轮内上下游同时拉入 --------------------------------------
online_d = payload([
    model("m1", "SELECT a.id AS id FROM t_a a"),
    model("m2", "SELECT x.id AS id FROM m1 x"),
    model("m3", "SELECT y.id AS id FROM m2 y"),
])
candidate_d = payload([
    model("m1", "SELECT a.id AS id, a.val AS val FROM t_a a"),
    model("m2", "SELECT x.id AS id, x.val AS val FROM m1 x"),
    model("m3", "SELECT y.id AS id FROM m2 y"),
])
r = plan_co_release(online_d, candidate_d, "m2")
# 精确判断：线上 m3 只读 id，候选 m2 仍输出 id，故 m3 可留旧版；m1 因 A 类必须切换
check("多轮-名单恰为m1m2", set(r["release_order"]) == {"m1", "m2"}, str(r["release_order"]))
check("多轮-m3可留旧版", "m3" in r["stayed_online"], str(r["stayed_online"]))
check("多轮-m1被A类拉入",
      any(x["added_model"] == "m1" and x["violation"] == "candidate_reads_missing_online"
          for x in r["reasons"]))
check("多轮-边界边覆盖留旧模型",
      any(e["downstream"] == "m3" and e["status"] == "ok" for e in r["edge_checks"]))


# 9. B 类沿链向下多轮扩散 ---------------------------------------------------------------------
online_e = payload([
    model("m1", "SELECT a.id AS id, a.flag AS flag FROM t_a a"),
    model("m2", "SELECT x.id AS id, x.flag AS flag FROM m1 x"),
    model("m3", "SELECT y.id AS id, y.flag AS flag FROM m2 y"),
])
candidate_e = payload([
    model("m1", "SELECT a.id AS id FROM t_a a"),
    model("m2", "SELECT x.id AS id FROM m1 x"),
    model("m3", "SELECT y.id AS id FROM m2 y"),
])
r = plan_co_release(online_e, candidate_e, "m1")
check("向下扩散-三模型同批", set(r["release_order"]) == {"m1", "m2", "m3"}, str(r["release_order"]))
check("向下扩散-两轮", r["counts"]["rounds"] == 2, str(r["counts"]))
b_rounds = sorted(x["round"] for x in r["reasons"]
                  if x["violation"] == "online_reads_missing_candidate")
check("向下扩散-B类轮次1和2", b_rounds == [1, 2], str(b_rounds))


# 10. 拟上线模型不存在 ------------------------------------------------------------------------
try:
    plan_co_release(online_d, candidate_d, "ghost")
    check("坏目标-报错", False)
except AnalysisError as e:
    check("坏目标-报错", "ghost" in str(e), str(e))


# 11. 清分样例端到端 --------------------------------------------------------------------------
sample_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "sample_clearing.json")
with open(sample_path, encoding="utf-8") as fh:
    sample = json.load(fh)

r = plan_co_release(sample["online"], sample["candidate"], "dws_line_settlement_detail")
check("样例-同批3个", r["counts"]["switched"] == 3, str(r["counts"]))
check("样例-报表留旧版", r["stayed_online"] == ["ads_company_settlement_report"],
      str(r["stayed_online"]))
check("样例-追加2个", r["counts"]["co_release_added"] == 2)
check("样例-3条缺列原因", r["counts"]["reason_count"] == 3, str(r["counts"]))
kinds = {x["violation"] for x in r["reasons"]}
check("样例-AB两类都有",
      kinds == {"candidate_reads_missing_online", "online_reads_missing_candidate"}, str(kinds))
check("样例-复核通过无残留", r["verified"] is True and r["residual_errors"] == [])
# 每个追加模型都能对应到原因
for m in r["models"]:
    if m["role"] == "co_release":
        check(f"样例-{m['name']}带原因", len(m["added_reasons"]) >= 1)
# 原因文案点明缺列与双方模型名
msg = r["reasons"][0]["message"]
check("样例-原因文案含列与模型", "「" in msg and "」" in msg and "列" in msg)

# 样例中选 dwd（纯新增列）应可单独上线
r2 = plan_co_release(sample["online"], sample["candidate"], "dwd_card_transaction_detail")
check("样例-dwd可单独上线", r2["release_order"] == ["dwd_card_transaction_detail"],
      str(r2["release_order"]))

# 样例中选 ads（两版无变化）闭包只有自己
r3 = plan_co_release(sample["online"], sample["candidate"], "ads_company_settlement_report")
check("样例-ads无需联发", r3["release_order"] == ["ads_company_settlement_report"])

# 12. CTE 内部引用同样可解析并归集 -------------------------------------------------------------
p = payload([
    model("m1", "WITH c1 AS (SELECT a.id AS id, a.flag AS flag FROM t_a a) "
                "SELECT c1.id AS id, c1.flag AS flag FROM c1"),
    model("m2", "SELECT x.id AS id FROM m1 x"),
])
_, infos, _ = build_models(p)
check("CTE-引用可解析", {r.column for r in infos["m1"].refs} >= {"id", "flag"})
check("CTE-m2上游", infos["m2"].upstream == {"m1"})

print()
if FAILURES:
    print(f"{len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("全部测试通过。")
