"""目录服务：课程包、导师、工坊资源、材料批次、接待窗口的登记。"""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.errors import (
    NotFoundError,
    PackageFrozenError,
    PackageIntegrityError,
    StateError,
    ValidationError,
)
from ..domain.models import (
    PACKAGE_CHECKSUM_ALGORITHM,
    CoursePackage,
    MaterialBatch,
    MaterialRequirement,
    MaterialSafety,
    Mentor,
    ReceptionWindow,
    WorkshopResource,
    dt_from_str,
    dt_to_str,
)
from ..persistence.store import Store
from .ports import Clock, IdGenerator

COLLECTION_PACKAGES = "packages"
COLLECTION_MENTORS = "mentors"
COLLECTION_RESOURCES = "resources"
COLLECTION_BATCHES = "material_batches"
COLLECTION_WINDOWS = "reception_windows"
COLLECTION_EVENTS = "events"

#: 更新接口接受的事件类型：课程包修订说明（只追加，不触碰原文件）
EVENT_REVISION_NOTE = "revision_note"

#: 冻结后必须拒绝的“替换原文件”类事件
REPLACE_EVENT_TYPES = frozenset({"replace", "replace_file", "update_content", "material_replace"})


def _require(data: dict[str, Any], field: str) -> Any:
    value = data.get(field)
    if value is None:
        raise ValidationError(f"missing required field: {field}", details={"field": field})
    return value


def _require_int(data: dict[str, Any], field: str, *, minimum: int | None = None) -> int:
    value = _require(data, field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"field {field} must be an integer", details={"field": field})
    if minimum is not None and value < minimum:
        raise ValidationError(f"field {field} must be >= {minimum}", details={"field": field, "minimum": minimum})
    return value


def _require_number(data: dict[str, Any], field: str, *, minimum: float | None = None) -> float:
    value = _require(data, field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"field {field} must be a number", details={"field": field})
    result = float(value)
    if minimum is not None and result < minimum:
        raise ValidationError(f"field {field} must be >= {minimum}", details={"field": field, "minimum": minimum})
    return result


def _require_str(data: dict[str, Any], field: str) -> str:
    value = _require(data, field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"field {field} must be a non-empty string", details={"field": field})
    return value.strip()


def _require_tz(data: dict[str, Any], field: str) -> str:
    tz = _require_str(data, field)
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"unknown IANA timezone: {tz}", details={"field": field}) from exc
    return tz


def _require_safety(data: dict[str, Any], field: str) -> MaterialSafety:
    value = _require(data, field)
    try:
        return MaterialSafety(int(value))
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            f"field {field} must be a valid safety level (1-3)", details={"field": field}
        ) from exc


def _require_time(data: dict[str, Any], field: str):
    value = _require(data, field)
    try:
        return dt_from_str(value)
    except ValueError as exc:
        raise ValidationError(f"field {field} must be an ISO-8601 datetime with offset: {exc}") from exc


class CatalogService:
    """维护课程包、导师资格、工坊资源、材料批次与接待窗口。"""

    def __init__(self, store: Store, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    # -- 课程包 ------------------------------------------------------------

    def create_package(self, data: dict[str, Any]) -> dict[str, Any]:
        materials_raw = data.get("materials", [])
        if not isinstance(materials_raw, list):
            raise ValidationError("field materials must be a list")
        materials = []
        for item in materials_raw:
            if not isinstance(item, dict):
                raise ValidationError("each material requirement must be an object")
            materials.append(
                MaterialRequirement(
                    material_id=_require_str(item, "material_id"),
                    quantity_per_seat=_require_number(item, "quantity_per_seat", minimum=0.0),
                )
            )
        quals_raw = data.get("required_qualifications", [])
        if not isinstance(quals_raw, list) or not all(isinstance(q, str) for q in quals_raw):
            raise ValidationError("field required_qualifications must be a list of strings")
        package = CoursePackage(
            package_id=self._ids.new_id("pkg"),
            name=_require_str(data, "name"),
            craft=_require_str(data, "craft"),
            duration_minutes=_require_int(data, "duration_minutes", minimum=1),
            max_seats=_require_int(data, "max_seats", minimum=1),
            required_qualifications=list(quals_raw),
            materials=materials,
            created_at=self._clock.now(),
        )
        with self._store.transaction():
            self._store.put(COLLECTION_PACKAGES, package.package_id, package.to_dict())
        return package.to_dict()

    # -- 课程包版本冻结 ----------------------------------------------------

    def freeze_package(self, package_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        """冻结课程包版本：记录冻结时刻与内容校验和，此后原文件不可替换。"""
        request = request or {}
        reason = request.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValidationError("field reason must be a non-empty string when provided")
        with self._store.transaction():
            package = self._load_package(package_id)
            if package.frozen:
                raise StateError(
                    "package is already frozen",
                    details={"package_id": package_id, "frozen_at": dt_to_str(package.frozen_at)},
                )
            checksum = package.current_checksum()
            package.frozen_at = self._clock.now()
            package.frozen_checksum = checksum
            self._store.put(COLLECTION_PACKAGES, package.package_id, package.to_dict())
            self._emit(
                "package_frozen",
                package.package_id,
                {
                    "package_id": package.package_id,
                    "checksum_algorithm": PACKAGE_CHECKSUM_ALGORITHM,
                    "checksum": checksum,
                    "reason": reason.strip() if isinstance(reason, str) else None,
                },
            )
            return self._package_view(package)

    def update_package(self, package_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """冻结课程包的更新接口：只接受“修订说明”事件。

        - ``revision_note``：追加一条修订说明，不触碰原文件内容；
        - 任何替换原文件的事件（``replace`` 等）在冻结后一律拒绝；
        - 未冻结的课程包不走此接口（其内容以登记为准）。
        """
        event_type = request.get("event_type", request.get("type"))
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValidationError(
                "field event_type is required", details={"allowed": [EVENT_REVISION_NOTE]}
            )
        event_type = event_type.strip()
        with self._store.transaction():
            package = self._load_package(package_id)
            if not package.frozen:
                raise StateError(
                    "only a frozen package accepts revision events; the original file is authoritative before freezing",
                    details={"package_id": package_id},
                )
            if event_type in REPLACE_EVENT_TYPES:
                raise PackageFrozenError(
                    "package version is frozen: replacing the original file is not allowed; append a revision note instead",
                    details={"package_id": package_id, "event_type": event_type},
                )
            if event_type != EVENT_REVISION_NOTE:
                raise ValidationError(
                    f"unsupported event type for frozen package: {event_type}",
                    details={"package_id": package_id, "allowed": [EVENT_REVISION_NOTE]},
                )
            note = _require_str(request, "note")
            author = request.get("author")
            if author is not None and (not isinstance(author, str) or not author.strip()):
                raise ValidationError("field author must be a non-empty string when provided")
            entry = {
                "note_id": self._ids.new_id("nt"),
                "event_type": EVENT_REVISION_NOTE,
                "note": note,
                "author": author.strip() if isinstance(author, str) else None,
                "created_at": dt_to_str(self._clock.now()),
            }
            package.revision_notes.append(entry)
            self._store.put(COLLECTION_PACKAGES, package.package_id, package.to_dict())
            self._emit(
                "package_revision_note_added",
                package.package_id,
                {"package_id": package.package_id, "note_id": entry["note_id"]},
            )
            return self._package_view(package)

    def get_package(self, package_id: str) -> dict[str, Any]:
        """读取课程包；冻结包返回冻结时的校验和并复核当前内容完整性。"""
        with self._store.transaction():
            package = self._load_package(package_id)
            return self._package_view(package)

    # -- 导师 --------------------------------------------------------------

    def create_mentor(self, data: dict[str, Any]) -> dict[str, Any]:
        quals_raw = data.get("qualifications", {})
        if not isinstance(quals_raw, dict):
            raise ValidationError("field qualifications must be an object of code -> valid-until ISO datetime")
        qualifications: dict[str, Any] = {}
        for code, until in quals_raw.items():
            try:
                qualifications[str(code)] = dt_from_str(until)
            except ValueError as exc:
                raise ValidationError(f"qualification {code!r} has invalid valid-until: {exc}") from exc
        mentor = Mentor(
            mentor_id=self._ids.new_id("men"),
            name=_require_str(data, "name"),
            home_tz=_require_tz(data, "home_tz"),
            hourly_fee_cents=_require_int(data, "hourly_fee_cents", minimum=0),
            qualifications=qualifications,
        )
        with self._store.transaction():
            self._store.put(COLLECTION_MENTORS, mentor.mentor_id, mentor.to_dict())
        return mentor.to_dict()

    # -- 工坊资源 ----------------------------------------------------------

    def create_resource(self, data: dict[str, Any]) -> dict[str, Any]:
        mutex_group = data.get("mutex_group")
        if mutex_group is not None and (not isinstance(mutex_group, str) or not mutex_group.strip()):
            raise ValidationError("field mutex_group must be null or a non-empty string")
        resource = WorkshopResource(
            resource_id=self._ids.new_id("res"),
            name=_require_str(data, "name"),
            capacity=_require_int(data, "capacity", minimum=1),
            safety_rating=_require_safety(data, "safety_rating"),
            mutex_group=mutex_group.strip() if isinstance(mutex_group, str) else None,
            tz=_require_tz(data, "tz"),
            hourly_fee_cents=_require_int(data, "hourly_fee_cents", minimum=0),
        )
        with self._store.transaction():
            self._store.put(COLLECTION_RESOURCES, resource.resource_id, resource.to_dict())
        return resource.to_dict()

    # -- 材料批次 ----------------------------------------------------------

    def create_material_batch(self, data: dict[str, Any]) -> dict[str, Any]:
        quantity = _require_number(data, "quantity", minimum=0.0)
        batch = MaterialBatch(
            batch_id=self._ids.new_id("bat"),
            material_id=_require_str(data, "material_id"),
            safety=_require_safety(data, "safety"),
            cross_border=bool(data.get("cross_border", False)),
            lead_time_seconds=_require_int(data, "lead_time_seconds", minimum=0),
            unit_cost_cents=_require_int(data, "unit_cost_cents", minimum=0),
            total_quantity=quantity,
            available_quantity=quantity,
        )
        with self._store.transaction():
            self._store.put(COLLECTION_BATCHES, batch.batch_id, batch.to_dict())
        return batch.to_dict()

    # -- 接待窗口 ----------------------------------------------------------

    def create_reception_window(self, data: dict[str, Any]) -> dict[str, Any]:
        start = _require_time(data, "start")
        end = _require_time(data, "end")
        if end <= start:
            raise ValidationError("window end must be after start")
        window = ReceptionWindow(
            window_id=self._ids.new_id("win"),
            institution=_require_str(data, "institution"),
            tz=_require_tz(data, "tz"),
            start=start,
            end=end,
            capacity=_require_int(data, "capacity", minimum=1),
            allowed_safety=_require_safety(data, "allowed_safety"),
        )
        with self._store.transaction():
            self._store.put(COLLECTION_WINDOWS, window.window_id, window.to_dict())
        return window.to_dict()

    # -- 查询 --------------------------------------------------------------

    def _load_package(self, package_id: str) -> CoursePackage:
        record = self._store.get(COLLECTION_PACKAGES, package_id)
        if record is None:
            raise NotFoundError(
                f"package not found: {package_id}", details={"package_id": package_id}
            )
        return CoursePackage.from_dict(record)

    def _emit(self, event_type: str, package_id: str, payload: dict[str, Any]) -> None:
        event = {
            "event_id": self._ids.new_id("evt"),
            "type": event_type,
            "package_id": package_id,
            "payload": payload,
            "created_at": dt_to_str(self._clock.now()),
        }
        self._store.put(COLLECTION_EVENTS, event["event_id"], event)

    def _package_view(self, package: CoursePackage) -> dict[str, Any]:
        """组装读取视图：冻结包附冻结校验和并复核当前内容是否被替换。"""
        view = package.to_dict()
        if package.frozen:
            current = package.current_checksum()
            view["frozen_checksum"] = package.frozen_checksum
            view["checksum_algorithm"] = PACKAGE_CHECKSUM_ALGORITHM
            view["current_checksum"] = current
            view["integrity_ok"] = current == package.frozen_checksum
            if not view["integrity_ok"]:
                raise PackageIntegrityError(
                    "frozen package content does not match the checksum recorded at freeze time; "
                    "the original file appears to have been replaced",
                    details={
                        "package_id": package.package_id,
                        "frozen_checksum": package.frozen_checksum,
                        "current_checksum": current,
                    },
                )
        return view

    def get(self, collection: str, key: str) -> dict[str, Any]:
        record = self._store.get(collection, key)
        if record is None:
            raise NotFoundError(f"{collection} not found: {key}", details={"collection": collection, "key": key})
        if collection == COLLECTION_PACKAGES:
            return self._package_view(CoursePackage.from_dict(record))
        return record

    def list(self, collection: str) -> list[dict[str, Any]]:
        records = self._store.query(collection)
        if collection == COLLECTION_PACKAGES:
            return [self._package_view(CoursePackage.from_dict(r)) for r in records]
        return records
