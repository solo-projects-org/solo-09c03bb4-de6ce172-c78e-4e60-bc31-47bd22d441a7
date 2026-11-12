"""票价口径分批上线 · 最小联发名单分析（纯静态解析，不执行 SQL）。

输入：同一批模型的线上版 JSON 与候选版 JSON，以及一个拟上线（候选版）模型。

前提：两版各自通过 lineage.build_models 的完整校验，且
- 源表字段清单一致（表名集合 + 每张表的列集合）；
- 模型名集合一致；
- 每个模型的直接上游模型集合一致。

闭包规则（DAG 上沿模型边双向检查）：
- A 类：候选版下游 D 已切换、上游 U 仍在线上，若候选 D 读取了线上 U 不输出的列，
  则 U 必须同批切换（候选模型读不到旧上游的列）；
- B 类：候选版上游 U 已切换、下游 D 仍在线上，若线上 D 读取了候选 U 已不输出的列，
  则 D 必须同批切换（线上模型读不到已切换上游的列）。

从拟上线模型出发反复扩散直到无新增，得到唯一的最小联发名单
（任何可行联发方案都必须包含闭包中的全部模型）。
"""
from __future__ import annotations

from dataclasses import dataclass

from lineage import AnalysisError, ColumnRef, ModelInfo, build_models


@dataclass
class VersionBundle:
    label: str
    source_schemas: dict[str, list[str]]
    infos: dict[str, ModelInfo]
    order: list[str]

    def outputs(self, model: str) -> set[str]:
        return set(self.infos[model].outputs)

    def reads(self, model: str) -> dict[str, dict[str, list[ColumnRef]]]:
        return self.infos[model].model_reads()


def build_version(payload: dict, label: str) -> VersionBundle:
    source_schemas, infos, order = build_models(payload)
    return VersionBundle(label=label, source_schemas=source_schemas, infos=infos, order=order)


# ---------------------------------------------------------------------------
# 两版一致性前提
# ---------------------------------------------------------------------------

def _diff_schemas(online: VersionBundle, candidate: VersionBundle) -> str | None:
    on_tables = set(online.source_schemas)
    cand_tables = set(candidate.source_schemas)
    if on_tables != cand_tables:
        only_on = sorted(on_tables - cand_tables)
        only_cand = sorted(cand_tables - on_tables)
        parts = []
        if only_on:
            parts.append(f"仅线上版声明的源表：{', '.join(only_on)}")
        if only_cand:
            parts.append(f"仅候选版声明的源表：{', '.join(only_cand)}")
        return "两版源表清单不一致（" + "；".join(parts) + "），请先对齐源表口径。"
    for table in sorted(on_tables):
        on_cols = set(online.source_schemas[table])
        cand_cols = set(candidate.source_schemas[table])
        if on_cols != cand_cols:
            added = sorted(cand_cols - on_cols)
            removed = sorted(on_cols - cand_cols)
            parts = [f"源表「{table}」字段清单不一致"]
            detail = []
            if added:
                detail.append(f"候选版新增列 {added}")
            if removed:
                detail.append(f"候选版缺少列 {removed}")
            parts.append("（".join(detail) + "）")
            return "，".join(parts) + "，请先对齐源表字段清单。"
    return None


def _diff_models(online: VersionBundle, candidate: VersionBundle) -> str | None:
    on_names = set(online.infos)
    cand_names = set(candidate.infos)
    if on_names != cand_names:
        only_on = sorted(on_names - cand_names)
        only_cand = sorted(cand_names - on_names)
        parts = []
        if only_on:
            parts.append(f"仅线上版存在的模型：{', '.join(only_on)}")
        if only_cand:
            parts.append(f"仅候选版存在的模型：{', '.join(only_cand)}")
        return "两版模型名集合不一致（" + "；".join(parts) + "），无法按同批模型做联发分析。"
    for name in sorted(on_names):
        on_up = online.infos[name].upstream
        cand_up = candidate.infos[name].upstream
        if on_up != cand_up:
            added = sorted(cand_up - on_up)
            removed = sorted(on_up - cand_up)
            detail = []
            if added:
                detail.append(f"候选版新增上游 {added}")
            if removed:
                detail.append(f"候选版移除上游 {removed}")
            return (f"模型「{name}」两版的直接上游不一致（"
                    + "；".join(detail) + "），请先对齐模型血缘再做联发分析。")
    return None


def check_versions(online: VersionBundle, candidate: VersionBundle) -> None:
    """两版一致性前提校验，失败时抛出带中文原因的 AnalysisError。"""
    msg = _diff_schemas(online, candidate)
    if msg:
        raise AnalysisError(msg)
    msg = _diff_models(online, candidate)
    if msg:
        raise AnalysisError(msg)


# ---------------------------------------------------------------------------
# 引用位置与原因文案
# ---------------------------------------------------------------------------

def _ref_dto(ref: ColumnRef) -> dict:
    return {
        "qualifier": ref.qualifier,
        "column": ref.column,
        "query": ref.query_name,
        "clause": ref.clause,
        "snippet": ref.snippet,
    }


def _locations_text(refs: list[ColumnRef]) -> str:
    seen = set()
    lines = []
    for ref in refs:
        key = (ref.query_name, ref.clause, ref.snippet, ref.column)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"查询「{ref.query_name}」{ref.clause} 中的 `{ref.snippet}`")
    return "；".join(lines)


def _reason_dto(added_model: str, round_no: int, violation: str,
                upstream: str, downstream: str, column: str,
                refs: list[ColumnRef]) -> dict:
    if violation == "candidate_reads_missing_online":
        # A 类：候选下游已切换，上游仍是线上版 -> 追加切换上游
        message = (
            f"候选版模型「{downstream}」{_locations_text(refs)}引用了上游模型「{upstream}」"
            f"的列「{column}」，但线上版「{upstream}」不输出该列；"
            f"若「{upstream}」留在旧版，候选「{downstream}」将读不到列，"
            f"故「{upstream}」需同批切换。"
        )
    else:
        # B 类：候选上游已切换，下游仍是线上版 -> 追加切换下游
        message = (
            f"线上版模型「{downstream}」{_locations_text(refs)}引用了上游模型「{upstream}」"
            f"的列「{column}」，但候选版「{upstream}」已不输出该列；"
            f"若「{upstream}」单独切换，线上版「{downstream}」将读不到列，"
            f"故「{downstream}」需同批切换。"
        )
    return {
        "added_model": added_model,
        "round": round_no,
        "violation": violation,
        "upstream": upstream,
        "downstream": downstream,
        "missing_column": column,
        "references": [_ref_dto(r) for r in refs],
        "message": message,
    }


# ---------------------------------------------------------------------------
# 最小联发闭包
# ---------------------------------------------------------------------------

def plan_co_release(online_payload: dict, candidate_payload: dict,
                    target: str) -> dict:
    online = build_version(online_payload, "线上版")
    candidate = build_version(candidate_payload, "候选版")
    check_versions(online, candidate)

    if not target or not isinstance(target, str):
        raise AnalysisError("请选择一个拟上线的模型。")
    if target not in online.infos:
        raise AnalysisError(f"拟上线模型「{target}」不在模型清单中。")

    all_models = list(online.order)  # 两版拓扑结构一致，用线上版拓扑序做稳定排序

    # 反向邻接：上游 -> 读取它的下游模型列表（两版上游关系一致）
    downstream_of: dict[str, list[str]] = {name: [] for name in all_models}
    for downstream in all_models:
        for upstream in online.infos[downstream].upstream:
            downstream_of[upstream].append(downstream)

    switched: set[str] = {target}
    rounds: dict[str, int] = {target: 0}
    reasons: list[dict] = []
    seen_reason_keys: set[tuple] = set()

    def add_reason(added_model: str, round_no: int, violation: str,
                   upstream: str, downstream: str, column: str,
                   refs: list[ColumnRef]) -> None:
        key = (violation, upstream, downstream, column)
        if key in seen_reason_keys:
            return
        seen_reason_keys.add(key)
        reasons.append(_reason_dto(added_model, round_no, violation,
                                   upstream, downstream, column, refs))

    round_no = 0
    while True:
        added_this_round: list[str] = []
        # 边集合两版一致（前提校验保证），用线上版遍历
        for upstream in all_models:
            for downstream in downstream_of[upstream]:
                d_switched = downstream in switched
                u_switched = upstream in switched
                if d_switched and not u_switched:
                    # A 类：候选下游读线上旧上游
                    cand_reads = candidate.reads(downstream).get(upstream, {})
                    online_outputs = online.outputs(upstream)
                    for column, refs in sorted(cand_reads.items()):
                        if column not in online_outputs:
                            add_reason(upstream, round_no + 1,
                                       "candidate_reads_missing_online",
                                       upstream, downstream, column, refs)
                            if upstream not in switched:
                                added_this_round.append(upstream)
                elif u_switched and not d_switched:
                    # B 类：线上下游读候选新上游
                    on_reads = online.reads(downstream).get(upstream, {})
                    cand_outputs = candidate.outputs(upstream)
                    for column, refs in sorted(on_reads.items()):
                        if column not in cand_outputs:
                            add_reason(downstream, round_no + 1,
                                       "online_reads_missing_candidate",
                                       upstream, downstream, column, refs)
                            if downstream not in switched:
                                added_this_round.append(downstream)

        new_models = []
        for m in added_this_round:
            if m not in switched:
                switched.add(m)
                new_models.append(m)
        if not new_models:
            break
        round_no += 1
        for m in new_models:
            rounds[m] = round_no

    # 终态复核：所有跨越“候选/线上”边界的边，双向引用必须仍可读
    edge_checks: list[dict] = []
    residual_errors: list[str] = []
    for upstream in all_models:
        for downstream in downstream_of[upstream]:
            u_switched = upstream in switched
            d_switched = downstream in switched
            if u_switched == d_switched:
                continue  # 同版本内部，已由各自单版本校验保证
            if d_switched and not u_switched:
                kind = "candidate_downstream_reads_online_upstream"
                checked = sorted(candidate.reads(downstream).get(upstream, {}).keys())
                missing = sorted(c for c in checked if c not in online.outputs(upstream))
            else:
                kind = "online_downstream_reads_candidate_upstream"
                checked = sorted(online.reads(downstream).get(upstream, {}).keys())
                missing = sorted(c for c in checked if c not in candidate.outputs(upstream))
            edge_checks.append({
                "upstream": upstream,
                "downstream": downstream,
                "kind": kind,
                "checked_columns": checked,
                "missing_columns": missing,
                "status": "ok" if not missing else "violated",
            })
            for column in missing:
                residual_errors.append(
                    f"边界边「{upstream} -> {downstream}」上列「{column}」在混用后仍不可读。"
                )

    reasons_by_model: dict[str, list[dict]] = {}
    for reason in reasons:
        reasons_by_model.setdefault(reason["added_model"], []).append(reason)

    def sort_key(name: str) -> tuple:
        return (rounds.get(name, 999), online.order.index(name))

    release_order = sorted(switched, key=sort_key)
    stayed = sorted(set(all_models) - switched, key=all_models.index)

    models_dto = []
    for name in all_models:
        if name == target:
            role = "target"
        elif name in switched:
            role = "co_release"
        else:
            role = "unchanged"
        models_dto.append({
            "name": name,
            "role": role,
            "round": rounds.get(name),
            "upstream": sorted(online.infos[name].upstream),
            "online_outputs": sorted(online.outputs(name)),
            "candidate_outputs": sorted(candidate.outputs(name)),
            "added_reasons": reasons_by_model.get(name, []),
        })

    return {
        "target": target,
        "release_order": release_order,
        "stayed_online": stayed,
        "models": models_dto,
        "reasons": reasons,
        "edge_checks": edge_checks,
        "residual_errors": residual_errors,
        "verified": not residual_errors,
        "counts": {
            "total_models": len(all_models),
            "switched": len(switched),
            "co_release_added": len(switched) - 1,
            "stayed_online": len(stayed),
            "reason_count": len(reasons),
            "rounds": round_no,
        },
        "schema_fingerprint": {
            table: sorted(cols) for table, cols in sorted(online.source_schemas.items())
        },
    }
