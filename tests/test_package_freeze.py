"""课程包版本冻结：冻结校验和、修订说明只追加、替换原文件被拒绝。

覆盖内存与 SQLite 双后端：
- 冻结时记录内容 SHA-256 校验和，读取时返回并复核；
- 冻结后更新接口只接受 ``revision_note`` 事件，``replace`` 事件拒绝；
- 直接替换底层原文件记录会导致读取时完整性校验失败；
- 冻结状态与校验和写入 SQLite，重启后仍可读取与追加说明。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest

from service_09252_008.application.catalog_service import (
    COLLECTION_PACKAGES,
    CatalogService,
)
from service_09252_008.application.ports import ManualClock, UuidIdGenerator
from service_09252_008.domain.errors import (
    PackageFrozenError,
    PackageIntegrityError,
    StateError,
    ValidationError,
)
from service_09252_008.domain.models import package_content_checksum
from service_09252_008.persistence.sqlite_store import SQLiteStore
from service_09252_008.persistence.store import InMemoryStore
from tests.helpers import NOW, make_services, seed_catalog


def expected_checksum(package: dict) -> str:
    canonical = json.dumps(
        {
            "package_id": package["package_id"],
            "name": package["name"],
            "craft": package["craft"],
            "duration_minutes": package["duration_minutes"],
            "max_seats": package["max_seats"],
            "required_qualifications": package["required_qualifications"],
            "materials": package["materials"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PackageFreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, _bookings, self.clock, self.store = make_services()
        self.ids = seed_catalog(self.catalog)
        self.package_id = self.ids["package_id"]

    def test_unfrozen_package_has_no_freeze_metadata(self) -> None:
        view = self.catalog.get_package(self.package_id)
        self.assertIsNone(view["frozen_at"])
        self.assertIsNone(view["frozen_checksum"])
        self.assertEqual(view["revision_notes"], [])

    def test_freeze_records_checksum_and_read_returns_it(self) -> None:
        before = self.catalog.get_package(self.package_id)
        result = self.catalog.freeze_package(self.package_id, {"reason": "发版冻结"})
        self.assertIsNotNone(result["frozen_at"])
        self.assertEqual(result["frozen_checksum"], expected_checksum(before))
        self.assertEqual(result["checksum_algorithm"], "sha256")
        self.assertTrue(result["integrity_ok"])
        self.assertEqual(result["current_checksum"], result["frozen_checksum"])

        read = self.catalog.get_package(self.package_id)
        self.assertEqual(read["frozen_checksum"], expected_checksum(before))
        self.assertTrue(read["integrity_ok"])
        # 校验和只依赖内容，与登记时间元数据解耦
        self.assertEqual(read["frozen_checksum"], package_content_checksum(before))

    def test_freeze_is_idempotently_rejected_on_second_freeze(self) -> None:
        self.catalog.freeze_package(self.package_id)
        with self.assertRaises(StateError):
            self.catalog.freeze_package(self.package_id)

    def test_freeze_unknown_package_is_not_found(self) -> None:
        from service_09252_008.domain.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.catalog.freeze_package("pkg_missing")

    def test_frozen_package_only_accepts_revision_note_events(self) -> None:
        self.catalog.freeze_package(self.package_id, {"reason": "发版冻结"})
        result = self.catalog.update_package(
            self.package_id,
            {"event_type": "revision_note", "note": "第3页染料配比勘误", "author": "教务处-王"},
        )
        self.assertEqual(len(result["revision_notes"]), 1)
        note = result["revision_notes"][0]
        self.assertEqual(note["event_type"], "revision_note")
        self.assertEqual(note["note"], "第3页染料配比勘误")
        self.assertEqual(note["author"], "教务处-王")
        self.assertIn("note_id", note)
        self.assertIn("created_at", note)
        # 追加说明不改变原文件校验和
        self.assertTrue(result["integrity_ok"])
        self.assertEqual(result["current_checksum"], result["frozen_checksum"])

        # 多条说明只追加，保持先后顺序
        again = self.catalog.update_package(
            self.package_id, {"event_type": "revision_note", "note": "附录补充安全提示"}
        )
        self.assertEqual([n["note"] for n in again["revision_notes"]],
                         ["第3页染料配比勘误", "附录补充安全提示"])

    def test_replace_original_file_event_is_rejected_after_freeze(self) -> None:
        self.catalog.freeze_package(self.package_id)
        for event_type in ("replace", "replace_file", "update_content"):
            with self.subTest(event_type=event_type):
                with self.assertRaises(PackageFrozenError) as ctx:
                    self.catalog.update_package(
                        self.package_id,
                        {
                            "event_type": event_type,
                            "name": "被篡改的课包",
                            "materials": [{"material_id": "dye", "quantity_per_seat": 9.0}],
                        },
                    )
                self.assertEqual(ctx.exception.code, "package_frozen")
        # 拒绝后原内容未被修改，也没有追加任何说明
        view = self.catalog.get_package(self.package_id)
        self.assertEqual(view["name"], "扎染体验课")
        self.assertEqual(view["revision_notes"], [])
        self.assertTrue(view["integrity_ok"])

    def test_unknown_event_type_and_invalid_note_rejected(self) -> None:
        self.catalog.freeze_package(self.package_id)
        with self.assertRaises(ValidationError):
            self.catalog.update_package(self.package_id, {"event_type": "delete_file", "note": "x"})
        with self.assertRaises(ValidationError):
            self.catalog.update_package(self.package_id, {"note": "缺少事件类型"})
        with self.assertRaises(ValidationError):
            self.catalog.update_package(self.package_id, {"event_type": "revision_note"})
        with self.assertRaises(ValidationError):
            self.catalog.update_package(
                self.package_id, {"event_type": "revision_note", "note": "   "}
            )

    def test_update_rejected_before_freeze(self) -> None:
        with self.assertRaises(StateError):
            self.catalog.update_package(
                self.package_id, {"event_type": "revision_note", "note": "未冻结时不接受修订事件"}
            )

    def test_direct_record_tampering_is_detected_on_read(self) -> None:
        frozen = self.catalog.freeze_package(self.package_id)
        frozen_checksum = frozen["frozen_checksum"]

        # 绕过应用接口直接替换底层原文件（模拟替换文件）
        tampered = self.store.get(COLLECTION_PACKAGES, self.package_id)
        assert tampered is not None
        tampered["name"] = "扎染体验课（替换版）"
        tampered["materials"][0]["quantity_per_seat"] = 5.0
        self.store.put(COLLECTION_PACKAGES, self.package_id, tampered)

        with self.assertRaises(PackageIntegrityError) as ctx:
            self.catalog.get_package(self.package_id)
        self.assertEqual(ctx.exception.details["frozen_checksum"], frozen_checksum)
        self.assertNotEqual(ctx.exception.details["current_checksum"], frozen_checksum)

        with self.assertRaises(PackageIntegrityError):
            self.catalog.list(COLLECTION_PACKAGES)
        # 追加说明接口同样拒绝在被替换的文件上操作
        with self.assertRaises(PackageIntegrityError):
            self.catalog.update_package(
                self.package_id, {"event_type": "revision_note", "note": "试图在篡改件上追加"}
            )

    def test_freeze_events_are_recorded(self) -> None:
        self.catalog.freeze_package(self.package_id, {"reason": "发版冻结"})
        events = self.store.query("events", package_id=self.package_id)
        types = [e["type"] for e in events]
        self.assertIn("package_frozen", types)
        self.catalog.update_package(
            self.package_id, {"event_type": "revision_note", "note": "勘误一"}
        )
        events = self.store.query("events", package_id=self.package_id)
        self.assertIn("package_revision_note_added", [e["type"] for e in events])
        # 课程包事件不得污染预约事件流（无 booking_id）
        self.assertTrue(all(e.get("booking_id") is None for e in events))


class PackageFreezeSQLiteTests(unittest.TestCase):
    """冻结校验和写入 SQLite，重启后仍可读取；替换尝试在新进程同样被拒绝。"""

    def test_freeze_persists_across_restart_and_replacement_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/booking.db"
            clock = ManualClock(NOW)

            store = SQLiteStore(db_path)
            catalog = CatalogService(store, clock, UuidIdGenerator())
            ids = seed_catalog(catalog)
            package_id = ids["package_id"]
            before = catalog.get_package(package_id)
            frozen = catalog.freeze_package(package_id, {"reason": "发版冻结"})
            checksum = frozen["frozen_checksum"]
            self.assertEqual(checksum, expected_checksum(before))
            store.close()

            # 模拟重启：全新存储与服务实例挂载同一数据库
            clock.advance(hours=2)
            store2 = SQLiteStore(db_path)
            catalog2 = CatalogService(store2, clock, UuidIdGenerator())
            read = catalog2.get_package(package_id)
            self.assertEqual(read["frozen_checksum"], checksum)
            self.assertTrue(read["integrity_ok"])
            self.assertIsNotNone(read["frozen_at"])

            # 重启后替换原文件仍被拒绝
            with self.assertRaises(PackageFrozenError):
                catalog2.update_package(
                    package_id,
                    {"event_type": "replace", "name": "重启后替换"},
                )
            # 重启后仍可追加修订说明，且校验和保持冻结值
            noted = catalog2.update_package(
                package_id, {"event_type": "revision_note", "note": "重启后追加勘误"}
            )
            self.assertEqual(noted["frozen_checksum"], checksum)
            self.assertTrue(noted["integrity_ok"])
            self.assertEqual(len(noted["revision_notes"]), 1)
            store2.close()

            # 再次重启：说明与冻结校验和依然存在
            store3 = SQLiteStore(db_path)
            catalog3 = CatalogService(store3, clock, UuidIdGenerator())
            again = catalog3.get_package(package_id)
            self.assertEqual(again["frozen_checksum"], checksum)
            self.assertEqual(again["revision_notes"][0]["note"], "重启后追加勘误")
            store3.close()

    def test_tampered_file_detected_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/booking.db"
            clock = ManualClock(NOW)
            store = SQLiteStore(db_path)
            catalog = CatalogService(store, clock, UuidIdGenerator())
            ids = seed_catalog(catalog)
            package_id = ids["package_id"]
            frozen = catalog.freeze_package(package_id)
            store.close()

            # 外部直接改写 SQLite 中的原文件记录
            import sqlite3

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT data FROM records WHERE collection='packages' AND key=?",
                (package_id,),
            ).fetchone()
            record = json.loads(row[0])
            record["max_seats"] = 999
            conn.execute(
                "UPDATE records SET data=? WHERE collection='packages' AND key=?",
                (json.dumps(record, ensure_ascii=False, sort_keys=True), package_id),
            )
            conn.commit()
            conn.close()

            store2 = SQLiteStore(db_path)
            catalog2 = CatalogService(store2, clock, UuidIdGenerator())
            with self.assertRaises(PackageIntegrityError):
                catalog2.get_package(package_id)
            store2.close()


class BackendParityTests(unittest.TestCase):
    def test_in_memory_and_sqlite_behave_identically(self) -> None:
        for store in (InMemoryStore(),):
            catalog = CatalogService(store, ManualClock(NOW), UuidIdGenerator())
            ids = seed_catalog(catalog)
            pid = ids["package_id"]
            catalog.freeze_package(pid)
            with self.assertRaises(PackageFrozenError):
                catalog.update_package(pid, {"event_type": "replace"})
            view = catalog.update_package(
                pid, {"event_type": "revision_note", "note": "n"}
            )
            self.assertTrue(view["integrity_ok"])


if __name__ == "__main__":
    unittest.main()
