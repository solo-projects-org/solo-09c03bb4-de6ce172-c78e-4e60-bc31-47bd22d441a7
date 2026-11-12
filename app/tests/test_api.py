"""Flask API 层测试：路由、错误码与样例端到端。"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


client = app.test_client()
SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "sample_clearing.json")
with open(SAMPLE, encoding="utf-8") as fh:
    sample = json.load(fh)

# 首页
r = client.get("/")
check("首页 200", r.status_code == 200 and "票价口径".encode() in r.data)

# 样例接口
r = client.get("/api/sample")
body = r.get_json()
check("样例接口含两版", r.status_code == 200 and "online" in body and "candidate" in body)

# 校验通过
r = client.post("/api/validate", json=sample)
body = r.get_json()
check("validate 通过", r.status_code == 200 and body["ok"] is True)
check("validate 返回4源表", len(body["source_tables"]) == 4)
check("validate 返回4模型", len(body["models"]) == 4)
m0 = next(m for m in body["models"] if m["name"] == "dwd_card_transaction_detail")
check("validate 输出列差异",
      set(m0["added_outputs"]) == {"base_fare", "fare_amount", "net_amount"},
      str(m0["added_outputs"]))
up = {m["name"]: m["upstream"] for m in body["models"]}
check("validate 直接上游",
      up["dws_line_settlement_detail"] == ["dwd_card_transaction_detail"], str(up))

# 分析：拟上线 dws_line_settlement_detail -> 上下游都被拉入
r = client.post("/api/analyze?model=dws_line_settlement_detail", json=sample)
body = r.get_json()
check("analyze 通过", r.status_code == 200 and body["ok"] is True)
res = body["result"]
check("analyze 同批3个", res["counts"]["switched"] == 3, str(res["counts"]))
check("analyze 留旧1个", res["counts"]["stayed_online"] == 1)
check("analyze 复核通过", res["verified"] is True)
check("analyze 目标在名单首位", res["release_order"][0] == "dws_line_settlement_detail")
roles = {m["name"]: m["role"] for m in res["models"]}
check("analyze 角色标注",
      roles["dws_line_settlement_detail"] == "target"
      and roles["ads_company_settlement_report"] == "unchanged"
      and roles["dwd_card_transaction_detail"] == "co_release", str(roles))
# A/B 两类原因都在
kinds = {x["violation"] for x in res["reasons"]}
check("analyze AB原因齐全",
      kinds == {"candidate_reads_missing_online", "online_reads_missing_candidate"}, str(kinds))
# 原因带引用位置
check("analyze 原因带片段",
      all(x["references"] and x["references"][0]["snippet"] for x in res["reasons"]))
# 边界边：留旧版报表与已切换 summary 之间应有一条 ok 的 B 向检查
edge = next((e for e in res["edge_checks"]
             if e["upstream"] == "dws_route_fare_summary"
             and e["downstream"] == "ads_company_settlement_report"), None)
check("analyze 边界边存在且通过", edge is not None and edge["status"] == "ok", str(edge))

# 分析：缺 model 参数
r = client.post("/api/analyze", json=sample)
check("缺 model 参数 400", r.status_code == 400 and r.get_json()["ok"] is False)

# 分析：未知模型
r = client.post("/api/analyze?model=ghost", json=sample)
check("未知模型 400", r.status_code == 400 and "ghost" in r.get_json()["error"])

# 坏 JSON
r = client.post("/api/validate", data="{oops", content_type="application/json")
check("坏 JSON 400", r.status_code == 400 and "JSON" in r.get_json()["error"])

# 空 body
r = client.post("/api/validate", data="", content_type="application/json")
check("空 body 400", r.status_code == 400 and "线上版" in r.get_json()["error"])

# 请求体缺 candidate
r = client.post("/api/validate", json={"online": sample["online"]})
check("缺 candidate 400", r.status_code == 400 and "candidate" in r.get_json()["error"])

# 非字典顶层
r = client.post("/api/validate", json=[1, 2, 3])
check("顶层非对象 400", r.status_code == 400)

# 候选版 SQL 自身校验不过（LEFT JOIN）-> 400 且带模型名
bad = copy.deepcopy(sample)
bad["candidate"]["models"][1]["sql"] = (
    "SELECT d.fare_amount AS x FROM dwd_card_transaction_detail d "
    "LEFT JOIN dim_bus_line l ON d.line_id = l.line_id"
)
r = client.post("/api/analyze?model=dws_line_settlement_detail", json=bad)
check("候选版坏SQL 400 带模型名",
      r.status_code == 400
      and "dws_line_settlement_detail" in r.get_json()["error"]
      and "INNER" in r.get_json()["error"], r.get_json().get("error"))

# 一致性前提不满足（候选版多加无人引用的源表列）-> 400
bad = copy.deepcopy(sample)
bad["candidate"]["source_tables"][1]["columns"].append("unused_col")
r = client.post("/api/validate", json=bad)
check("源表不一致 400",
      r.status_code == 400 and "源表" in r.get_json()["error"], r.get_json().get("error"))

# 模型名集合不一致 -> 400
bad = copy.deepcopy(sample)
bad["candidate"]["models"][3]["name"] = "ads_renamed"
r = client.post("/api/validate", json=bad)
check("模型名不一致 400",
      r.status_code == 400 and "模型名集合不一致" in r.get_json()["error"])

# 直接上游不一致 -> 400
bad = copy.deepcopy(sample)
bad["candidate"]["models"][3]["sql"] = (
    "SELECT r.company_id AS company_id, r.line_id AS line_id, r.line_name AS line_name, "
    "r.total_settlement AS total_settlement, d.transfer_flag AS transfer_flag "
    "FROM dws_route_fare_summary r "
    "INNER JOIN dws_line_settlement_detail d ON r.transaction_id = d.transaction_id"
)
r = client.post("/api/validate", json=bad)
check("上游不一致 400",
      r.status_code == 400 and "直接上游不一致" in r.get_json()["error"],
      r.get_json().get("error"))

# 选 dwd 目标：纯新增列，可单独上线
r = client.post("/api/analyze?model=dwd_card_transaction_detail", json=sample)
body = r.get_json()
check("dwd单独上线",
      body["result"]["release_order"] == ["dwd_card_transaction_detail"]
      and body["result"]["counts"]["co_release_added"] == 0)

print()
if FAILURES:
    print(f"{len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("API 测试全部通过。")
