"""实验因子业务规则：状态流转、字段校验与筛选口径都收在这里。

V007 跨模块联动：因子登记/生效时读取对照实验分群清单（有效快照）中的互斥规则，
同一实验的同一因子层只允许一个因子占用，阻止重复分配。
"""
from __future__ import annotations

from typing import Any

from app.services.group_snapshot import GroupSnapshotService
from app.store import store

MODULE = "expr_factor"
LIST_FIELDS = ["因子编号", "因子名称", "因子层级", "取值集合", "分配比例", "互斥规则", "关联实验", "生效批次"]
REQUIRED_FIELDS = ["因子编号", "因子名称", "因子层级"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用因子": "已停用"}
NEGATIVE_ACTIONS = ["停用因子"]


class ExprFactorService:
    def __init__(self) -> None:
        self.snapshots = GroupSnapshotService()

    def _resolve_experiment_id(self, linked_experiment: str) -> int | None:
        """因子按方案编号关联实验，这里换算成实验主键用于读取分群清单。"""
        if not linked_experiment:
            return None
        for row in store.rows("experiment"):
            if str(row.get("方案编号")) == str(linked_experiment):
                return int(row["id"])
        return None

    def _mutex_conflict(self, values: dict[str, Any], factor_id: int | None = None) -> str | None:
        linked = str(values.get("关联实验") or "")
        layer = str(values.get("因子层级") or "")
        experiment_id = self._resolve_experiment_id(linked)
        if experiment_id is None or not layer:
            return None
        # 互斥规则以有效分群清单为准：实验未启动、没有有效清单时不拦截因子登记。
        snapshot = self.snapshots.get_active_snapshot(experiment_id)
        if snapshot is None:
            return None
        # 做法 B（登记即查重 + 清单占用双保险）：
        # 1) 清单 factor_layers 已登记的生效占用；2) 同实验同层已存在因子（含草稿）。
        conflict = self.snapshots.check_factor_mutex(experiment_id, layer, factor_id)
        if conflict is not None:
            return conflict
        for row in store.rows(MODULE):
            if (
                str(row.get("关联实验") or "") == linked
                and str(row.get("因子层级") or "") == layer
                and int(row.get("id", 0)) != (factor_id or -1)
            ):
                return (
                    f"因子层「{layer}」已被因子 {row.get('因子编号')} 占用"
                    f"（互斥规则：{snapshot.get('互斥规则') or '同层互斥'}），禁止同一因子层重复分配"
                )
        return None

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("因子编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        # V007：登记时即读取有效分群清单互斥规则，同因子层重复分配直接拦下。
        conflict = self._mutex_conflict(values)
        if conflict is not None:
            return None, [conflict]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"因子配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于实验因子可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 标记生效前再次核对有效清单互斥规则，并把因子层占用登记到清单。
        if action == "标记生效":
            experiment_id = self._resolve_experiment_id(str(entry.get("关联实验") or ""))
            if experiment_id is not None:
                conflict = self.snapshots.check_factor_mutex(
                    experiment_id, str(entry.get("因子层级") or ""), entry_id,
                )
                if conflict is not None:
                    return None, conflict
                register_conflict = self.snapshots.register_factor_layer(
                    experiment_id, str(entry.get("因子层级") or ""),
                    str(entry.get("因子编号") or ""), entry_id,
                )
                if register_conflict is not None:
                    return None, register_conflict
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"因子配置已{action}"
