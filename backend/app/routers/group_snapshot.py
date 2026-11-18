"""对照实验分群清单接口。

清单由实验启动在事务内自动生成，不提供手工登记入口；本模块负责列表/详情查询、
生命周期动作（归档、恢复、切换）以及 V007 消费端的全量读取与互斥核对。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.group_snapshot import GroupSnapshotService

router = APIRouter(prefix="/api/group_snapshot", tags=["对照实验分群清单"])

service = GroupSnapshotService()

LIST_FIELDS = [
    "清单编号", "实验编号", "方案编号", "方案版本", "分流比例快照", "分流比例版本",
    "对照组样本量", "实验组样本量", "样本总量", "样本总量核对", "互斥规则", "状态",
]
STATUSES = ["生成中", "有效", "已归档"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受清单声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按清单编号检索"),
    status: str | None = Query(default=None, description="生成中、有效、已归档"),
    experiment_id: int | None = Query(default=None, description="按实验方案 id 过滤"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """列表页读取分群确认结果，支持按实验、状态与清单编号过滤。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status,
        experiment_id=experiment_id, page=page, size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    experiment_id: int | None = Query(default=None),
) -> StreamingResponse:
    """导出当前筛选条件下的分群清单，按 V007 留档核对。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status,
        experiment_id=experiment_id, page=1, size=1000000,
    )
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS[:-1], "status"])
    for item in items:
        row = []
        values = [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS[:-1]), item.get("status", "")]
        for value in values:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="group_snapshot.csv"'})


@router.get("/active/{experiment_id}")
def get_active(experiment_id: int) -> dict:
    """消费端读取某实验当前唯一有效的全量分群清单。"""
    snapshot = service.get_active_snapshot(experiment_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"实验 {experiment_id} 暂无有效分群清单")
    return snapshot


@router.post("/consume/{experiment_id}")
def consume_snapshot(experiment_id: int, payload: ActionPayload) -> ActionResult:
    """V007 消费端读取全量清单；存在生成中残留时事务整体回滚并返回失败。"""
    try:
        snapshot = service.consume_snapshot(experiment_id, payload.remark or payload.action or "V007")
    except Exception as exc:  # 残留/无有效清单均返回可读失败，由调用方按回滚处理
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="分群清单消费成功，已按 V007 留痕", entry=snapshot)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """详情页读取单条清单，含样本组、生命周期事件流与因子层占用。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分群清单 {entry_id} 不存在")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """归档、恢复、切换；生成中清单与非法动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
