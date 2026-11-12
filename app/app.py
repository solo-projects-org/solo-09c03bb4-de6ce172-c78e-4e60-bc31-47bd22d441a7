"""公交清分数仓 · 票价口径分批上线最小联发名单分析 Web 应用。

仅解析 SQL，不连接数仓、不执行任何 SQL。
"""
from __future__ import annotations

import json
import os

from flask import Flask, jsonify, render_template, request

from lineage import AnalysisError
from release import build_version, check_versions, plan_co_release

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(BASE_DIR, "data", "sample_clearing.json")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB（两版 JSON）


@app.errorhandler(AnalysisError)
def handle_analysis_error(exc: AnalysisError):
    return jsonify({"ok": False, "error": str(exc)}), 400


@app.errorhandler(413)
def handle_too_large(_exc):
    return jsonify({"ok": False, "error": "上传内容超过 16 MB 限制。"}), 413


def _parse_payload(raw: bytes, label: str) -> dict:
    if not raw.strip():
        raise AnalysisError(f"{label}内容为空，请上传包含 source_tables 和 models 的 JSON。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise AnalysisError(f"{label}文件不是 UTF-8 编码，请另存为 UTF-8 后重试。") from exc
    except json.JSONDecodeError as exc:
        raise AnalysisError(
            f"{label}JSON 格式错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise AnalysisError(f"{label}JSON 顶层必须是对象，包含 source_tables 和 models 两个数组。")
    return payload


def _parse_body() -> tuple[dict, dict]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise AnalysisError("请求体必须是 JSON 对象，包含 online（线上版）与 candidate（候选版）两个字段。")
    online = body.get("online")
    candidate = body.get("candidate")
    if not isinstance(online, dict) or not isinstance(candidate, dict):
        raise AnalysisError("请同时提供线上版 JSON（online）与候选版 JSON（candidate）。")
    return online, candidate


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/sample")
def sample():
    with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return jsonify(payload)


@app.post("/api/validate")
def validate():
    """两版各自完整校验 + 一致性前提校验，返回模型清单与输出列差异。"""
    online_payload, candidate_payload = _parse_body()
    online = build_version(online_payload, "线上版")
    candidate = build_version(candidate_payload, "候选版")
    check_versions(online, candidate)

    models = []
    for name in online.order:
        on_out = set(online.outputs(name))
        cand_out = set(candidate.outputs(name))
        models.append({
            "name": name,
            "upstream": sorted(online.infos[name].upstream),
            "online_outputs": sorted(on_out),
            "candidate_outputs": sorted(cand_out),
            "added_outputs": sorted(cand_out - on_out),
            "removed_outputs": sorted(on_out - cand_out),
        })
    return jsonify({
        "ok": True,
        "source_tables": [
            {"table": t, "columns": cols}
            for t, cols in sorted(online.source_schemas.items())
        ],
        "models": models,
    })


@app.post("/api/analyze")
def analyze():
    online_payload, candidate_payload = _parse_body()
    target = (request.args.get("model") or "").strip()
    result = plan_co_release(online_payload, candidate_payload, target)
    return jsonify({"ok": True, "result": result})


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=8000)
