"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

仓库自带轻量事务（transactional）：业务服务在一个事务里同时改多张表，
任一步骤失败就整体回滚，避免出现「实验状态已变更、分群清单却没生成」的半截数据。
"""
from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class TransactionResidual(RuntimeError):
    """事务提交时检测到残留分群（生成中快照），按 V007 核对要求整体回滚。"""


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._versions: dict[str, int] = {name: 1 for name in self._tables}
        self._created_at: dict[str, float] = {name: time.time() for name in self._tables}
        # 事务支持：只允许一层活动事务，嵌套调用复用同一事务。
        self._tx_active: bool = False
        self._tx_snapshot: dict[str, list[dict[str, Any]]] | None = None
        self._tx_versions: dict[str, int] | None = None
        self._tx_depth: int = 0

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def filter_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        return max((int(r.get("id", 0)) for r in rows), default=0) + 1

    def bump_version(self, module: str) -> int:
        self._versions[module] = self._versions.get(module, 0) + 1
        return self._versions[module]

    def version(self, module: str) -> int:
        return self._versions.get(module, 1)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 轻量事务：状态与分群清单必须同时落库，否则整体回滚 -----------------

    def _begin(self) -> None:
        if self._tx_active:
            self._tx_depth += 1
            return
        self._tx_active = True
        self._tx_depth = 1
        self._tx_snapshot = copy.deepcopy(self._tables)
        self._tx_versions = dict(self._versions)

    def _rollback(self) -> None:
        if not self._tx_active:
            return
        self._tx_depth -= 1
        if self._tx_depth > 0:
            return
        self._tables = copy.deepcopy(self._tx_snapshot)  # type: ignore[arg-type]
        self._versions = dict(self._tx_versions)  # type: ignore[arg-type]
        self._tx_snapshot = None
        self._tx_versions = None
        self._tx_active = False

    def _commit(self) -> None:
        if not self._tx_active:
            return
        self._tx_depth -= 1
        if self._tx_depth > 0:
            return
        self._tx_snapshot = None
        self._tx_versions = None
        self._tx_active = False

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """事务上下文：正常退出提交，抛异常或检测到残留分群时整体回滚。"""
        self._begin()
        try:
            yield
            self._commit()
        except Exception:
            self._rollback()
            raise

    @property
    def in_transaction(self) -> bool:
        return self._tx_active


store = Store()
