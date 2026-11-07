"""记账、查询、预算和统计。HTTP 界面之外也可以独立调用。"""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from threading import RLock

from .models import CATEGORIES, Record, parse_amount, parse_date, parse_month
from pathlib import Path
import os
import tempfile
from .storage import StorageError, validate_backup


class LedgerService:
    def __init__(self, store, budget_store=None):
        self.store = store
        self.budget_store = budget_store
        self.records = store.load()
        self.budgets = budget_store.load() if budget_store else {}
        self.lock = RLock()

    def add(self, payload):
        record = Record.create(payload)
        with self.lock:
            updated = self.records + [record]
            self.store.save(updated)
            self.records = updated
        return record.to_dict()

    def edit(self, record_id, payload):
        with self.lock:
            old = self._find(record_id)
            record = Record.create(payload, existing=old)
            updated = [record if r.id == record_id else r for r in self.records]
            self.store.save(updated)
            self.records = updated
            return record.to_dict()

    def delete(self, record_id):
        with self.lock:
            self._find(record_id)
            updated = [r for r in self.records if r.id != record_id]
            self.store.save(updated)
            self.records = updated

    def _find(self, record_id):
        for record in self.records:
            if record.id == record_id:
                return record
        raise KeyError("这笔记录已不存在，请刷新页面")

    def set_budget(self, month, payload):
        month = parse_month(month)
        if not isinstance(payload, dict):
            raise ValueError("预算内容必须是 JSON 对象")
        amount_cents = parse_amount(payload.get("amount"))
        if self.budget_store is None:
            raise StorageError("预算存储未配置")
        with self.lock:
            updated = {**self.budgets, month: amount_cents}
            self.budget_store.save(updated)
            self.budgets = updated
            return self._budget_status(month, self._month_expense_total(month))

    def get_budget(self, month):
        month = parse_month(month)
        with self.lock:
            return self._budget_status(month, self._month_expense_total(month))

    def month_expense_total(self, month):
        month = parse_month(month)
        with self.lock:
            return self._month_expense_total(month)

    def _month_expense_total(self, month):
        return sum(
            record.amount_cents
            for record in self.records
            if record.kind == "expense" and record.date[:7] == month
        )

    def _budget_status(self, month, spent_cents):
        budget_cents = self.budgets.get(month)
        if budget_cents is None:
            return {
                "month": month,
                "has_budget": False,
                "budget_cents": None,
                "spent_cents": spent_cents,
                "remaining_cents": None,
                "usage_percent": None,
                "overspent_cents": 0,
            }
        remaining_cents = budget_cents - spent_cents
        usage_percent = float(
            (Decimal(spent_cents) * 100 / Decimal(budget_cents)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        return {
            "month": month,
            "has_budget": True,
            "budget_cents": budget_cents,
            "spent_cents": spent_cents,
            "remaining_cents": remaining_cents,
            "usage_percent": usage_percent,
            "overspent_cents": max(0, spent_cents - budget_cents),
        }

    def query(self, filters):
        start, end = filters.get("start", ""), filters.get("end", "")
        if start:
            parse_date(start, "开始日期")
        if end:
            parse_date(end, "结束日期")
        if start and end and start > end:
            raise ValueError("开始日期不能晚于结束日期")
        low = parse_amount(filters["min"], allow_zero=True) if filters.get("min") else None
        high = parse_amount(filters["max"], allow_zero=True) if filters.get("max") else None
        if low is not None and high is not None and low > high:
            raise ValueError("最低金额不能大于最高金额")
        kind, category = filters.get("kind", ""), filters.get("category", "")
        if kind and kind not in CATEGORIES:
            raise ValueError("收支类型无效")
        if category and category not in sum(CATEGORIES.values(), []):
            raise ValueError("类别无效")
        search = filters.get("q", "").strip().casefold()
        with self.lock:
            selected = [r for r in self.records if
                        (not start or r.date >= start) and (not end or r.date <= end)
                        and (low is None or r.amount_cents >= low)
                        and (high is None or r.amount_cents <= high)
                        and (not kind or r.kind == kind) and (not category or r.category == category)
                        and (not search or search in (r.note + " " + r.category).casefold())]
            total_count = len(self.records)
        selected.sort(key=lambda r: (r.date, r.created_at, r.id), reverse=True)
        income = sum(r.amount_cents for r in selected if r.kind == "income")
        expense = sum(r.amount_cents for r in selected if r.kind == "expense")
        by_category = defaultdict(int)
        for record in selected:
            if record.kind == "expense":
                by_category[record.category] += record.amount_cents
        return {
            "records": [r.to_dict() for r in selected],
            "summary": {"income_cents": income, "expense_cents": expense,
                        "balance_cents": income - expense, "count": len(selected)},
            "expense_categories": [{"category": name, "amount_cents": amount}
                                   for name, amount in sorted(by_category.items(), key=lambda item: (-item[1], item[0]))],
            "categories": CATEGORIES, "total_count": total_count,
        }

    def export_backup(self):
        with self.lock:
            return {'version': 1, 'records': [record.to_dict() for record in self.records]}

    def restore_backup(self, data):
        records = validate_backup(data)
        with self.lock:
            temporary_path = None
            backup_path = Path(str(self.store.path) + '.before-restore.bak')
            try:
                old_raw = self.store.path.read_bytes() if self.store.path.exists() else None
                if old_raw != self.store._snapshot:
                    raise StorageError('账本已被其他程序修改，请重新加载后重试')
                if old_raw is not None:
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=backup_path.parent,
                            prefix='.backup-', suffix='.tmp', delete=False) as handle:
                        temporary_path = Path(handle.name)
                        handle.write(old_raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary_path, backup_path)
                    temporary_path = None
                self.store.save(records)
                self.records = records
            except OSError as exc:
                raise StorageError('恢复失败，原账本未被替换') from exc
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        return {'ok': True, 'restored_count': len(records)}

    def preview_backup(self, data):
        records = validate_backup(data)
        dates = sorted(record.date for record in records)
        return {"valid": True, "count": len(records),
                "start": dates[0] if dates else None, "end": dates[-1] if dates else None}
