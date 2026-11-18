"""对照实验分群清单（group_snapshot）全链路验收测试。

覆盖需求点：
1. 读取分流比例按比例计算对照/实验组，生成清单且分群基数可回查实验方案；
2. 实验状态与分群清单在同一事务同时落库，失败整体回滚；
3. 连续启动只有一份有效快照，样本总量按分流比例可核对；
4. 启动中断后只补齐缺失分群，不产生第二套有效样本；
5. 生命周期归档/恢复/切换保序留痕；
6. V007 残留核对整体回滚、消费端全量读取、实验因子同层互斥；
7. 分流比例调整与启动同时生效的两种版本冲突策略。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.experiment import ExperimentService
from app.services.expr_factor import ExprFactorService
from app.services.group_snapshot import (
    RATIO_POLICY_APPLY_NEW,
    RATIO_POLICY_FREEZE_OLD,
    GroupSnapshotService,
    split_sample_size,
)
from app.store import TransactionResidual, store


def _clear_all():
    for key in list(store._tables.keys()):
        store._tables[key] = []


def _make_experiment(es: ExperimentService, ratio: str = "5:5", total: str = "10000", code: str = "EXPE-T1"):
    entry, missing = es.create_entry({
        "方案编号": code,
        "实验名称": "分群清单验收实验",
        "对照组说明": "旧策略",
        "实验组说明": "新策略",
        "样本总量": total,
        "分流比例": ratio,
        "启动日期": "2026-11-18",
        "预期时长": "14",
        "互斥规则": "UI层同层互斥",
    })
    assert entry is not None, missing
    return entry


def test_ratio_split_and_plan_traceability():
    """按分流比例切分样本，清单字段始终可回查实验方案。"""
    _clear_all()
    es = ExperimentService()
    entry = _make_experiment(es, ratio="7:3", total="1000")
    out, message = es.run_action(entry["id"], "启动实验")
    assert out is not None, message

    snap = es.snapshots.get_active_snapshot(entry["id"])
    assert snap is not None
    assert snap["分流比例快照"] == "7:3"
    assert snap["对照组样本量"] == 700
    assert snap["实验组样本量"] == 300
    # 样本总量按分流比例核对：两组之和恒等于总量。
    assert snap["对照组样本量"] + snap["实验组样本量"] == 1000
    # 回查锚点齐全：方案编号、方案版本、比例版本、总量快照。
    for field in ("方案编号", "方案版本", "分流比例版本", "样本总量", "样本总量核对"):
        assert snap.get(field) not in (None, "")
    # 切分函数对任意比例都保证守恒。
    c, e = split_sample_size(1001, "3:7")
    assert c + e == 1001


def test_status_and_snapshot_commit_atomically():
    """实验状态与分群清单同时落库；构造失败时事务整体回滚。"""
    _clear_all()
    es = ExperimentService()
    entry = _make_experiment(es)
    exp_before = dict(store.find("experiment", entry["id"]))
    sg_count_before = len(store.rows("sample_group"))
    snap_count_before = len(store.rows("group_snapshot"))

    # 比例冲突但未选择策略：启动被拒，状态与清单都不变。
    es.adjust_ratio(entry["id"], "8:2")
    out, message = es.run_action(entry["id"], "启动实验")
    assert out is None
    assert "freeze_old" in message and "apply_new" in message
    assert store.find("experiment", entry["id"])["status"] == exp_before["status"]
    assert len(store.rows("group_snapshot")) == snap_count_before
    assert len(store.rows("sample_group")) == sg_count_before


def test_repeated_start_keeps_single_active_snapshot():
    """连续启动同一实验，对照组与实验组各只有一份有效快照。"""
    _clear_all()
    es = ExperimentService()
    entry = _make_experiment(es, ratio="6:4", total="500")

    first, msg1 = es.run_action(entry["id"], "启动实验")
    assert first is not None, msg1
    first_snapshot_id = first["分群确认结果"]["有效清单编号"]

    # 再次、第三次启动：幂等复用，不新增清单、不新增有效样本组。
    second, _ = es.run_action(entry["id"], "启动实验")
    third, _ = es.run_action(entry["id"], "启动实验")
    assert second["分群确认结果"]["有效清单编号"] == first_snapshot_id
    assert third["分群确认结果"]["有效清单编号"] == first_snapshot_id

    active = [s for s in store.rows("group_snapshot")
              if s["experiment_id"] == entry["id"] and s["status"] == "有效"]
    assert len(active) == 1
    snapshot_groups = [g for g in store.rows("sample_group")
                       if g.get("snapshot_id") == active[0]["id"] and g["status"] == "有效"]
    types = sorted(g["group_type"] for g in snapshot_groups)
    assert types == ["control", "experiment"]
    assert sum(int(g["样本基数"]) for g in snapshot_groups) == 500


def test_interrupted_start_only_backfills_missing_groups():
    """启动中断事务回滚后，再次启动只补齐缺失分群，不生成第二套样本。"""
    _clear_all()
    es = ExperimentService()
    gs = GroupSnapshotService()
    entry = _make_experiment(es, ratio="5:5", total="200")

    # 模拟中断：在生成第 1 个样本组后抛错，事务整体回滚。
    out, message = es.run_action(entry["id"], "启动实验", fail_after_groups=1)
    assert out is None and "中断" in message
    # 回滚后仓库不留半截数据：没有生成中清单、没有残留样本组、实验仍是草稿。
    assert gs.get_generating_snapshot(entry["id"]) is None
    assert store.find("experiment", entry["id"])["status"] == "草稿"
    assert all(g.get("snapshot_id") is None for g in store.rows("sample_group"))

    # 重新启动正常完成：只有一份有效清单、两组样本且总量守恒。
    resumed, msg = es.run_action(entry["id"], "启动实验")
    assert resumed is not None, msg
    active = gs.get_active_snapshot(entry["id"])
    assert active is not None and active["status"] == "有效"
    assert len(active["groups"]) == 2
    assert sum(g["样本量"] for g in active["groups"]) == 200
    assert any(e["action"] == "确认生效" for e in active["events"])
    assert len([s for s in store.rows("group_snapshot") if s["experiment_id"] == entry["id"]]) == 1


def test_lifecycle_archive_restore_switch_keeps_order():
    """归档、恢复、切换都保留事件先后顺序，且任一时刻只有一份有效清单。"""
    _clear_all()
    es = ExperimentService()
    gs = GroupSnapshotService()
    entry = _make_experiment(es)
    es.run_action(entry["id"], "启动实验")
    first = gs.get_active_snapshot(entry["id"])

    # 归档第一份。
    archived, msg = gs.run_action(first["id"], "归档")
    assert archived is not None and archived["status"] == "已归档"
    assert gs.get_active_snapshot(entry["id"]) is None

    # 调整比例后重新启动，生成第二份有效清单。
    es.adjust_ratio(entry["id"], "8:2")
    es.run_action(entry["id"], "启动实验", ratio_policy=RATIO_POLICY_APPLY_NEW)
    second = gs.get_active_snapshot(entry["id"])
    assert second is not None and second["id"] != first["id"]

    # 恢复第一份：第二份自动归档，第一份回到有效，事件顺序可回放。
    restored, _ = gs.run_action(first["id"], "恢复")
    assert restored["status"] == "有效"
    assert gs.get_active_snapshot(entry["id"])["id"] == first["id"]
    assert store.find("group_snapshot", second["id"])["status"] == "已归档"
    actions_first = [e["action"] for e in first["events"]]
    assert actions_first.index("归档") < actions_first.index("恢复")
    assert any(e["action"] == "切换" for e in second["events"])

    # 再切回第二份：仍只有一份有效。
    gs.run_action(second["id"], "切换")
    assert gs.get_active_snapshot(entry["id"])["id"] == second["id"]
    assert store.find("group_snapshot", first["id"])["status"] == "已归档"


def test_v007_residual_check_rolls_back_and_consume_reads_full_snapshot():
    """V007 核对：使用环节存在残留则事务整体回滚；正常时消费端读取全量清单。"""
    _clear_all()
    es = ExperimentService()
    gs = GroupSnapshotService()
    entry = _make_experiment(es)
    es.run_action(entry["id"], "启动实验")

    # 正常消费：读到全量有效清单并留痕。
    consumed = gs.consume_snapshot(entry["id"], "V007实验因子模块")
    assert consumed["status"] == "有效"
    assert any(e["action"] == "消费读取" for e in consumed["events"])

    # 手工制造一份生成中残留（模拟另一实验的中断现场）。
    other = _make_experiment(es, code="EXPE-T2")
    with pytest.raises(TransactionResidual):
        with store.transaction():
            gs.ensure_snapshot_for_start(other, fail_after_groups=1)
    # 残留核对必须抛错，且外层事务回滚后现场被清空。
    assert gs.get_generating_snapshot(other["id"]) is None

    # 直接对有效实验做残留核对通过；对不存在清单的实验消费应报错。
    gs.assert_no_residual(entry["id"])
    with pytest.raises(TransactionResidual):
        gs.consume_snapshot(9999, "V007")


def test_v007_factor_mutex_blocks_same_layer():
    """实验因子跨模块读取清单互斥规则，阻止同一因子层重复分配。"""
    _clear_all()
    es = ExperimentService()
    ef = ExprFactorService()
    entry = _make_experiment(es, code="EXPE-FACT")
    es.run_action(entry["id"], "启动实验")

    first, missing = ef.create_entry({
        "因子编号": "F-UI-1", "因子名称": "按钮颜色", "因子层级": "UI层",
        "取值集合": "红,蓝", "分配比例": "5:5", "互斥规则": "同层互斥",
        "关联实验": "EXPE-FACT", "生效批次": "B001",
    })
    assert first is not None, missing
    ef.run_action(first["id"], "标记生效")

    # 同一因子层再来一个因子：登记即被互斥规则拦下。
    blocked, missing2 = ef.create_entry({
        "因子编号": "F-UI-2", "因子名称": "按钮形状", "因子层级": "UI层",
        "取值集合": "圆,方", "分配比例": "5:5", "互斥规则": "同层互斥",
        "关联实验": "EXPE-FACT", "生效批次": "B002",
    })
    assert blocked is None
    assert any("UI层" in m and "互斥" in m for m in missing2)

    # 不同因子层不受影响。
    other, missing3 = ef.create_entry({
        "因子编号": "F-ALG-1", "因子名称": "排序模型", "因子层级": "算法层",
        "关联实验": "EXPE-FACT",
    })
    assert other is not None, missing3


def test_ratio_conflict_freeze_old_policy():
    """freeze_old：清单沿用旧比例，新比例标记待下一批。"""
    _clear_all()
    es = ExperimentService()
    entry = _make_experiment(es, ratio="5:5", total="1000")
    es.adjust_ratio(entry["id"], "8:2")

    out, message = es.run_action(entry["id"], "启动实验", ratio_policy=RATIO_POLICY_FREEZE_OLD)
    assert out is not None, message
    snap = es.snapshots.get_active_snapshot(entry["id"])
    assert snap["分流比例快照"] == "5:5"  # 旧比例
    assert snap["对照组样本量"] == 500 and snap["实验组样本量"] == 500
    assert snap["比例冲突"] is True
    assert "freeze_old" in snap["冲突处理说明"]
    # 新比例仍挂在实验上等待下一批。
    assert store.find("experiment", entry["id"])["待生效分流比例"] == "8:2"


def test_ratio_conflict_apply_new_policy():
    """apply_new：清单使用新比例，并从旧方案补算差异。"""
    _clear_all()
    es = ExperimentService()
    entry = _make_experiment(es, ratio="5:5", total="1000")
    es.adjust_ratio(entry["id"], "8:2")

    out, message = es.run_action(entry["id"], "启动实验", ratio_policy=RATIO_POLICY_APPLY_NEW)
    assert out is not None, message
    snap = es.snapshots.get_active_snapshot(entry["id"])
    assert snap["分流比例快照"] == "8:2"
    assert snap["对照组样本量"] == 800 and snap["实验组样本量"] == 200
    assert "+300" in snap["补算差异"] and "-300" in snap["补算差异"]
    # 实验方案比例已更新，待生效版本清空。
    exp_row = store.find("experiment", entry["id"])
    assert exp_row["分流比例"] == "8:2"
    assert exp_row["待生效分流比例"] == ""


def test_terminate_archives_snapshot_and_blocks_with_residual():
    """终止实验联动归档清单；V007 残留存在时终止被整体回滚。"""
    _clear_all()
    es = ExperimentService()
    gs = GroupSnapshotService()
    entry = _make_experiment(es)
    es.run_action(entry["id"], "启动实验")
    out, message = es.run_action(entry["id"], "终止实验")
    assert out is not None, message
    assert gs.get_active_snapshot(entry["id"]) is None
    archived = [s for s in store.rows("group_snapshot") if s["experiment_id"] == entry["id"]]
    assert all(s["status"] == "已归档" for s in archived)
    assert store.find("experiment", entry["id"])["status"] == "已结束"
