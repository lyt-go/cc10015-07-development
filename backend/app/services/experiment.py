"""实验设计 时间线服务。

实验创建后保留按分流比例生成基础样本分群的能力（兼容既有链路）；
实验「启动」时在同一个仓库事务里完成状态变更与对照实验分群清单（快照）落库：

- 实验状态与分群清单同时落库，任一步失败整体回滚，运行中实验不会缺少可消费样本；
- 连续启动同一实验只保留一份有效快照，启动中断后只补齐缺失分群；
- 分流比例调整会产生待生效版本，启动时按 freeze_old / apply_new 两种策略解决版本冲突。
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from app.services.group_snapshot import (
    RATIO_POLICIES,
    GroupSnapshotService,
    parse_ratio,
)
from app.store import TransactionResidual, store

MODULE = "experiment"
REQUIRED_FIELDS = ["方案编号", "实验名称", "对照组说明"]
STATUS_ORDER = ["草稿", "待审批", "运行中", "已结束"]
ACTION_RULES = {"提交审批": "待审批", "启动实验": "运行中", "终止实验": "已结束"}
NEGATIVE_ACTIONS = []


class ExperimentService:
    def __init__(self) -> None:
        self.snapshots = GroupSnapshotService()

    def get_timeline(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        today = date.today()
        items: list[dict] = []
        for i, row in enumerate(rows):
            start = (today - timedelta(days=7 - i * 2)).isoformat()
            end = (today + timedelta(days=3 - i)).isoformat()
            status = str(row.get("status", STATUS_ORDER[i % len(STATUS_ORDER)]))
            # 列表页直接读取分群确认结果：有效快照编号、比例快照、总量核对与冲突标记。
            confirmation = self._confirmation_brief(row)
            items.append({
                "id": int(row.get("id", i + 1)),
                "name": str(row.get("实验名称", "")),
                "start": start,
                "end": end,
                "status": status,
                "logs": f"[{start}] 作业启动\n[{start}] 正在处理...\n[{end}] 任务{'完成' if status == STATUS_ORDER[-1] else '进行中'}",
                **confirmation,
            })
        return items

    def _confirmation_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        """汇总某实验的分群清单确认结果，供列表/详情页共同消费。"""
        exp_id = int(row.get("id", 0))
        all_snapshots = self.snapshots.list_snapshots(exp_id)
        active = self.snapshots.get_active_snapshot(exp_id)
        generating = self.snapshots.get_generating_snapshot(exp_id)
        return {
            "分群确认": "已确认" if active else ("待补齐" if generating else "未生成"),
            "有效清单编号": active.get("清单编号") if active else "",
            "分流比例快照": active.get("分流比例快照") if active else "",
            "样本总量核对": active.get("样本总量核对") if active else "",
            "比例冲突": bool(active and active.get("比例冲突")),
            "冲突处理说明": active.get("冲突处理说明", "") if active else "",
            "待生效分流比例": row.get("待生效分流比例", ""),
            "历史清单数": len(all_snapshots),
        }

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        # 详情页读取确认结果与全量清单（含归档历史，按生命周期先后顺序）。
        detail = dict(entry)
        detail["分群确认结果"] = self._confirmation_brief(entry)
        detail["分群清单"] = self.snapshots.list_snapshots(entry_id)
        return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        for f in REQUIRED_FIELDS + ["实验组说明", "样本总量", "分流比例", "启动日期", "预期时长", "互斥规则"]:
            entry[f] = values.get(f, "")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 比例与方案版本化：分群基数始终可回查到当时的实验方案。
        entry["分流比例版本"] = 1
        entry["方案版本"] = 1
        entry["待生效分流比例"] = ""
        entry["created_at"] = time.time()
        rows.append(entry)
        store.bump_version(MODULE)
        # 自动根据分流比例生成基础样本分群定义（保留既有链路；启动时以分群清单为准）。
        self._auto_create_sample_groups(entry)
        return entry, []

    def adjust_ratio(self, entry_id: int, new_ratio: str) -> tuple[dict[str, Any] | None, str]:
        """调整分流比例：只登记为待生效版本，不直接改写已确认清单使用的比例。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"实验方案 {entry_id} 不存在或已归档"
        control, experiment = parse_ratio(new_ratio)
        entry["待生效分流比例"] = f"{control:g}:{experiment:g}"
        entry.setdefault("比例调整记录", []).append({
            "at": time.time(),
            "old_ratio": entry.get("分流比例"),
            "new_ratio": entry["待生效分流比例"],
            "status": "待下一批生效",
        })
        store.bump_version(MODULE)
        return entry, f"新分流比例 {entry['待生效分流比例']} 已登记，将在下次启动时按冲突策略处理"

    def _auto_create_sample_groups(self, experiment: dict[str, Any]) -> None:
        """实验创建后，根据分流比例自动生成基础对照组/实验组分群定义（草稿态）。"""
        ratio_str = str(experiment.get("分流比例") or "5:5")
        control_ratio, exp_ratio = parse_ratio(ratio_str)
        total = control_ratio + exp_ratio
        control_pct = control_ratio / total if total > 0 else 0.5
        total_n = self._parse_total(experiment.get("样本总量"))
        control_n = int(total_n * control_pct)
        exp_n = total_n - control_n
        exp_id = experiment["id"]
        exp_code = experiment.get("方案编号", f"EXPE-{exp_id:04d}")
        sg_rows = store.rows("sample_group")
        now_iso = date.today().isoformat()
        for group_type, label, sample_n in (
            ("control", "对照组", control_n),
            ("experiment", "实验组", exp_n),
        ):
            sg_rows.append({
                "id": store.next_id("sample_group"),
                "分群编号": f"SG-{exp_code}-{label}",
                "分群名称": f"{experiment.get('实验名称', '')} {label}",
                "筛选条件": f"experiment_id={exp_id} AND group='{group_type}'",
                "样本基数": str(sample_n),
                "分群日期": now_iso,
                "更新方式": "实验触发",
                "有效天数": experiment.get("预期时长", "14"),
                "负责人": experiment.get("负责人", "system"),
                "status": "草稿",
                "pending": True,
                "abnormal": False,
                "experiment_id": exp_id,
                "group_type": group_type,
                "source": "experiment_create",
                "created_at": time.time(),
            })
            store.bump_version("sample_group")

    @staticmethod
    def _parse_total(total_value: Any) -> int:
        try:
            return max(int(float(total_value)), 0)
        except (TypeError, ValueError):
            return 10000

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        ratio_policy: str | None = None,
        fail_after_groups: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"实验方案 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于实验设计可执行范围"

        # 启动/终止与分群清单强一致：状态与清单在同一事务内同时落库。
        if action == "启动实验":
            return self._start_experiment(entry, ratio_policy, fail_after_groups)
        if action == "终止实验":
            return self._terminate_experiment(entry)

        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.bump_version(MODULE)
        return entry, f"实验方案已{action}"

    def _start_experiment(
        self,
        entry: dict[str, Any],
        ratio_policy: str | None,
        fail_after_groups: int | None,
    ) -> tuple[dict[str, Any] | None, str]:
        # 连续启动：已在运行且有效清单存在，幂等返回，不生成第二套有效样本。
        if entry.get("status") == "运行中":
            active = self.snapshots.get_active_snapshot(int(entry["id"]))
            if active is not None:
                return self.get_entry(int(entry["id"])), "实验已在运行，连续启动复用唯一有效分群快照，未生成第二套样本"

        if ratio_policy is not None and ratio_policy not in RATIO_POLICIES:
            return None, f"未知分流比例冲突策略「{ratio_policy}」，可选：{'、'.join(RATIO_POLICIES)}"

        try:
            with store.transaction():
                snapshot, message, conflict = self.snapshots.ensure_snapshot_for_start(
                    entry, ratio_policy=ratio_policy, fail_after_groups=fail_after_groups,
                )
                if snapshot is None:
                    # 比例冲突但未选策略：抛错回滚，实验状态保持不变。
                    raise TransactionResidual(message)
                # apply_new 策略下新比例正式生效，待生效版本清空。
                if conflict and ratio_policy == "apply_new":
                    entry["分流比例"] = snapshot["分流比例快照"]
                    entry["分流比例版本"] = snapshot["分流比例版本"]
                    entry["待生效分流比例"] = ""
                    entry.setdefault("比例调整记录", []).append({
                        "at": time.time(),
                        "new_ratio": snapshot["分流比例快照"],
                        "status": "启动时应用（apply_new）",
                    })
                elif conflict and ratio_policy == "freeze_old":
                    entry.setdefault("比例调整记录", []).append({
                        "at": time.time(),
                        "new_ratio": entry.get("待生效分流比例"),
                        "status": "冻结旧比例分群，新比例待下一批",
                    })
                entry["status"] = "运行中"
                entry["pending"] = False
                entry["abnormal"] = False
                store.bump_version(MODULE)
        except TransactionResidual as exc:
            text = str(exc)
            if fail_after_groups is not None:
                # 中断演练：事务已整体回滚，返回可识别的失败说明，等待后续启动补齐。
                return None, f"启动中断，事务已整体回滚：{text}"
            return None, text

        entry_out = self.get_entry(int(entry["id"]))
        return entry_out, message

    def _terminate_experiment(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with store.transaction():
            # 终止前做 V007 残留核对：存在生成中清单则整体回滚，不允许带残留结束。
            self.snapshots.assert_no_residual(int(entry["id"]))
            self.snapshots.archive_for_experiment(int(entry["id"]))
            entry["status"] = "已结束"
            entry["pending"] = False
            store.bump_version(MODULE)
        return self.get_entry(int(entry["id"])), "实验已终止，有效分群清单联动归档"
