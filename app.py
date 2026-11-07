
"""本地 HTTP 入口。运行 python app.py 后打开打印出的地址。"""

import argparse
import csv
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit
import webbrowser

from ledger.service import LedgerService
from ledger.storage import JsonStore, StorageError

ROOT = Path(__file__).resolve().parent


def create_server(data_file, port=8000):
    ledger = LedgerService(JsonStore(data_file))

    class Handler(BaseHTTPRequestHandler):
        server_version = "Youshu/1.0"

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_PUT(self):
            self.dispatch("PUT")

        def do_DELETE(self):
            self.dispatch("DELETE")

        def dispatch(self, method):
            try:
                port = self.server.server_port
                hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

                if self.headers.get("Host") not in hosts:
                    return self.send_json(403, {"error": "仅支持本机访问"})

                if method != "GET":
                    origin = self.headers.get("Origin")
                    if origin and origin not in {
                        f"http://{host}" for host in hosts
                    }:
                        return self.send_json(
                            403, {"error": "不允许跨站修改账本"}
                        )

                url = urlsplit(self.path)

                # Issue #6：导出完整 JSON 备份
                if method == "GET" and url.path == "/api/backup":
                    backup = ledger.export_backup()
                    body = (
                        json.dumps(
                            backup, ensure_ascii=False, indent=2
                        ) + "\n"
                    ).encode("utf-8")
                    return self.respond(
                        200,
                        body,
                        "application/json; charset=utf-8",
                        "attachment; filename=account-book-backup.json",
                    )

                # Issue #6：校验并恢复 JSON 备份
                if method == "POST" and url.path == "/api/restore":
                    if self.headers.get_content_type() != "application/json":
                        raise ValueError("备份文件必须是 JSON 格式")

                    try:
                        length = int(
                            self.headers.get("Content-Length", "0")
                        )
                    except ValueError:
                        raise ValueError("文件大小无效") from None

                    if not 0 < length <= 5 * 1024 * 1024:
                        raise ValueError("备份文件不能为空或超过 5MB")

                    try:
                        raw = self.rfile.read(length)
                        data = json.loads(raw.decode("utf-8-sig"))
                    except (UnicodeError, json.JSONDecodeError):
                        raise ValueError("备份文件不是有效的 JSON") from None

                    result = ledger.restore_backup(data)
                    return self.send_json(200, result)

                # 原有记录查询
                if method == "GET" and url.path == "/api/records":
                    filters = {
                        key: values[-1]
                        for key, values in parse_qs(url.query).items()
                    }
                    return self.send_json(200, ledger.query(filters))

                # 原有 CSV 导出
                if method == "GET" and url.path == "/api/records.csv":
                    filters = {
                        key: values[-1]
                        for key, values in parse_qs(url.query).items()
                    }
                    return self.send_csv(ledger.query(filters)["records"])

                # 新增记录
                if method == "POST" and url.path == "/api/records":
                    return self.send_json(201, ledger.add(self.read_json()))

                # 修改或删除记录
                if url.path.startswith("/api/records/") and method in (
                    "PUT", "DELETE"
                ):
                    record_id = url.path.removeprefix("/api/records/")
                    if method == "PUT":
                        return self.send_json(
                            200, ledger.edit(record_id, self.read_json())
                        )
                    ledger.delete(record_id)
                    return self.send_json(200, {"ok": True})

                # 原有静态网页
                if method == "GET":
                    assets = {
                        "/": ("index.html", "text/html; charset=utf-8"),
                        "/styles.css": (
                            "styles.css", "text/css; charset=utf-8"
                        ),
                        "/app.js": (
                            "app.js", "text/javascript; charset=utf-8"
                        ),
                        "/favicon.svg": (
                            "favicon.svg", "image/svg+xml"
                        ),
                    }
                    if url.path in assets:
                        name, content_type = assets[url.path]
                        return self.respond(
                            200,
                            (ROOT / "static" / name).read_bytes(),
                            content_type,
                        )

                self.send_json(404, {"error": "页面或接口不存在"})

            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except KeyError as exc:
                self.send_json(404, {"error": exc.args[0]})
            except StorageError as exc:
                self.send_json(409, {"error": str(exc)})
            except OSError:
                self.send_json(
                    500,
                    {"error": "文件操作失败，请查看终端并检查文件是否完整"},
                )

        def read_json(self):
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请求内容必须为 JSON")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16_384:
                raise ValueError("请求内容为空或过大")
            try:
                return json.loads(
                    self.rfile.read(length).decode("utf-8")
                )
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError("JSON 内容格式错误") from None

        def send_json(self, status, data):
            self.respond(
                status,
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def send_csv(self, records):
            output = StringIO(newline="")
            writer = csv.writer(output, lineterminator="\r\n")
            writer.writerow(["日期", "收支类型", "类别", "金额（元）", "备注"])
            for record in records:
                kind = "收入" if record["kind"] == "income" else "支出"
                writer.writerow([
                    record["date"],
                    kind,
                    _safe_spreadsheet_text(record["category"]),
                    f"{Decimal(record['amount_cents']) / 100:.2f}",
                    _safe_spreadsheet_text(record["note"]),
                ])
            body = b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")
            self.respond(
                200,
                body,
                "text/csv; charset=utf-8",
                "attachment; filename=account-book.csv",
            )

        def respond(
            self, status, body, content_type, content_disposition=None
        ):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if content_disposition:
                self.send_header(
                    "Content-Disposition", content_disposition
                )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def _safe_spreadsheet_text(value):
    """Prevent user-controlled text from becoming an Excel formula."""
    if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def main():
    parser = argparse.ArgumentParser(
        description="有数记账 · 本地个人账本"
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--data", type=Path, default=ROOT / "data" / "ledger.json"
    )
    parser.add_argument(
        "--open", action="store_true", help="启动时打开浏览器"
    )
    args = parser.parse_args()

    try:
        server = create_server(args.data, args.port)
    except (StorageError, OSError, OverflowError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{server.server_port}"
    print(
        f"有数记账已启动：{url}\n"
        f"数据文件：{args.data.resolve()}\n"
        "按 Ctrl+C 停止服务。",
        flush=True,
    )

    if args.open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
