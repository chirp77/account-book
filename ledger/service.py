
"""账本业务逻辑：记录管理、查询、备份与恢复。"""

from collections import defaultdict
from pathlib import Path
from threading import RLock
import os
import tempfile

from .models import CATEGORIES, Record, parse_amount, parse_date
from .storage import StorageError, validate_backup


class LedgerService:
    def __init__(self, store):
        self.store = store
        self.records = store.load()
        self.lock = RLock()

    def add(self, payload):
        """新增记录。"""
        record = Record.create(payload)

        with self.lock:
            updated = self.records + [record]
            self.store.save(updated)
            self.records = updated

        return record.to_dict()

    def edit(self, record_id, payload):
        """修改记录，保留原 ID 和创建时间。"""
        with self.lock:
            old = self._find(record_id)

            data = old.to_dict()
            data.update(payload)
            data["id"] = old.id
            data["created_at"] = old.created_at

            record = Record.create(data)
            record = Record(
                id=old.id,
                date=record.date,
                kind=record.kind,
                amount_cents=record.amount_cents,
                category=record.category,
                note=record.note,
                created_at=old.created_at,
            )

            updated = [
                record if item.id == record_id else item
                for item in self.records
            ]

            self.store.save(updated)
            self.records = updated

            return record.to_dict()

    def delete(self, record_id):
        """删除记录。"""
        with self.lock:
            self._find(record_id)

            updated = [
                item for item in self.records
                if item.id != record_id
            ]

            self.store.save(updated)
            self.records = updated

            return {"ok": True}

    def _find(self, record_id):
        """根据 ID 查找记录。"""
        for record in self.records:
            if record.id == record_id:
                return record

        raise KeyError("记录不存在")

    def query(self, filters=None):
        """查询记录并计算收支汇总。"""
        filters = filters or {}

        with self.lock:
            records = list(self.records)

        kind = filters.get("kind")
        category = filters.get("category")
        start = filters.get("start") or filters.get("start_date")
        end = filters.get("end") or filters.get("end_date")
        keyword = filters.get("keyword") or filters.get("q")

        if kind:
            if kind not in CATEGORIES:
                raise ValueError("收支类型无效")
            records = [
                item for item in records
                if item.kind == kind
            ]

        if category:
            records = [
                item for item in records
                if item.category == category
            ]

        if start:
            start = parse_date(start)
            records = [
                item for item in records
                if item.date >= start
            ]

        if end:
            end = parse_date(end)
            records = [
                item for item in records
                if item.date <= end
            ]

        if keyword:
            records = [
                item for item in records
                if keyword in item.note
                or keyword in item.category
            ]

        records.sort(
            key=lambda item: (item.date, item.created_at),
            reverse=True,
        )

        income = sum(
            item.amount_cents
            for item in records
            if item.kind == "income"
        )
        expense = sum(
            item.amount_cents
            for item in records
            if item.kind == "expense"
        )

        category_totals = defaultdict(int)

        for item in records:
            category_totals[item.category] += item.amount_cents

        return {
            "records": [
                item.to_dict() for item in records
            ],
            "summary": {
                "income_cents": income,
                "expense_cents": expense,
                "balance_cents": income - expense,
            },
            "category_totals": dict(category_totals),
        }

    def export_backup(self):
        """导出完整 JSON 账本。"""
        with self.lock:
            return {
                "version": 1,
                "records": [
                    record.to_dict()
                    for record in self.records
                ],
            }

    def restore_backup(self, data):
        """校验并恢复账本，恢复前保存旧账本。"""

        # 先校验全部数据，校验失败不修改账本
        records = validate_backup(data)

        with self.lock:
            temporary_path = None
            backup_path = Path(
                str(self.store.path) + ".before-restore.bak"
            )

            try:
                # 读取磁盘上的旧账本
                old_raw = (
                    self.store.path.read_bytes()
                    if self.store.path.exists()
                    else None
                )

                # 防止覆盖被其他程序修改的数据
                if old_raw != self.store._snapshot:
                    raise StorageError(
                        "账本已被其他程序修改，请重新加载后重试"
                    )

                # 恢复前保留旧账本
                if old_raw is not None:
                    backup_path.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    with tempfile.NamedTemporaryFile(
                        dir=backup_path.parent,
                        prefix=".backup-",
                        suffix=".tmp",
                        delete=False,
                    ) as handle:
                        temporary_path = Path(handle.name)
                        handle.write(old_raw)
                        handle.flush()
                        os.fsync(handle.fileno())

                    os.replace(
                        temporary_path,
                        backup_path,
                    )
                    temporary_path = None

                # JsonStore.save 使用原子替换写入
                self.store.save(records)

                # 只有保存成功才更新内存
                self.records = records

            except OSError as exc:
                raise StorageError(
                    "恢复失败，原账本未被替换"
                ) from exc

            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

        return {
            "ok": True,
            "restored_count": len(records),
        }
