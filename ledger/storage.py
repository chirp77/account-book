"""JSON 持久化：校验读取、原子替换与外部修改检测。"""

import json
import os
from pathlib import Path
import tempfile

from .models import MAX_CENTS, Record, parse_month


class StorageError(Exception):
    pass


def _write_atomic(path, raw, prefix):
    temporary_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件和目标文件位于同一目录，写入完整后再原子替换。
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=prefix, suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def _validate_budgets(budgets):
    if not isinstance(budgets, dict):
        raise ValueError("预算内容必须是月份到金额的映射")
    validated = {}
    for month, amount_cents in budgets.items():
        parse_month(month, "预算月份")
        if type(amount_cents) is not int or not 0 < amount_cents <= MAX_CENTS:
            raise ValueError(f"{month} 的预算金额必须是合法的整数分")
        validated[month] = amount_cents
    return validated


class JsonStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._snapshot = None

    def load(self):
        try:
            raw = self.path.read_bytes() if self.path.exists() else None
            if raw is None:
                self._snapshot = None
                return []
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("records"), list):
                raise ValueError("不支持的账本格式")
            records = [Record.from_dict(item) for item in data["records"]]
            if len({record.id for record in records}) != len(records):
                raise ValueError("发现重复记录编号")
            self._snapshot = raw
            return records
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise StorageError(f"账本读取失败，原文件已保留：{exc}") from exc

    def save(self, records):
        try:
            current = self.path.read_bytes() if self.path.exists() else None
            if current != self._snapshot:
                raise StorageError("账本文件已被其他程序修改，请重启本程序后再操作")
            raw = (json.dumps({"version": 1, "records": [r.to_dict() for r in records]},
                              ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            _write_atomic(self.path, raw, ".ledger-")
            self._snapshot = raw
        except OSError as exc:
            raise StorageError("保存失败，请检查账本目录的写入权限和磁盘空间") from exc


class BudgetStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._snapshot = None

    def load(self):
        try:
            raw = self.path.read_bytes() if self.path.exists() else None
            if raw is None:
                self._snapshot = None
                return {}
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("budgets"), dict):
                raise ValueError("不支持的预算格式")
            budgets = _validate_budgets(data["budgets"])
            self._snapshot = raw
            return budgets
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise StorageError(f"预算读取失败，原文件已保留：{exc}") from exc

    def save(self, budgets):
        try:
            current = self.path.read_bytes() if self.path.exists() else None
            if current != self._snapshot:
                raise StorageError("预算文件已被其他程序修改，请重启本程序后再操作")
            validated = _validate_budgets(budgets)
            raw = (json.dumps({"version": 1, "budgets": validated},
                              ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            _write_atomic(self.path, raw, ".budget-")
            self._snapshot = raw
        except OSError as exc:
            raise StorageError("预算保存失败，请检查预算目录的写入权限和磁盘空间") from exc


def validate_backup(data):
    if not isinstance(data, dict):
        raise ValueError('备份文件必须是 JSON 对象')
    if type(data.get('version')) is not int or data['version'] != 1:
        raise ValueError('不支持的备份版本')
    items = data.get('records')
    if not isinstance(items, list):
        raise ValueError('备份记录必须是数组')
    records = []
    seen_ids = set()
    required = {'id', 'date', 'kind', 'amount_cents', 'category', 'note', 'created_at'}
    for index, item in enumerate(items, start=1):
        try:
            if not isinstance(item, dict):
                raise ValueError('记录必须是对象')
            if not required.issubset(item):
                raise ValueError('记录缺少必要字段')
            record = Record.from_dict(item)
            if record.id in seen_ids:
                raise ValueError('存在重复的记录 ID')
            seen_ids.add(record.id)
            records.append(record)
        except (ValueError, TypeError) as exc:
            raise ValueError(f'第 {index} 条记录校验失败：{exc}') from exc
    return records
