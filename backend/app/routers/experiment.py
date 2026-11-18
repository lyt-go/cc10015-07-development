"""实验设计 时间线接口：返回作业时间序列结构。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.experiment import ExperimentService

router = APIRouter(prefix="/api/experiment", tags=["实验设计"])
service = ExperimentService()
LIST_FIELDS = ["方案编号", "实验名称", "对照组说明", "实验组说明", "样本总量", "分流比例", "启动日期", "预期时长"]
STATUSES = ["草稿", "待审批", "运行中", "已结束"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def list_entries() -> dict[str, Any]:
    items = service.get_timeline()
    return {"items": items}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.get_timeline()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=experiment.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"实验方案 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="实验方案已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    options = payload.options or {}
    entry_or_none, message = service.run_action(
        entry_id,
        payload.action,
        ratio_policy=options.get("ratio_policy"),
        fail_after_groups=options.get("fail_after_groups"),
    )
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)


@router.post("/{entry_id}/ratio")
def adjust_ratio(entry_id: int, payload: EntryPayload) -> ActionResult:
    """登记分流比例调整：先挂为待生效版本，下次启动时按冲突策略处理。"""
    new_ratio = str(payload.values.get("分流比例") or "").strip()
    if not new_ratio:
        return ActionResult(ok=False, message="缺少待调整的分流比例")
    entry_or_none, message = service.adjust_ratio(entry_id, new_ratio)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
