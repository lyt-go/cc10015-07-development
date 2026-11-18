"""对照实验分群清单（分群快照）业务规则。

一份「对照实验分群清单」记录某实验在某次启动时，按当时实验方案中的分流比例
计算出的对照组/实验组样本组。它是实验样本来源的唯一确认结果：

- 分群基数始终回查实验方案（方案编号、分流比例快照、样本总量快照、方案版本）；
- 清单沿生命周期流转，状态变更、归档、恢复、切换都按先后顺序留痕；
- 同一实验连续启动只保留一份「有效」快照，中断重启只补齐缺失分群，
  不会生成第二套有效样本；
- 生成与实验状态变更放在同一个仓库事务里，任一步失败（含 V007 残留核对）
  整体回滚；
- 实验因子（V007）跨模块读取有效快照里的互斥规则，阻止同一因子层重复分配。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import TransactionResidual, store

MODULE = "group_snapshot"

# 清单生命周期：生成中（启动中断残留）→ 有效 → 已归档；归档后可恢复或被切换。
STATUS_GENERATING = "生成中"
STATUS_ACTIVE = "有效"
STATUS_ARCHIVED = "已归档"
STATUS_ORDER = [STATUS_GENERATING, STATUS_ACTIVE, STATUS_ARCHIVED]

# 生命周期动作（动作名同时写入事件流，保证先后顺序可回放）。
ACTION_ARCHIVE = "归档"
ACTION_RESTORE = "恢复"
ACTION_SWITCH = "切换"
ACTION_RESUME = "补齐分群"
ACTION_CONFIRM = "确认生效"

# 分流比例调整与启动同时生效时的两种版本冲突处理策略。
RATIO_POLICY_FREEZE_OLD = "freeze_old"   # 先冻结旧比例再启动，新比例标记待下一批
RATIO_POLICY_APPLY_NEW = "apply_new"     # 先应用新比例再生成分群，并从旧方案补算差异
RATIO_POLICIES = [RATIO_POLICY_FREEZE_OLD, RATIO_POLICY_APPLY_NEW]

LIST_FIELDS = [
    "清单编号", "实验编号", "方案编号", "方案版本", "分流比例快照", "分流比例版本",
    "对照组样本量", "实验组样本量", "样本总量", "样本总量核对", "互斥规则",
    "生成时间", "生效时间", "状态",
]


def parse_ratio(ratio_str: str) -> tuple[float, float]:
    """解析「对照:实验」分流比例，非法输入回退到 5:5。"""
    try:
        parts = str(ratio_str or "5:5").split(":")
        control = float(parts[0]) if len(parts) > 0 else 5.0
        experiment = float(parts[1]) if len(parts) > 1 else 5.0
    except (TypeError, ValueError):
        control, experiment = 5.0, 5.0
    if control < 0 or experiment < 0 or control + experiment <= 0:
        control, experiment = 5.0, 5.0
    return control, experiment


def split_sample_size(total_n: int, ratio_str: str) -> tuple[int, int]:
    """按分流比例切分样本总量，对照组取整，实验组兜底余量，保证两组之和等于总量。"""
    control_ratio, exp_ratio = parse_ratio(ratio_str)
    ratio_sum = control_ratio + exp_ratio
    control_n = int(round(total_n * control_ratio / ratio_sum))
    exp_n = total_n - control_n
    return control_n, exp_n


def parse_total(total_value: Any) -> int:
    try:
        total_n = int(float(total_value))
    except (TypeError, ValueError):
        total_n = 10000
    return max(total_n, 0)


class GroupSnapshotService:
    """分群清单的生成、生命周期流转与跨模块消费都收敛在本服务。"""

    # ---------- 查询：列表页与详情页都读这份确认结果 ----------

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        experiment_id: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if experiment_id is not None:
            rows = [row for row in rows if int(row.get("experiment_id", 0)) == experiment_id]
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("清单编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 按生命周期先后顺序（序号）倒序，新事件在前；同序号按 id 倒序。
        rows = sorted(rows, key=lambda r: (int(r.get("seq", 0)), int(r.get("id", 0))), reverse=True)
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_active_snapshot(self, experiment_id: int) -> dict[str, Any] | None:
        """供消费端（V007 实验因子等）读取某实验当前唯一有效的全量分群清单。"""
        for row in store.rows(MODULE):
            if int(row.get("experiment_id", 0)) == experiment_id and row.get("status") == STATUS_ACTIVE:
                return row
        return None

    def get_generating_snapshot(self, experiment_id: int) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if int(row.get("experiment_id", 0)) == experiment_id and row.get("status") == STATUS_GENERATING:
                return row
        return None

    def list_snapshots(self, experiment_id: int) -> list[dict[str, Any]]:
        """某实验全量清单（含历史归档），按生命周期先后顺序返回。"""
        rows = [
            row for row in store.rows(MODULE)
            if int(row.get("experiment_id", 0)) == experiment_id
        ]
        return sorted(rows, key=lambda r: (int(r.get("seq", 0)), int(r.get("id", 0))))

    # ---------- 生成：实验启动时在同一事务内调用 ----------

    def ensure_snapshot_for_start(
        self,
        experiment: dict[str, Any],
        *,
        ratio_policy: str | None = None,
        fail_after_groups: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """实验启动时确保存在一份有效分群清单。

        返回 ``(快照, 说明消息, 是否冲突已按策略处理)``。
        - 已存在有效快照：连续启动幂等返回，绝不生成第二套有效样本；
        - 存在「生成中」残留（启动中断）：只补齐缺失分群后转有效；
        - 否则按当前实验方案（分流比例/样本总量）新建清单。

        ``fail_after_groups`` 仅供测试模拟中断：生成指定数量样本组后抛错，
        由外层事务整体回滚，仓库里留下可核对的失败现场（由调用方决定是否保留）。
        """
        exp_id = int(experiment["id"])

        # 1) 连续启动：已有有效快照直接复用，样本总量仍按比例可核对。
        active = self.get_active_snapshot(exp_id)
        if active is not None:
            return active, "对照实验分群清单已存在，连续启动复用唯一有效快照", False

        # 2) 启动中断：存在生成中残留，只补齐缺失分群，不新建第二套。
        generating = self.get_generating_snapshot(exp_id)
        if generating is not None:
            return self._resume_generating(generating, experiment)

        # 3) 全新启动：判定比例版本冲突，确定本批使用的比例。
        ratio_used, ratio_version, conflict, policy_note = self._resolve_ratio(experiment, ratio_policy)
        if conflict and ratio_policy not in RATIO_POLICIES:
            return None, (
                "分流比例存在待生效新版本，启动前必须选择冲突处理策略："
                "freeze_old（冻结旧比例，新比例待下一批）或 apply_new（应用新比例并补算差异）"
            ), True

        snapshot = self._create_snapshot(
            experiment, ratio_used=ratio_used, ratio_version=ratio_version,
            conflict=conflict, policy_note=policy_note, fail_after_groups=fail_after_groups,
        )
        if fail_after_groups is not None:
            # 模拟中断：抛出残留异常，外层事务回滚；测试可在回滚前观察到生成中清单。
            raise TransactionResidual("模拟启动中断：分群清单存在生成中残留，事务整体回滚")
        return snapshot, f"对照实验分群清单已按分流比例 {ratio_used} 生成并确认生效", conflict

    def _resolve_ratio(
        self, experiment: dict[str, Any], ratio_policy: str | None
    ) -> tuple[str, int, bool, str]:
        """决定本批分群使用的比例，返回 (比例字符串, 比例版本, 是否冲突, 策略说明)。"""
        current_ratio = str(experiment.get("分流比例") or "5:5")
        current_version = int(experiment.get("分流比例版本", 1))
        pending = experiment.get("待生效分流比例")
        if pending in (None, ""):
            return current_ratio, current_version, False, ""

        conflict = True
        if ratio_policy == RATIO_POLICY_FREEZE_OLD:
            # 旧比例分群，新比例保持待生效，留给下一批。
            return current_ratio, current_version, conflict, (
                f"按 freeze_old 冻结旧比例 {current_ratio} 分群，新比例 {pending} 标记待下一批生效"
            )
        if ratio_policy == RATIO_POLICY_APPLY_NEW:
            # 新比例分群，同时落库旧比例基数，便于从旧方案补算差异。
            old_total = parse_total(experiment.get("样本总量"))
            old_control, old_exp = split_sample_size(old_total, current_ratio)
            note = (
                f"按 apply_new 应用新比例 {pending} 分群；旧方案 {current_ratio} 基数 "
                f"对照{old_control}/实验{old_exp}，差异随新清单补算"
            )
            return str(pending), current_version + 1, conflict, note
        # 未指定策略：交给调用方拦截并要求选择。
        return current_ratio, current_version, conflict, ""

    def _create_snapshot(
        self,
        experiment: dict[str, Any],
        *,
        ratio_used: str,
        ratio_version: int,
        conflict: bool,
        policy_note: str,
        fail_after_groups: int | None,
    ) -> dict[str, Any]:
        exp_id = int(experiment["id"])
        total_n = parse_total(experiment.get("样本总量"))
        control_n, exp_n = split_sample_size(total_n, ratio_used)
        now_ts = time.time()
        seq = self._next_seq(exp_id)
        exp_code = str(experiment.get("方案编号") or f"EXPE-{exp_id:04d}")
        snapshot_id = store.next_id(MODULE)
        snapshot = {
            "id": snapshot_id,
            "清单编号": f"GSL-{exp_code}-S{seq:02d}",
            "experiment_id": exp_id,
            "实验编号": exp_code,
            # 分群基数回查实验方案的锚点：方案编号、方案版本、比例及其版本、总量快照。
            "方案编号": exp_code,
            "方案版本": int(experiment.get("方案版本", 1)),
            "分流比例快照": ratio_used,
            "分流比例版本": ratio_version,
            "样本总量": total_n,
            "对照组样本量": control_n,
            "实验组样本量": exp_n,
            "样本总量核对": f"{control_n}+{exp_n}={control_n + exp_n}（总量{total_n}，比例{ratio_used}）",
            "互斥规则": experiment.get("互斥规则", ""),
            "status": STATUS_GENERATING,
            "pending": True,
            "abnormal": False,
            "seq": seq,
            "比例冲突": conflict,
            "冲突处理说明": policy_note,
            # 仅 apply_new 使用新比例分群时才从旧方案补算差异；freeze_old 沿用旧比例无差异。
            "补算差异": self._diff_note(experiment, ratio_used, total_n)
            if conflict and ratio_used != str(experiment.get("分流比例") or "") else "",
            "生成时间": now_ts,
            "生效时间": None,
            "归档时间": None,
            "groups": [],
            "events": [self._event(seq, "生成", f"按方案 {exp_code} 比例 {ratio_used} 开始生成分群清单")],
            "created_at": now_ts,
        }
        store.rows(MODULE).append(snapshot)
        store.bump_version(MODULE)

        # 样本组逐条生成；中断注入时只落下部分组，状态停在「生成中」。
        self._append_group(snapshot, "control", control_n, experiment, fail_after_groups)
        if fail_after_groups is not None and fail_after_groups <= 1:
            return snapshot
        self._append_group(snapshot, "experiment", exp_n, experiment, fail_after_groups)

        snapshot["status"] = STATUS_ACTIVE
        snapshot["pending"] = False
        snapshot["生效时间"] = time.time()
        snapshot["events"].append(
            self._event(seq, ACTION_CONFIRM, f"对照组{control_n}/实验组{exp_n}，样本总量核对一致，清单确认生效")
        )
        return snapshot

    def _append_group(
        self,
        snapshot: dict[str, Any],
        group_type: str,
        sample_n: int,
        experiment: dict[str, Any],
        fail_after_groups: int | None,
    ) -> None:
        label = "对照组" if group_type == "control" else "实验组"
        exp_id = int(experiment["id"])
        group_id = store.next_id("sample_group")
        group = {
            "id": group_id,
            "分群编号": f"{snapshot['清单编号']}-{'CTRL' if group_type == 'control' else 'EXP'}",
            "分群名称": f"{experiment.get('实验名称', '')} {label}",
            "筛选条件": f"experiment_id={exp_id} AND group='{group_type}'",
            "样本基数": str(sample_n),
            "分群日期": time.strftime("%Y-%m-%d"),
            "更新方式": "实验启动触发",
            "有效天数": experiment.get("预期时长", "14"),
            "负责人": experiment.get("负责人", "system"),
            "status": STATUS_ACTIVE if fail_after_groups is None else STATUS_GENERATING,
            "pending": fail_after_groups is not None,
            "abnormal": False,
            "experiment_id": exp_id,
            "group_type": group_type,
            "snapshot_id": snapshot["id"],
            "source": "group_snapshot",
            "created_at": time.time(),
        }
        store.rows("sample_group").append(group)
        store.bump_version("sample_group")
        snapshot["groups"].append({
            "group_id": group_id,
            "group_type": group_type,
            "label": label,
            "样本量": sample_n,
            "分群编号": group["分群编号"],
        })

    def _resume_generating(
        self, generating: dict[str, Any], experiment: dict[str, Any]
    ) -> tuple[dict[str, Any], str, bool]:
        """启动中断恢复：只补齐缺失分群，不新建清单、不重置已有组。"""
        existing_types = {g.get("group_type") for g in generating.get("groups", [])}
        seq = int(generating["seq"])
        if "control" not in existing_types:
            self._append_group(generating, "control", int(generating["对照组样本量"]), experiment, None)
        if "experiment" not in existing_types:
            self._append_group(generating, "experiment", int(generating["实验组样本量"]), experiment, None)
        # 恢复后重新核对样本总量。
        total_groups = sum(int(g["样本量"]) for g in generating["groups"])
        if total_groups != int(generating["样本总量"]):
            return None, f"补齐后样本总量核对不一致：分组合计{total_groups} != 清单总量{generating['样本总量']}", False
        for group in store.rows("sample_group"):
            if group.get("snapshot_id") == generating["id"]:
                group["status"] = STATUS_ACTIVE
                group["pending"] = False
        generating["status"] = STATUS_ACTIVE
        generating["pending"] = False
        generating["生效时间"] = time.time()
        generating["events"].append(
            self._event(seq, ACTION_RESUME, f"启动中断恢复，补齐缺失分群后确认生效，样本总量核对 {total_groups}")
        )
        store.bump_version(MODULE)
        return generating, "启动中断后已补齐缺失分群，未生成第二套有效样本", False

    # ---------- 生命周期：归档 / 恢复 / 切换，全部留痕且保序 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        snapshot = store.find(MODULE, entry_id)
        if snapshot is None:
            return None, f"对照实验分群清单 {entry_id} 不存在"
        if snapshot.get("status") == STATUS_GENERATING:
            return None, "生成中清单不能执行生命周期动作，请先通过实验启动补齐分群"
        seq = int(snapshot["seq"])
        if action == ACTION_ARCHIVE:
            if snapshot["status"] == STATUS_ARCHIVED:
                return None, "清单已归档，无需重复归档"
            self._mark_archived(snapshot)
            snapshot["events"].append(self._event(seq, ACTION_ARCHIVE, "清单归档，保留全部历史分群与事件顺序"))
        elif action == ACTION_RESTORE:
            if snapshot["status"] != STATUS_ARCHIVED:
                return None, "只有已归档清单可以恢复"
            self._restore(snapshot)
        elif action == ACTION_SWITCH:
            if snapshot["status"] != STATUS_ARCHIVED:
                return None, "只有已归档清单可以切换为有效清单"
            self._switch_to(snapshot)
        else:
            return None, f"动作「{action}」不属于分群清单可执行范围"
        store.bump_version(MODULE)
        return snapshot, f"分群清单已{action}"

    def _mark_archived(self, snapshot: dict[str, Any]) -> None:
        snapshot["status"] = STATUS_ARCHIVED
        snapshot["pending"] = False
        snapshot["归档时间"] = time.time()
        for group in store.rows("sample_group"):
            if group.get("snapshot_id") == snapshot["id"]:
                group["status"] = STATUS_ARCHIVED

    def _restore(self, snapshot: dict[str, Any]) -> None:
        exp_id = int(snapshot["experiment_id"])
        active = self.get_active_snapshot(exp_id)
        if active is not None:
            # 恢复归档清单等价于一次切换：当前有效清单先归档，保证同一时刻只有一份有效。
            self._mark_archived(active)
            active["events"].append(
                self._event(int(active["seq"]), ACTION_SWITCH, f"恢复清单 {snapshot['清单编号']}，原有效清单归档")
            )
        snapshot["status"] = STATUS_ACTIVE
        snapshot["pending"] = False
        snapshot["归档时间"] = None
        snapshot["生效时间"] = snapshot.get("生效时间") or time.time()
        for group in store.rows("sample_group"):
            if group.get("snapshot_id") == snapshot["id"]:
                group["status"] = STATUS_ACTIVE
        seq = int(snapshot["seq"])
        snapshot["events"].append(self._event(seq, ACTION_RESTORE, "归档清单恢复为有效，事件顺序保持不变"))

    def _switch_to(self, snapshot: dict[str, Any]) -> None:
        exp_id = int(snapshot["experiment_id"])
        active = self.get_active_snapshot(exp_id)
        if active is not None and active["id"] != snapshot["id"]:
            self._mark_archived(active)
            active["events"].append(
                self._event(int(active["seq"]), ACTION_SWITCH, f"切换到清单 {snapshot['清单编号']}，本清单归档")
            )
        snapshot["status"] = STATUS_ACTIVE
        snapshot["pending"] = False
        snapshot["归档时间"] = None
        for group in store.rows("sample_group"):
            if group.get("snapshot_id") == snapshot["id"]:
                group["status"] = STATUS_ACTIVE
        snapshot["events"].append(
            self._event(int(snapshot["seq"]), ACTION_SWITCH, "切换为当前有效分群清单，历史事件顺序保留")
        )

    def archive_for_experiment(self, experiment_id: int) -> None:
        """实验终止时联动归档其有效清单（在实验事务内调用）。"""
        active = self.get_active_snapshot(experiment_id)
        if active is None:
            return
        self._mark_archived(active)
        active["events"].append(
            self._event(int(active["seq"]), ACTION_ARCHIVE, "实验终止，有效分群清单联动归档")
        )
        store.bump_version(MODULE)

    # ---------- V007：互斥规则消费与事务残留核对 ----------

    def assert_no_residual(self, experiment_id: int) -> None:
        """V007 核对：分群清单使用环节若存在生成中残留，抛错触发事务整体回滚。"""
        residual = self.get_generating_snapshot(experiment_id)
        if residual is not None:
            raise TransactionResidual(
                f"V007 核对未通过：实验 {experiment_id} 的分群清单 {residual['清单编号']} "
                "存在生成中残留，事务整体回滚"
            )

    def check_factor_mutex(self, experiment_id: int, factor_layer: str, factor_id: int | None = None) -> str | None:
        """实验因子（V007）跨模块读取有效快照互斥规则。

        同一实验有效清单里，同一因子层只允许一个因子占用；返回冲突说明，
        不冲突时返回 None。
        """
        snapshot = self.get_active_snapshot(experiment_id)
        if snapshot is None:
            return None  # 实验尚未启动、没有有效清单时不拦截
        rules = snapshot.get("互斥规则")
        # 清单上登记的因子层占用情况（由 register_factor_layer 写入）。
        occupied: list[dict[str, Any]] = snapshot.get("factor_layers", [])
        for item in occupied:
            if item.get("因子层级") == factor_layer and item.get("factor_id") != factor_id:
                return (
                    f"因子层「{factor_layer}」已由因子 {item.get('因子编号')} 在有效分群清单 "
                    f"{snapshot['清单编号']} 中占用（互斥规则：{rules or '同层互斥'}），禁止重复分配"
                )
        return None

    def register_factor_layer(
        self, experiment_id: int, factor_layer: str, factor_code: str, factor_id: int
    ) -> str | None:
        """因子生效时把因子层占用登记到有效清单，返回冲突说明或 None。"""
        conflict = self.check_factor_mutex(experiment_id, factor_layer, factor_id)
        if conflict is not None:
            return conflict
        snapshot = self.get_active_snapshot(experiment_id)
        if snapshot is None:
            return None
        snapshot.setdefault("factor_layers", []).append({
            "factor_id": factor_id,
            "因子编号": factor_code,
            "因子层级": factor_layer,
            "登记时间": time.time(),
        })
        snapshot["events"].append(
            self._event(int(snapshot["seq"]), "因子登记", f"因子 {factor_code} 占用因子层 {factor_layer}")
        )
        store.bump_version(MODULE)
        return None

    def consume_snapshot(self, experiment_id: int, consumer: str) -> dict[str, Any]:
        """消费端（V007）读取全量有效清单；使用前先做残留核对，有残留整体回滚。"""
        self.assert_no_residual(experiment_id)
        snapshot = self.get_active_snapshot(experiment_id)
        if snapshot is None:
            raise TransactionResidual(f"实验 {experiment_id} 没有有效分群清单可供消费")
        # 留一条消费痕迹，顺序并入事件流。
        snapshot["events"].append(
            self._event(int(snapshot["seq"]), "消费读取", f"消费端 {consumer} 读取全量分群清单")
        )
        store.bump_version(MODULE)
        return snapshot

    # ---------- 工具 ----------

    def _next_seq(self, experiment_id: int) -> int:
        existing = [int(r.get("seq", 0)) for r in store.rows(MODULE)
                    if int(r.get("experiment_id", 0)) == experiment_id]
        return max(existing, default=0) + 1

    def _diff_note(self, experiment: dict[str, Any], new_ratio: str, total_n: int) -> str:
        old_ratio = str(experiment.get("分流比例") or "5:5")
        old_control, old_exp = split_sample_size(total_n, old_ratio)
        new_control, new_exp = split_sample_size(total_n, new_ratio)
        return (
            f"旧比例{old_ratio} 对照{old_control}/实验{old_exp} → 新比例{new_ratio} "
            f"对照{new_control}/实验{new_exp}，差异对照{new_control - old_control:+d}/实验{new_exp - old_exp:+d}"
        )

    @staticmethod
    def _event(seq: int, action: str, detail: str) -> dict[str, Any]:
        return {"seq": seq, "action": action, "detail": detail, "at": time.time()}
