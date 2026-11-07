import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ledger.service import LedgerService
from ledger.storage import JsonStore, StorageError

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'ledger.json'
        self.service = LedgerService(JsonStore(self.path))
        self.service.add(dict(date='2026-11-07', amount='12.50', kind='expense', category='餐饮', note='午餐'))
        self.backup = self.service.export_backup()
        self.raw = self.path.read_bytes()

    def test_preview_does_not_write_and_restore_survives_restart(self):
        self.assertEqual(self.service.preview_backup(self.backup), dict(valid=True, count=1, start='2026-11-07', end='2026-11-07'))
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.service.restore_backup(dict(version=1, records=[]))
        self.assertEqual(self.service.query({})['summary']['count'], 0)
        self.assertEqual(Path(str(self.path)+'.before-restore.bak').read_bytes(), self.raw)
        self.service.restore_backup(self.backup)
        self.assertEqual(LedgerService(JsonStore(self.path)).export_backup(), self.backup)

    def test_invalid_backups_preserve_disk_and_memory(self):
        invalid = [None, dict(version=True, records=[]), dict(version=2, records=[]),
                   dict(version=1, records=self.backup['records']*2)]
        for field, value in [('amount_cents',-1),('date','2026-02-30'),('category','不存在'),('id','')]:
            record = {**self.backup['records'][0],field:value}
            invalid.append(dict(version=1, records=[record]))
        for data in invalid:
            for method in [self.service.preview_backup, self.service.restore_backup]:
                with self.assertRaises(ValueError): method(data)
                self.assertEqual(self.path.read_bytes(), self.raw)
                self.assertEqual(self.service.export_backup(), self.backup)

    def test_write_failure_preserves_disk_and_memory(self):
        with patch('ledger.storage.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(StorageError):
                self.service.restore_backup(dict(version=1, records=[]))
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self.service.export_backup(), self.backup)
