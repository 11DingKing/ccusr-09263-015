"""目录服务：课程包、导师、工坊资源、材料批次、接待窗口的登记。"""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.errors import NotFoundError, PackageFrozenError, StateError, ValidationError
from ..domain.models import (
    CoursePackage,
    MaterialBatch,
    MaterialRequirement,
    MaterialSafety,
    Mentor,
    PACKAGE_CHECKSUM_FIELDS,
    ReceptionWindow,
    RevisionNote,
    WorkshopResource,
    dt_from_str,
    package_content_checksum,
)
from ..persistence.store import Store
from .ports import Clock, IdGenerator

COLLECTION_PACKAGES = "packages"
COLLECTION_MENTORS = "mentors"
COLLECTION_RESOURCES = "resources"
COLLECTION_BATCHES = "material_batches"
COLLECTION_WINDOWS = "reception_windows"
#: 课程包冻结记录（冻结校验和的独立凭证，随主库一起持久化到 SQLite）
COLLECTION_PACKAGE_FREEZES = "package_freezes"

#: 更新接口允许修改的课程包内容字段（对应“原文件/包内材料”）
_PACKAGE_CONTENT_FIELDS = frozenset(PACKAGE_CHECKSUM_FIELDS)
CHECKSUM_ALGORITHM = "sha256"


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

    def _load_package(self, package_id: str) -> CoursePackage:
        record = self._store.get(COLLECTION_PACKAGES, package_id)
        if record is None:
            raise NotFoundError(
                f"package not found: {package_id}",
                details={"collection": COLLECTION_PACKAGES, "key": package_id},
            )
        return CoursePackage.from_dict(record)

    def _parse_materials(self, materials_raw: Any) -> list[MaterialRequirement]:
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
        return materials

    def _parse_revision_note(self, data: dict[str, Any]) -> RevisionNote | None:
        """从载荷解析修订说明事件；载荷中没有说明时返回 None。

        接受 ``"revision_note"``（对象或纯字符串），兼容简写 ``"note"``。
        """
        raw = data.get("revision_note", data.get("note"))
        if raw is None:
            return None
        if isinstance(raw, str):
            summary, detail = raw.strip(), ""
            author = None
        elif isinstance(raw, dict):
            summary = _require_str(raw, "summary")
            detail = raw.get("detail", "")
            if not isinstance(detail, str):
                raise ValidationError("revision_note.detail must be a string")
            author = raw.get("author")
            if author is not None and (not isinstance(author, str) or not author.strip()):
                raise ValidationError("revision_note.author must be a non-empty string or null")
            author = author.strip() if isinstance(author, str) else None
        else:
            raise ValidationError("revision_note must be an object or a string")
        if not summary:
            raise ValidationError("revision_note.summary must be a non-empty string")
        return RevisionNote(
            note_id=self._ids.new_id("rev"),
            summary=summary,
            detail=detail.strip(),
            author=author,
            created_at=self._clock.now(),
        )

    def freeze_package(self, package_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """冻结课程包版本：固化校验和并写入持久化冻结记录。

        冻结后包内材料/内容不可替换，仅可经 :meth:`update_package` 追加修订说明。
        载荷可携带首条修订说明（``revision_note``）。
        """
        data = data or {}
        with self._store.transaction():
            package = self._load_package(package_id)
            if package.frozen:
                raise StateError(
                    f"package already frozen at {package.frozen_at}",
                    details={"package_id": package_id, "frozen_checksum": package.frozen_checksum},
                )
            note = self._parse_revision_note(data)
            frozen_at = self._clock.now()
            checksum = package_content_checksum(package.to_dict())
            package.frozen_checksum = checksum
            package.frozen_at = frozen_at
            if note is not None:
                package.revision_notes.append(note)
            content_snapshot = {f: package.to_dict()[f] for f in PACKAGE_CHECKSUM_FIELDS}
            freeze_record = {
                "package_id": package_id,
                "algorithm": CHECKSUM_ALGORITHM,
                "checksum": checksum,
                "frozen_at": package.to_dict()["frozen_at"],
                "revision_count_at_freeze": len(package.revision_notes),
                "content_snapshot": content_snapshot,
            }
            self._store.put(COLLECTION_PACKAGES, package_id, package.to_dict())
            self._store.put(COLLECTION_PACKAGE_FREEZES, package_id, freeze_record)
        return self.get_package(package_id)

    def add_revision_note(self, package_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """追加一条修订说明（冻结前后均可，永远只追加，不触碰原内容）。"""
        note = self._parse_revision_note(data)
        if note is None:
            raise ValidationError(
                "revision_note is required (object with summary, or a string)",
                details={"field": "revision_note"},
            )
        with self._store.transaction():
            package = self._load_package(package_id)
            package.revision_notes.append(note)
            self._store.put(COLLECTION_PACKAGES, package_id, package.to_dict())
        return self.get_package(package_id)

    def update_package(self, package_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """更新课程包。

        - 未冻结：可修改内容字段（材料、名称等），也可附带修订说明；
        - 已冻结：只接受修订说明事件，任何内容字段（含替换 materials 原文件）
          都抛 :class:`PackageFrozenError`。
        """
        if not isinstance(data, dict):
            raise ValidationError("request body must be an object")
        note = self._parse_revision_note(data)
        requested_content = sorted(f for f in _PACKAGE_CONTENT_FIELDS if f in data)
        allowed_note_keys = {"revision_note", "note"}
        extraneous = sorted(k for k in data if k not in allowed_note_keys)
        with self._store.transaction():
            package = self._load_package(package_id)
            if package.frozen:
                # 冻结后更新接口只接受说明事件：内容字段与任何未知字段一律拒绝
                if extraneous:
                    raise PackageFrozenError(
                        "package version is frozen: original materials cannot be replaced; "
                        "only revision_note events are accepted",
                        details={
                            "package_id": package_id,
                            "rejected_fields": extraneous,
                            "frozen_checksum": package.frozen_checksum,
                        },
                    )
                if note is None:
                    raise ValidationError(
                        "frozen package accepts revision_note events only",
                        details={"package_id": package_id},
                    )
                package.revision_notes.append(note)
                self._store.put(COLLECTION_PACKAGES, package_id, package.to_dict())
                return self.get_package(package_id)

            if not requested_content and note is None:
                raise ValidationError("nothing to update: provide content fields and/or revision_note")
            if "name" in data:
                package.name = _require_str(data, "name")
            if "craft" in data:
                package.craft = _require_str(data, "craft")
            if "duration_minutes" in data:
                package.duration_minutes = _require_int(data, "duration_minutes", minimum=1)
            if "max_seats" in data:
                package.max_seats = _require_int(data, "max_seats", minimum=1)
            if "required_qualifications" in data:
                quals_raw = data["required_qualifications"]
                if not isinstance(quals_raw, list) or not all(isinstance(q, str) for q in quals_raw):
                    raise ValidationError("field required_qualifications must be a list of strings")
                package.required_qualifications = list(quals_raw)
            if "materials" in data:
                package.materials = self._parse_materials(data["materials"])
            if note is not None:
                package.revision_notes.append(note)
            self._store.put(COLLECTION_PACKAGES, package_id, package.to_dict())
        return self.get_package(package_id)

    # -- 课程包查询 --------------------------------------------------------

    def _package_view(self, record: dict[str, Any]) -> dict[str, Any]:
        """组装课程包读取视图：展示冻结时校验和与当前完整性比对结果。"""
        package = CoursePackage.from_dict(record)
        view = dict(record)
        view["frozen"] = package.frozen
        view["checksum_algorithm"] = CHECKSUM_ALGORITHM if package.frozen else None
        current = package_content_checksum(record)
        view["current_checksum"] = current
        if package.frozen:
            view["frozen_checksum"] = package.frozen_checksum
            view["content_intact"] = current == package.frozen_checksum
        else:
            view["content_intact"] = None
        return view

    def get_package(self, package_id: str) -> dict[str, Any]:
        with self._store.transaction():
            record = self._store.get(COLLECTION_PACKAGES, package_id)
            if record is None:
                raise NotFoundError(
                    f"package not found: {package_id}",
                    details={"collection": COLLECTION_PACKAGES, "key": package_id},
                )
            return self._package_view(record)

    def list_packages(self) -> list[dict[str, Any]]:
        return [self._package_view(r) for r in self._store.query(COLLECTION_PACKAGES)]

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

    def get(self, collection: str, key: str) -> dict[str, Any]:
        record = self._store.get(collection, key)
        if record is None:
            raise NotFoundError(f"{collection} not found: {key}", details={"collection": collection, "key": key})
        return record

    def list(self, collection: str) -> list[dict[str, Any]]:
        return self._store.query(collection)
