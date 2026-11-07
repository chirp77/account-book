import json
import csv
from io import StringIO
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app import create_server


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.server = create_server(Path(cls.folder.name) / "ledger.json", 0)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.folder.cleanup()

    def request(self, path, method="GET", payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.url + path, data=body, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    def test_assets_and_private_files(self):
        for path in ["/", "/styles.css", "/app.js", "/favicon.svg"]:
            status, body, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertIn("nosniff", headers["X-Content-Type-Options"])
        for path in ["/data/ledger.json", "/../app.py", "/app.py"]:
            self.assertEqual(self.request(path)[0], 404)

    def test_http_crud_and_validation(self):
        payload = dict(date="2026-11-07", amount="0.10", kind="expense", category="餐饮", note="<script>alert(1)</script>")
        status, body, _ = self.request("/api/records", "POST", payload)
        self.assertEqual(status, 201)
        record = json.loads(body)
        path = f"/api/records/{record['id']}"
        self.assertEqual(self.request(path, "PUT", {**payload, "amount": "0.20"})[0], 200)
        status, body, _ = self.request("/api/records?min=0.20&max=0.20")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["summary"]["expense_cents"], 20)
        self.assertEqual(self.request("/api/records", "POST", {**payload, "amount": "0"})[0], 400)
        self.assertEqual(self.request("/api/records", "POST", [1, 2])[0], 400)
        self.assertEqual(self.request(path, "DELETE")[0], 200)
        self.assertEqual(self.request(path, "DELETE")[0], 404)

    def test_foreign_origin_and_host_rejected(self):
        self.assertEqual(self.request("/api/records", "POST", {}, {"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.request("/api/records", headers={"Host": "other.example"})[0], 403)

    def test_csv_exports_all_filtered_records_safely(self):
        entries = [
            dict(date="2026-11-07", kind="expense", amount="12.30", category="餐饮", note='午餐, "朋友"\n第二行'),
            dict(date="2026-11-06", kind="income", amount="1000", category="工资", note="  =1+1"),
            dict(date="2026-11-05", kind="expense", amount="6", category="交通", note="地铁"),
        ]
        for entry in entries:
            self.assertEqual(self.request("/api/records", "POST", entry)[0], 201)

        status, body, headers = self.request("/api/records.csv?kind=expense&min=6&max=12.30")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/csv")
        self.assertIn("charset=utf-8", headers.get("Content-Type"))
        self.assertIn("attachment; filename=account-book.csv", headers.get("Content-Disposition"))
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["日期", "收支类型", "类别", "金额（元）", "备注"])
        self.assertEqual(len(rows) - 1, 2)
        self.assertEqual({row[2] for row in rows[1:]}, {"餐饮", "交通"})
        self.assertEqual(rows[1][3], "12.30")
        self.assertEqual(rows[1][4], '午餐, "朋友"\n第二行')

        status, body, _ = self.request("/api/records.csv?kind=income")
        rows = list(csv.reader(StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(status, 200)
        self.assertEqual(len(rows) - 1, 1)
        self.assertEqual(rows[1][4], "'=1+1")

        status, body, _ = self.request("/api/records.csv")
        rows = list(csv.reader(StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(status, 200)
        self.assertEqual(len(rows) - 1, 3)

        status, body, _ = self.request("/api/records.csv?" + urlencode({"category": "交通", "min": "999"}))
        self.assertEqual(status, 200)
        self.assertEqual(list(csv.reader(StringIO(body.decode("utf-8-sig")))), [["日期", "收支类型", "类别", "金额（元）", "备注"]])
        self.assertEqual(self.request("/api/records.csv?start=invalid")[0], 400)




    def test_backup_preview_restore_http(self):
        status, body, headers = self.request('/api/backup')
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        backup = json.loads(body)
        status, body, _ = self.request('/api/restore/preview', 'POST', backup)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['valid'])
        self.assertEqual(json.loads(self.request('/api/backup')[1]), backup)
        self.assertEqual(self.request('/api/restore', 'POST', {'version':2,'records':[]})[0], 400)
        self.assertEqual(json.loads(self.request('/api/backup')[1]), backup)
        self.assertEqual(self.request('/api/restore', 'POST', backup)[0], 200)
        self.assertEqual(json.loads(self.request('/api/backup')[1]), backup)


if __name__ == "__main__":
    unittest.main()
