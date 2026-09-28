"""课程包版本冻结：冻结校验和、只追加修订说明、拒绝替换原文件。

覆盖内存与 SQLite 双后端：冻结校验和写入 SQLite 后重启仍可读取。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from service_09252_008.interfaces.http_api import create_server
from tests.helpers import make_services, seed_catalog

from service_09252_008.application.catalog_service import COLLECTION_PACKAGE_FREEZES
from service_09252_008.application.ports import ManualClock, SequentialIdGenerator
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.domain.errors import PackageFrozenError, StateError, ValidationError
from service_09252_008.domain.models import package_content_checksum
from service_09252_008.persistence.sqlite_store import SQLiteStore
from service_09252_008.persistence.store import InMemoryStore, Store
from tests.helpers import NOW, make_services, seed_catalog


def _package_payload() -> dict:
    return {
        "name": "扎染体验课",
        "craft": "扎染",
        "duration_minutes": 120,
        "max_seats": 20,
        "required_qualifications": ["tie-dye-basic"],
        "materials": [
            {"material_id": "dye", "quantity_per_seat": 0.5},
            {"material_id": "cloth", "quantity_per_seat": 1.0},
        ],
    }


class _FreezeCaseMixin:
    """双后端共用用例。"""

    store: Store

    def _catalog(self) -> CatalogService:
        return CatalogService(self.store, ManualClock(NOW), SequentialIdGenerator())

    def setUp(self) -> None:  # noqa: D401 - unittest 钩子
        raise NotImplementedError

    def test_freeze_records_checksum_and_read_view(self) -> None:
        catalog = self._catalog()
        package = catalog.create_package(_package_payload())
        pid = package["package_id"]
        expected = package_content_checksum(package)

        view = catalog.freeze_package(pid, {"revision_note": "发版冻结"})
        self.assertTrue(view["frozen"])
        self.assertEqual(view["frozen_checksum"], expected)
        self.assertEqual(view["checksum_algorithm"], "sha256")
        self.assertEqual(view["current_checksum"], expected)
        self.assertTrue(view["content_intact"])
        # 冻结时的首条修订说明被保留
        self.assertEqual([n["summary"] for n in view["revision_notes"]], ["发版冻结"])

        # 冻结记录独立持久化
        freeze_record = self.store.get(COLLECTION_PACKAGE_FREEZES, pid)
        self.assertIsNotNone(freeze_record)
        self.assertEqual(freeze_record["checksum"], expected)
        self.assertEqual(freeze_record["algorithm"], "sha256")
        self.assertTrue(freeze_record["frozen_at"])

    def test_unfrozen_read_view_has_no_checksum(self) -> None:
        catalog = self._catalog()
        package = catalog.create_package(_package_payload())
        view = catalog.get_package(package["package_id"])
        self.assertFalse(view["frozen"])
        self.assertIsNone(view["frozen_checksum"])
        self.assertIsNone(view["content_intact"])
        self.assertIsNone(view["checksum_algorithm"])

    def test_freeze_is_idempotently_rejected_on_retry(self) -> None:
        catalog = self._catalog()
        pid = catalog.create_package(_package_payload())["package_id"]
        catalog.freeze_package(pid)
        with self.assertRaises(StateError):
            catalog.freeze_package(pid)

    def test_frozen_package_rejects_material_replacements(self) -> None:
        catalog = self._catalog()
        pid = catalog.create_package(_package_payload())["package_id"]
        catalog.freeze_package(pid)

        # 替换原文件（materials 全量替换）必须被拒绝
        with self.assertRaises(PackageFrozenError) as ctx:
            catalog.update_package(
                pid,
                {"materials": [{"material_id": "new-dye", "quantity_per_seat": 0.9}]},
            )
        self.assertEqual(ctx.exception.details["rejected_fields"], ["materials"])
        self.assertEqual(ctx.exception.details["frozen_checksum"], catalog.get_package(pid)["frozen_checksum"])

        # 任何内容字段的修改同样被拒绝（名称、用量、资格等）
        for field_name, value in (
            ("name", "新课名"),
            ("craft", "蜡染"),
            ("duration_minutes", 90),
            ("max_seats", 30),
            ("required_qualifications", []),
        ):
            with self.assertRaises(PackageFrozenError):
                catalog.update_package(pid, {field_name: value})

        # 被拒后原文件纹丝不动，校验和仍然一致
        view = catalog.get_package(pid)
        self.assertEqual(view["materials"], _package_payload()["materials"])
        self.assertTrue(view["content_intact"])

        # 未知字段同样拒绝（更新接口只接受说明事件），不得静默忽略
        with self.assertRaises(PackageFrozenError):
            catalog.update_package(pid, {"unexpected_field": 1, "revision_note": "夹带内容"})
        view = catalog.get_package(pid)
        self.assertEqual(view["materials"], _package_payload()["materials"])

    def test_frozen_package_accepts_revision_note_events_only(self) -> None:
        catalog = self._catalog()
        pid = catalog.create_package(_package_payload())["package_id"]
        frozen = catalog.freeze_package(pid)
        original_checksum = frozen["frozen_checksum"]

        # 说明事件（对象形式）
        view = catalog.update_package(
            pid,
            {
                "revision_note": {
                    "summary": "勘误：第3页步骤描述",
                    "detail": "将“浸泡10分钟”更正为“浸泡20分钟”",
                    "author": "教务处-王老师",
                }
            },
        )
        self.assertEqual(len(view["revision_notes"]), 1)
        self.assertEqual(view["revision_notes"][0]["detail"], "将“浸泡10分钟”更正为“浸泡20分钟”")
        # 追加说明不改变内容校验和
        self.assertEqual(view["frozen_checksum"], original_checksum)
        self.assertTrue(view["content_intact"])

        # 字符串简写形式
        view = catalog.update_package(pid, {"revision_note": "第二条勘误"})
        self.assertEqual([n["summary"] for n in view["revision_notes"]], ["勘误：第3页步骤描述", "第二条勘误"])

        # 专用追加端点
        view = catalog.add_revision_note(pid, {"revision_note": {"summary": "第三条说明"}})
        self.assertEqual(len(view["revision_notes"]), 3)
        self.assertEqual(view["frozen_checksum"], original_checksum)

        # 既无内容也无说明 -> 400
        with self.assertRaises(ValidationError):
            catalog.update_package(pid, {})
        # 空说明 -> 400
        with self.assertRaises(ValidationError):
            catalog.add_revision_note(pid, {"revision_note": "   "})

    def test_frozen_checksum_independent_of_revision_notes(self) -> None:
        catalog = self._catalog()
        pid = catalog.create_package(_package_payload())["package_id"]
        frozen = catalog.freeze_package(pid)
        catalog.add_revision_note(pid, {"revision_note": "补充说明A"})
        catalog.add_revision_note(pid, {"revision_note": "补充说明B"})
        view = catalog.get_package(pid)
        self.assertEqual(view["frozen_checksum"], frozen["frozen_checksum"])
        self.assertEqual(view["current_checksum"], frozen["frozen_checksum"])
        self.assertTrue(view["content_intact"])

    def test_unfrozen_package_still_editable(self) -> None:
        catalog = self._catalog()
        pid = catalog.create_package(_package_payload())["package_id"]
        view = catalog.update_package(pid, {"max_seats": 25, "revision_note": "冻结前调整席位"})
        self.assertEqual(view["max_seats"], 25)
        self.assertEqual(len(view["revision_notes"]), 1)


class InMemoryFreezeTests(_FreezeCaseMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()


class SQLiteFreezeTests(_FreezeCaseMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "freeze.db"
        self.store = SQLiteStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_checksum_persists_across_restart(self) -> None:
        catalog = self._catalog()
        pid = catalog.create_package(_package_payload())["package_id"]
        frozen = catalog.freeze_package(pid, {"revision_note": "发版冻结"})
        expected = frozen["frozen_checksum"]
        self.store.close()

        # 重启：新存储实例指向同一数据库文件
        reopened = SQLiteStore(self.db_path)
        try:
            restarted_catalog = CatalogService(reopened, ManualClock(NOW), SequentialIdGenerator())
            view = restarted_catalog.get_package(pid)
            self.assertTrue(view["frozen"])
            self.assertEqual(view["frozen_checksum"], expected)
            self.assertTrue(view["content_intact"])
            self.assertEqual([n["summary"] for n in view["revision_notes"]], ["发版冻结"])

            # 重启后替换原文件仍被拒绝
            with self.assertRaises(PackageFrozenError):
                restarted_catalog.update_package(
                    pid, {"materials": [{"material_id": "x", "quantity_per_seat": 1.0}]}
                )
            # 重启后仍可追加说明
            view = restarted_catalog.add_revision_note(pid, {"revision_note": "重启后的勘误"})
            self.assertEqual([n["summary"] for n in view["revision_notes"]], ["发版冻结", "重启后的勘误"])

            # 直接查 SQLite 行，校验和确实落库
            import sqlite3

            conn = sqlite3.connect(str(self.db_path))
            try:
                row = conn.execute(
                    "SELECT data FROM records WHERE collection = ? AND key = ?",
                    (COLLECTION_PACKAGE_FREEZES, pid),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row)
            stored = json.loads(row[0])
            self.assertEqual(stored["checksum"], expected)
        finally:
            reopened.close()


class ExistingCatalogCompatibilityTests(unittest.TestCase):
    """冻结字段为可选：既有 fixtures / 预约流程不受影响。"""

    def test_seed_and_booking_still_works(self) -> None:
        catalog, bookings, clock, store = make_services()
        ids = seed_catalog(catalog)
        package_view = catalog.get_package(ids["package_id"])
        self.assertFalse(package_view["frozen"])
        applied = bookings.apply(
            {
                "idempotency_key": "k1",
                "institution": "城北学院",
                "package_id": ids["package_id"],
                "mentor_id": ids["mentor_id"],
                "resource_id": ids["resource_id"],
                "window_id": ids["window_id"],
                "seats": 10,
                "slot_start": "2026-10-01T02:00:00+00:00",
                "slot_end": "2026-10-01T04:00:00+00:00",
            }
        )
        self.assertEqual(applied["status"], "REQUESTED")


class FreezeHttpApiTests(unittest.TestCase):
    """HTTP 边界：冻结端点展示校验和，替换原文件返回 409。"""

    @classmethod
    def setUpClass(cls) -> None:
        catalog, bookings, clock, store = make_services()
        cls.catalog = catalog
        cls.ids = seed_catalog(catalog)
        cls.server = create_server("127.0.0.1", 0, catalog, bookings)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_freeze_and_reject_replacement_over_http(self) -> None:
        pid = self.ids["package_id"]

        # 冻结
        status, frozen = self._request("POST", f"/packages/{pid}/freeze", {"revision_note": "版本冻结"})
        self.assertEqual(status, 200)
        self.assertTrue(frozen["frozen"])
        checksum = frozen["frozen_checksum"]
        self.assertRegex(checksum, r"^[0-9a-f]{64}$")

        # GET 读取展示冻结时校验和
        status, view = self._request("GET", f"/packages/{pid}")
        self.assertEqual(status, 200)
        self.assertEqual(view["frozen_checksum"], checksum)
        self.assertTrue(view["content_intact"])
        self.assertEqual(view["checksum_algorithm"], "sha256")

        # PUT 替换原文件 -> 409 package_frozen
        status, err = self._request(
            "PUT", f"/packages/{pid}", {"materials": [{"material_id": "x", "quantity_per_seat": 1}]}
        )
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "package_frozen")
        self.assertIn("materials", err["details"]["rejected_fields"])

        # PUT 仅说明事件 -> 200，校验和不变
        status, ok = self._request("PUT", f"/packages/{pid}", {"revision_note": "勘误说明"})
        self.assertEqual(status, 200)
        self.assertEqual(ok["frozen_checksum"], checksum)
        self.assertTrue(ok["content_intact"])

        # 重复冻结 -> 409
        status, err = self._request("POST", f"/packages/{pid}/freeze", {})
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
