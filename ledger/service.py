"""记账、查询和统计。HTTP 界面之外也可以独立调用。"""

from collections import defaultdict
from threading import RLock

from .models import CATEGORIES, Record, parse_amount, parse_date


class LedgerService:
    def __init__(self, store):
        self.store = store
        self.records = store.load()
        self.lock = RLock()

    
    def export_backup(self):
        """导出完整账本，保持现有 JSON 格式。"""
        with self.lock:
            return {
                "version": 1,
                "records": [
                    record.to_dict()
                    for record in self.records
                ],
            }

    def restore_backup(self, data):
        """校验后全量恢复账本。"""
        from .storage import validate_backup

        # 先完成所有数据校验
        records = validate_backup(data)

        # 使用原有存储逻辑进行原子保存
        with self.lock:
            self.store.save(records)

            # 保存成功后才更新内存
            self.records = records

        return {
            "ok": True,
            "restored_count": len(records),
        }


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
