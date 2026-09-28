"""HTTP 接口边界：路由、幂等头、错误映射与端到端流程。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from service_09252_008.interfaces.http_api import create_server
from tests.helpers import make_services, seed_catalog


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        catalog, bookings, clock, store = make_services()
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

    def _request(
        self, method: str, path: str, body: dict | None = None, headers: dict | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers or {}
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

    def test_booking_flow_over_http_with_idempotency_header(self) -> None:
        apply_body = {
            "institution": "港城理工学院",
            "package_id": self.ids["package_id"],
            "mentor_id": self.ids["mentor_id"],
            "resource_id": self.ids["resource_id"],
            "window_id": self.ids["window_id"],
            "seats": 6,
            "slot_start": "2026-10-01T02:00:00+00:00",
            "slot_end": "2026-10-01T04:00:00+00:00",
        }
        status, applied = self._request(
            "POST", "/bookings", apply_body, headers={"Idempotency-Key": "http-apply-1"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(applied["status"], "REQUESTED")
        booking_id = applied["booking_id"]

        # 相同幂等键重放：不产生新预约
        status, replay = self._request(
            "POST", "/bookings", apply_body, headers={"Idempotency-Key": "http-apply-1"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(replay["booking_id"], booking_id)
        self.assertTrue(replay["idempotent_replay"])

        # 同一键不同载荷 -> 409
        status, conflict = self._request(
            "POST", "/bookings", {**apply_body, "seats": 8}, headers={"Idempotency-Key": "http-apply-1"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "idempotency_conflict")

        status, quoted = self._request("POST", f"/bookings/{booking_id}/quote", {})
        self.assertEqual(status, 200)
        self.assertEqual(quoted["status"], "QUOTED")

        status, locked = self._request(
            "POST", f"/bookings/{booking_id}/lock", {}, headers={"Idempotency-Key": "http-lock-1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(locked["status"], "LOCKED")

        status, fetched = self._request("GET", f"/bookings/{booking_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["booking_id"], booking_id)
        self.assertEqual(len(fetched["reservations"]), 2)

    def test_error_mapping(self) -> None:
        status, body = self._request("GET", "/bookings/bkg_missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

        status, body = self._request("POST", "/bookings", {"institution": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation_error")

        status, body = self._request("POST", "/no-such-route", {})
        self.assertEqual(status, 404)

    def test_admin_recover_endpoint(self) -> None:
        status, body = self._request("POST", "/admin/recover", {})
        self.assertEqual(status, 200)
        self.assertIn("expired_locks", body)
        self.assertIn("expired_quotes", body)


class PackageFreezeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        catalog, bookings, clock, store = make_services()
        cls.ids = seed_catalog(catalog)
        cls.catalog = catalog
        cls.server = create_server("127.0.0.1", 0, catalog, bookings)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request(
        self, method: str, path: str, body: dict | None = None, headers: dict | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers or {}
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_freeze_read_note_and_replace_rejected_over_http(self) -> None:
        package_id = self.ids["package_id"]

        status, frozen = self._request(
            "POST", f"/packages/{package_id}/freeze", {"reason": "发版冻结"}
        )
        self.assertEqual(status, 200)
        self.assertIn("frozen_checksum", frozen)
        self.assertTrue(frozen["integrity_ok"])
        checksum = frozen["frozen_checksum"]

        # 读取时显示冻结时的校验和
        status, read = self._request("GET", f"/packages/{package_id}")
        self.assertEqual(status, 200)
        self.assertEqual(read["frozen_checksum"], checksum)
        self.assertEqual(read["current_checksum"], checksum)

        # 替换原文件 -> 409 package_frozen
        status, rejected = self._request(
            "POST",
            f"/packages/{package_id}/updates",
            {"event_type": "replace", "name": "替换件"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(rejected["error"], "package_frozen")

        # 只接受修订说明事件
        status, noted = self._request(
            "POST",
            f"/packages/{package_id}/updates",
            {"event_type": "revision_note", "note": "第3页勘误", "author": "教务处"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(noted["revision_notes"][0]["note"], "第3页勘误")
        self.assertEqual(noted["frozen_checksum"], checksum)

        # 未知事件类型 -> 400
        status, bad = self._request(
            "POST",
            f"/packages/{package_id}/updates",
            {"event_type": "overwrite", "note": "x"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(bad["error"], "validation_error")


if __name__ == "__main__":
    unittest.main()
