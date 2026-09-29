"""SQLite CRUD + config_changelog 기록.

규칙 (agent.md 3-4):
- 모든 DB 접근은 이 DBManager를 통해서만 수행한다.
- INSERT / UPDATE / DELETE 시 반드시 config_changelog에 변경 이력을 남긴다.
- SQLite 연결은 스레드별로 분리한다 (``:memory:``는 테스트용 단일 공유 연결).
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from utils.config_models import (
    ChargingStationConfig,
    FactoryConfig,
    FactoryProfile,
    ObstacleConfig,
    OrderRecord,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)
from utils.event_types import ChangeType, OrderStatus, Tables

SCHEMA_VERSION: int = 2

# 마이그레이션: 기존 DB에 누락된 컬럼을 ALTER TABLE ADD COLUMN 으로 보충한다.
_MIGRATION_COLUMNS: dict[str, dict[str, str]] = {
    Tables.WORK_STATIONS: {
        "width": "REAL DEFAULT 80",
        "height": "REAL DEFAULT 60",
        "process_time_std": "REAL DEFAULT 1.0",
        "is_active": "INTEGER DEFAULT 1",
    },
    Tables.CHARGING_STATIONS: {"charge_rate": "REAL DEFAULT 2.0", "is_active": "INTEGER DEFAULT 1"},
    Tables.ROBOTS: {"charge_threshold": "REAL DEFAULT 20", "is_active": "INTEGER DEFAULT 1"},
    Tables.OBSTACLES: {"is_passable": "INTEGER DEFAULT 0", "label": "TEXT"},
    Tables.PATH_NODES: {"linked_station_id": "TEXT"},
    Tables.ORDERS: {"required_stations": "TEXT", "completed": "INTEGER DEFAULT 0"},
}


@dataclass(frozen=True)
class _TableSpec:
    """설정 테이블 ↔ dataclass 매핑."""

    table: str
    model: type
    columns: tuple[str, ...]
    bool_columns: frozenset[str]
    autoincrement: bool


_SPECS: dict[str, _TableSpec] = {
    Tables.WORK_STATIONS: _TableSpec(
        Tables.WORK_STATIONS, WorkStationConfig,
        ("label", "x", "y", "width", "height", "task_type", "avg_process_time", "process_time_std", "is_active"),
        frozenset({"is_active"}), False,
    ),
    Tables.CHARGING_STATIONS: _TableSpec(
        Tables.CHARGING_STATIONS, ChargingStationConfig,
        ("x", "y", "capacity", "charge_rate", "is_active"),
        frozenset({"is_active"}), False,
    ),
    Tables.ROBOTS: _TableSpec(
        Tables.ROBOTS, RobotConfig,
        ("robot_type", "battery_capacity", "move_speed", "battery_drain_rate", "charge_threshold", "is_active"),
        frozenset({"is_active"}), False,
    ),
    Tables.OBSTACLES: _TableSpec(
        Tables.OBSTACLES, ObstacleConfig,
        ("obstacle_type", "x1", "y1", "x2", "y2", "is_passable", "label"),
        frozenset({"is_passable"}), True,
    ),
    Tables.PATH_NODES: _TableSpec(
        Tables.PATH_NODES, PathNodeConfig,
        ("x", "y", "node_type", "linked_station_id"),
        frozenset(), True,
    ),
    Tables.PATH_EDGES: _TableSpec(
        Tables.PATH_EDGES, PathEdgeConfig,
        ("from_node", "to_node", "distance", "is_bidirectional"),
        frozenset({"is_bidirectional"}), True,
    ),
}

_ORDER_COLUMNS: tuple[str, ...] = (
    "order_id", "product_type", "quantity", "completed", "priority", "deadline", "required_stations", "status",
    "created_at",
)


@dataclass
class ChangeRecord:
    """config_changelog 한 행."""

    id: int
    table_name: str
    record_id: str
    change_type: str
    old_values: dict[str, Any] | None
    new_values: dict[str, Any] | None
    changed_at: str
    changed_by: str

    def to_dict(self) -> dict[str, Any]:
        """dict로 변환한다.

        Returns:
            변경 이력 dict
        """
        return asdict(self)


def _now_str() -> str:
    """현재 시각을 DB 저장 형식 문자열로 반환한다."""
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def _dt_to_str(value: datetime | None) -> str | None:
    """datetime → DB 문자열."""
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def _str_to_dt(value: str | None) -> datetime | None:
    """DB 문자열 → datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    """dict를 JSON 문자열로 변환한다 (None 유지)."""
    return json.dumps(value, ensure_ascii=False, default=str) if value is not None else None


def _values_equal(a: Any, b: Any) -> bool:
    """DB 값 비교 (실수 허용오차 포함)."""
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), abs_tol=1e-9)
        except (TypeError, ValueError):
            return False
    return a == b


class DBManager:
    """factory_config.db 접근 단일 진입점."""

    def __init__(self, db_path: str | Path = config.DB_PATH, schema_path: str | Path = config.SCHEMA_PATH) -> None:
        """DBManager를 생성한다. 연결은 스레드별로 지연 생성된다.

        Args:
            db_path: SQLite 파일 경로 또는 ``":memory:"``
            schema_path: schema.sql 경로
        """
        self._path = str(db_path)
        self._schema_path = Path(schema_path)
        self._memory = self._path == ":memory:"
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 연결 관리
    # ------------------------------------------------------------------
    @property
    def path(self) -> str:
        """DB 경로."""
        return self._path

    def _conn(self) -> sqlite3.Connection:
        """현재 스레드의 연결을 반환한다."""
        if self._memory:
            if self._shared is None:
                self._shared = sqlite3.connect(":memory:", check_same_thread=False)
                self._shared.row_factory = sqlite3.Row
            return self._shared
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=OFF")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """현재 스레드의 연결을 닫는다."""
        if self._memory:
            return
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """쓰기 트랜잭션 컨텍스트. 예외 시 롤백한다."""
        with self._lock:
            conn = self._conn()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """SELECT 결과를 dict 리스트로 반환한다."""
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """SELECT 첫 행을 dict로 반환한다."""
        with self._lock:
            row = self._conn().execute(sql, params).fetchone()
        return dict(row) if row else None

    def ping(self) -> bool:
        """DB 연결 상태를 확인한다.

        Returns:
            연결 가능 여부
        """
        try:
            self._fetch_one("SELECT 1 AS ok")
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------
    # 스키마 / 마이그레이션
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """schema.sql을 적용하고 마이그레이션을 수행한다."""
        script = self._schema_path.read_text(encoding="utf-8")
        with self._lock:
            conn = self._conn()
            conn.executescript(script)
            self._migrate(conn)
            conn.commit()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """누락 컬럼 보충 + user_version 갱신."""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        for table, columns in _MIGRATION_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def is_seeded(self) -> bool:
        """기본 구성이 이미 들어있는지 확인한다.

        Returns:
            factory_profile 행 존재 여부
        """
        row = self._fetch_one("SELECT COUNT(*) AS n FROM factory_profile")
        return bool(row and row["n"] > 0)

    # ------------------------------------------------------------------
    # 변경 이력
    # ------------------------------------------------------------------
    @staticmethod
    def _log_change(
        conn: sqlite3.Connection,
        table: str,
        record_id: str,
        change_type: str,
        old: dict[str, Any] | None,
        new: dict[str, Any] | None,
        changed_by: str,
    ) -> None:
        """config_changelog에 한 행을 추가한다."""
        conn.execute(
            "INSERT INTO config_changelog(table_name, record_id, change_type, old_values, new_values, changed_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (table, str(record_id), change_type, _json_or_none(old), _json_or_none(new), changed_by),
        )

    def get_last_changelog_id(self) -> int:
        """가장 최근 changelog ID.

        Returns:
            마지막 ID (없으면 0)
        """
        row = self._fetch_one("SELECT COALESCE(MAX(id), 0) AS last_id FROM config_changelog")
        return int(row["last_id"]) if row else 0

    def get_changelog_since(self, since_id: int, limit: int = 200) -> list[ChangeRecord]:
        """since_id 이후의 변경 이력을 반환한다.

        Args:
            since_id: 이 ID보다 큰 행만 조회
            limit: 최대 행 수

        Returns:
            ChangeRecord 리스트 (id 오름차순)
        """
        rows = self._fetch_all(
            "SELECT * FROM config_changelog WHERE id > ? ORDER BY id LIMIT ?", (since_id, limit)
        )
        return [
            ChangeRecord(
                id=r["id"],
                table_name=r["table_name"],
                record_id=r["record_id"],
                change_type=r["change_type"],
                old_values=json.loads(r["old_values"]) if r["old_values"] else None,
                new_values=json.loads(r["new_values"]) if r["new_values"] else None,
                changed_at=str(r["changed_at"]),
                changed_by=r["changed_by"] or "system",
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 공장 구성 (generic)
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_model(spec: _TableSpec, row: dict[str, Any]) -> Any:
        """DB 행을 dataclass로 변환한다."""
        values = {c: row.get(c) for c in spec.columns}
        for column in spec.bool_columns:
            values[column] = bool(values[column]) if values[column] is not None else True
        return spec.model(id=row["id"], **values)

    @staticmethod
    def _model_to_values(spec: _TableSpec, obj: Any) -> dict[str, Any]:
        """dataclass를 DB 컬럼 dict로 변환한다."""
        values = {c: getattr(obj, c) for c in spec.columns}
        for column in spec.bool_columns:
            values[column] = int(bool(values[column]))
        return values

    def list_records(self, table: str) -> list[Any]:
        """설정 테이블 전체를 dataclass 리스트로 조회한다.

        Args:
            table: 테이블 이름 (Tables.* 중 설정 테이블)

        Returns:
            dataclass 리스트
        """
        spec = _SPECS[table]
        rows = self._fetch_all(f"SELECT * FROM {table} ORDER BY id")
        return [self._row_to_model(spec, r) for r in rows]

    def _upsert_in(self, conn: sqlite3.Connection, table: str, obj: Any, changed_by: str) -> tuple[Any, bool]:
        """트랜잭션 내부 upsert. (최종 id, 변경 여부)를 반환한다."""
        spec = _SPECS[table]
        values = self._model_to_values(spec, obj)
        obj_id = obj.id
        is_new_auto = spec.autoincrement and (obj_id is None or int(obj_id) < 0)
        old_row = None if is_new_auto else conn.execute(f"SELECT * FROM {table} WHERE id = ?", (obj_id,)).fetchone()
        if old_row is None:
            columns = list(values)
            if not is_new_auto:
                columns = ["id", *columns]
                values = {"id": obj_id, **values}
            placeholders = ", ".join("?" for _ in columns)
            cur = conn.execute(
                f"INSERT INTO {table}({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values[c] for c in columns),
            )
            new_id = cur.lastrowid if is_new_auto else obj_id
            self._log_change(conn, table, str(new_id), ChangeType.INSERT, None, values, changed_by)
            return new_id, True
        old_values = {c: old_row[c] for c in spec.columns}
        if all(_values_equal(old_values[c], values[c]) for c in spec.columns):
            return obj_id, False
        assignments = ", ".join(f"{c} = ?" for c in spec.columns)
        conn.execute(
            f"UPDATE {table} SET {assignments}, updated_at = ? WHERE id = ?",
            (*(values[c] for c in spec.columns), _now_str(), obj_id),
        )
        self._log_change(conn, table, str(obj_id), ChangeType.UPDATE, old_values, values, changed_by)
        return obj_id, True

    def _delete_in(self, conn: sqlite3.Connection, table: str, record_id: Any, changed_by: str) -> bool:
        """트랜잭션 내부 delete."""
        spec = _SPECS[table]
        old_row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
        if old_row is None:
            return False
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
        old_values = {c: old_row[c] for c in spec.columns}
        self._log_change(conn, table, str(record_id), ChangeType.DELETE, old_values, None, changed_by)
        return True

    def upsert_record(self, table: str, obj: Any, changed_by: str = "system") -> Any:
        """설정 레코드를 INSERT 또는 UPDATE 한다.

        Args:
            table: 설정 테이블 이름
            obj: 해당 테이블의 dataclass
            changed_by: 변경 주체

        Returns:
            최종 레코드 ID
        """
        with self._transaction() as conn:
            record_id, _ = self._upsert_in(conn, table, obj, changed_by)
        return record_id

    def delete_record(self, table: str, record_id: Any, changed_by: str = "system") -> bool:
        """설정 레코드를 삭제한다.

        Args:
            table: 설정 테이블 이름
            record_id: 레코드 ID
            changed_by: 변경 주체

        Returns:
            삭제 여부
        """
        with self._transaction() as conn:
            return self._delete_in(conn, table, record_id, changed_by)

    def get_factory_profile(self) -> FactoryProfile:
        """공장 메타 정보를 조회한다. 없으면 config.py fallback.

        Returns:
            FactoryProfile
        """
        row = self._fetch_one("SELECT * FROM factory_profile ORDER BY id LIMIT 1")
        if row is None:
            return FactoryProfile()
        return FactoryProfile(
            id=row["id"],
            factory_name=row["factory_name"] or config.DEFAULT_FACTORY_NAME,
            floor_width=row["floor_width"] or config.DEFAULT_FLOOR_WIDTH,
            floor_height=row["floor_height"] or config.DEFAULT_FLOOR_HEIGHT,
            grid_cell_size=row["grid_cell_size"] or config.DEFAULT_GRID_CELL_SIZE,
        )

    def _save_profile_in(self, conn: sqlite3.Connection, profile: FactoryProfile, changed_by: str) -> bool:
        """트랜잭션 내부 공장 정보 upsert."""
        columns = ("factory_name", "floor_width", "floor_height", "grid_cell_size")
        values = {c: getattr(profile, c) for c in columns}
        old = conn.execute("SELECT * FROM factory_profile WHERE id = ?", (profile.id,)).fetchone()
        if old is None:
            conn.execute(
                "INSERT INTO factory_profile(id, factory_name, floor_width, floor_height, grid_cell_size) "
                "VALUES (?, ?, ?, ?, ?)",
                (profile.id, *values.values()),
            )
            self._log_change(conn, Tables.FACTORY_PROFILE, str(profile.id), ChangeType.INSERT, None, values,
                             changed_by)
            return True
        old_values = {c: old[c] for c in columns}
        if all(_values_equal(old_values[c], values[c]) for c in columns):
            return False
        conn.execute(
            "UPDATE factory_profile SET factory_name = ?, floor_width = ?, floor_height = ?, grid_cell_size = ?, "
            "updated_at = ? WHERE id = ?",
            (*values.values(), _now_str(), profile.id),
        )
        self._log_change(conn, Tables.FACTORY_PROFILE, str(profile.id), ChangeType.UPDATE, old_values, values,
                         changed_by)
        return True

    def save_factory_profile(self, profile: FactoryProfile, changed_by: str = "system") -> bool:
        """공장 메타 정보를 저장한다.

        Args:
            profile: 공장 정보
            changed_by: 변경 주체

        Returns:
            변경 여부
        """
        with self._transaction() as conn:
            return self._save_profile_in(conn, profile, changed_by)

    def load_factory_config(self) -> FactoryConfig:
        """공장 전체 구성을 조회한다.

        Returns:
            FactoryConfig
        """
        return FactoryConfig(
            profile=self.get_factory_profile(),
            stations=self.list_records(Tables.WORK_STATIONS),
            chargers=self.list_records(Tables.CHARGING_STATIONS),
            robots=self.list_records(Tables.ROBOTS),
            obstacles=self.list_records(Tables.OBSTACLES),
            nodes=self.list_records(Tables.PATH_NODES),
            edges=self.list_records(Tables.PATH_EDGES),
        )

    def save_factory_config(self, factory: FactoryConfig, changed_by: str = "config_editor") -> int:
        """공장 전체 구성을 DB와 diff 하여 저장한다. (단일 트랜잭션)

        음수 ID의 장애물/노드/엣지는 신규로 INSERT 되며, 엣지의 음수 노드 참조는 새 ID로 치환된다.

        Args:
            factory: 저장할 구성
            changed_by: 변경 주체

        Returns:
            변경된 레코드 수
        """
        changes = 0
        with self._transaction() as conn:
            changes += int(self._save_profile_in(conn, factory.profile, changed_by))
            for table, items in (
                (Tables.WORK_STATIONS, factory.stations),
                (Tables.CHARGING_STATIONS, factory.chargers),
                (Tables.ROBOTS, factory.robots),
                (Tables.OBSTACLES, factory.obstacles),
            ):
                changes += self._sync_table_in(conn, table, items, changed_by)[0]

            node_changes, node_map = self._sync_table_in(conn, Tables.PATH_NODES, factory.nodes, changed_by)
            changes += node_changes
            positions = {int(n.id if n.id >= 0 else node_map[n.id]): (n.x, n.y) for n in factory.nodes}
            edges: list[PathEdgeConfig] = []
            for edge in factory.edges:
                a = node_map.get(edge.from_node, edge.from_node)
                b = node_map.get(edge.to_node, edge.to_node)
                if a not in positions or b not in positions or a == b:
                    continue
                (ax, ay), (bx, by) = positions[a], positions[b]
                distance = math.hypot(ax - bx, ay - by)
                edges.append(PathEdgeConfig(edge.id, a, b, round(distance, 3), edge.is_bidirectional))
            changes += self._sync_table_in(conn, Tables.PATH_EDGES, edges, changed_by)[0]
        return changes

    def _sync_table_in(
        self, conn: sqlite3.Connection, table: str, items: list[Any], changed_by: str
    ) -> tuple[int, dict[Any, Any]]:
        """테이블을 items와 일치시킨다. (변경 수, 임시ID→신규ID 매핑)."""
        existing_ids = {row[0] for row in conn.execute(f"SELECT id FROM {table}")}
        kept: set[Any] = set()
        id_map: dict[Any, Any] = {}
        changes = 0
        for obj in items:
            new_id, changed = self._upsert_in(conn, table, obj, changed_by)
            if new_id != obj.id:
                id_map[obj.id] = new_id
            kept.add(new_id)
            changes += int(changed)
        for stale_id in existing_ids - kept:
            changes += int(self._delete_in(conn, table, stale_id, changed_by))
        return changes, id_map

    def replace_factory_config(self, factory: FactoryConfig, changed_by: str = "preset") -> int:
        """구성을 통째로 교체한다 (프리셋 불러오기용). 자동증가 ID는 새로 부여된다.

        Args:
            factory: 새 구성
            changed_by: 변경 주체

        Returns:
            변경된 레코드 수
        """
        remapped = FactoryConfig.from_dict(factory.to_dict())
        id_map: dict[int, int] = {}
        for index, node in enumerate(remapped.nodes, start=1):
            id_map[node.id] = -index
            node.id = -index
        for index, edge in enumerate(remapped.edges, start=1):
            edge.id = -index
            edge.from_node = id_map.get(edge.from_node, edge.from_node)
            edge.to_node = id_map.get(edge.to_node, edge.to_node)
        for index, obstacle in enumerate(remapped.obstacles, start=1):
            obstacle.id = -index
        return self.save_factory_config(remapped, changed_by)

    # ------------------------------------------------------------------
    # 생산 주문
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_order(row: dict[str, Any]) -> OrderRecord:
        """DB 행 → OrderRecord."""
        stations: list[str] = []
        if row.get("required_stations"):
            try:
                stations = list(json.loads(row["required_stations"]))
            except (json.JSONDecodeError, TypeError):
                stations = [s.strip() for s in str(row["required_stations"]).split(",") if s.strip()]
        return OrderRecord(
            order_id=row["order_id"],
            product_type=row["product_type"],
            quantity=int(row["quantity"]),
            completed=int(row["completed"] or 0),
            priority=row["priority"] or "normal",
            deadline=_str_to_dt(row["deadline"]),
            required_stations=stations,
            status=row["status"] or OrderStatus.PENDING,
            created_at=_str_to_dt(row["created_at"]),
        )

    @staticmethod
    def _order_to_values(order: OrderRecord) -> dict[str, Any]:
        """OrderRecord → DB 컬럼 dict."""
        return {
            "order_id": order.order_id,
            "product_type": order.product_type,
            "quantity": order.quantity,
            "completed": order.completed,
            "priority": order.priority,
            "deadline": _dt_to_str(order.deadline),
            "required_stations": json.dumps(order.required_stations, ensure_ascii=False),
            "status": order.status,
            "created_at": _dt_to_str(order.created_at) or _now_str(),
        }

    def next_order_id(self) -> str:
        """다음 주문 ID를 생성한다. (ORD-YYYY-NNNN)

        Returns:
            새 주문 ID
        """
        row = self._fetch_one("SELECT COALESCE(MAX(id), 0) AS n FROM production_orders")
        seq = (int(row["n"]) if row else 0) + 1
        return f"ORD-{datetime.now().year}-{seq:04d}"

    def insert_order(self, order: OrderRecord, changed_by: str = "system") -> None:
        """생산 주문을 추가한다.

        Args:
            order: 주문 레코드
            changed_by: 변경 주체
        """
        values = self._order_to_values(order)
        with self._transaction() as conn:
            conn.execute(
                f"INSERT INTO production_orders({', '.join(_ORDER_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _ORDER_COLUMNS)})",
                tuple(values[c] for c in _ORDER_COLUMNS),
            )
            self._log_change(conn, Tables.ORDERS, order.order_id, ChangeType.INSERT, None, values, changed_by)

    def list_orders(self, active_only: bool = False) -> list[OrderRecord]:
        """주문 목록을 조회한다.

        Args:
            active_only: True면 pending / in_progress만

        Returns:
            OrderRecord 리스트
        """
        if active_only:
            rows = self._fetch_all(
                "SELECT * FROM production_orders WHERE status IN (?, ?) ORDER BY id",
                (OrderStatus.PENDING, OrderStatus.IN_PROGRESS),
            )
        else:
            rows = self._fetch_all("SELECT * FROM production_orders ORDER BY id")
        return [self._row_to_order(r) for r in rows]

    def get_order(self, order_id: str) -> OrderRecord | None:
        """주문 1건을 조회한다.

        Args:
            order_id: 주문 ID

        Returns:
            OrderRecord 또는 None
        """
        row = self._fetch_one("SELECT * FROM production_orders WHERE order_id = ?", (order_id,))
        return self._row_to_order(row) if row else None

    def update_order(self, order_id: str, fields: dict[str, Any], changed_by: str = "system") -> bool:
        """주문 필드를 수정한다.

        Args:
            order_id: 주문 ID
            fields: 변경할 컬럼 → 값 (deadline은 datetime, required_stations는 list 허용)
            changed_by: 변경 주체

        Returns:
            변경 여부
        """
        allowed = {"product_type", "quantity", "completed", "priority", "deadline", "required_stations", "status"}
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                raise KeyError(f"수정할 수 없는 주문 필드: {key}")
            if key == "deadline" and isinstance(value, datetime):
                value = _dt_to_str(value)
            elif key == "required_stations" and isinstance(value, list):
                value = json.dumps(value, ensure_ascii=False)
            values[key] = value
        if not values:
            return False
        with self._transaction() as conn:
            old = conn.execute("SELECT * FROM production_orders WHERE order_id = ?", (order_id,)).fetchone()
            if old is None:
                return False
            old_values = {k: old[k] for k in values}
            if all(_values_equal(old_values[k], values[k]) for k in values):
                return False
            assignments = ", ".join(f"{k} = ?" for k in values)
            conn.execute(
                f"UPDATE production_orders SET {assignments}, updated_at = ? WHERE order_id = ?",
                (*values.values(), _now_str(), order_id),
            )
            self._log_change(conn, Tables.ORDERS, order_id, ChangeType.UPDATE, old_values, values, changed_by)
        return True

    # ------------------------------------------------------------------
    # 생산 이벤트
    # ------------------------------------------------------------------
    def insert_event(
        self, event_type: str, order_id: str | None, payload: dict[str, Any], changed_by: str = "system"
    ) -> int:
        """생산 이벤트를 추가한다.

        Args:
            event_type: ProductionEventType.*
            order_id: 관련 주문 ID (없으면 None)
            payload: 이벤트 상세
            changed_by: 변경 주체

        Returns:
            이벤트 ID
        """
        text = json.dumps(payload, ensure_ascii=False, default=str)
        with self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO production_events(event_type, order_id, payload) VALUES (?, ?, ?)",
                (event_type, order_id, text),
            )
            event_id = int(cur.lastrowid or 0)
            self._log_change(
                conn, Tables.EVENTS, str(event_id), ChangeType.INSERT, None,
                {"event_type": event_type, "order_id": order_id, "payload": payload}, changed_by,
            )
        return event_id

    def fetch_unprocessed_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """미처리 이벤트를 조회한다.

        Args:
            limit: 최대 행 수

        Returns:
            이벤트 dict 리스트 (payload는 dict로 파싱됨)
        """
        rows = self._fetch_all(
            "SELECT * FROM production_events WHERE is_processed = 0 ORDER BY id LIMIT ?", (limit,)
        )
        for row in rows:
            try:
                row["payload"] = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                row["payload"] = {"raw": row["payload"]}
            row["created_at"] = str(row["created_at"])
        return rows

    def mark_events_processed(self, event_ids: list[int], changed_by: str = "event_watcher") -> None:
        """이벤트를 처리 완료로 표시한다.

        Args:
            event_ids: 이벤트 ID 목록
            changed_by: 변경 주체
        """
        if not event_ids:
            return
        with self._transaction() as conn:
            for event_id in event_ids:
                conn.execute("UPDATE production_events SET is_processed = 1 WHERE id = ?", (event_id,))
                self._log_change(conn, Tables.EVENTS, str(event_id), ChangeType.UPDATE,
                                 {"is_processed": 0}, {"is_processed": 1}, changed_by)

    def has_event(self, event_type: str, order_id: str) -> bool:
        """해당 주문에 대한 이벤트가 이미 있는지 확인한다.

        Args:
            event_type: 이벤트 종류
            order_id: 주문 ID

        Returns:
            존재 여부
        """
        row = self._fetch_one(
            "SELECT 1 AS ok FROM production_events WHERE event_type = ? AND order_id = ? LIMIT 1",
            (event_type, order_id),
        )
        return row is not None

    # ------------------------------------------------------------------
    # 프리셋
    # ------------------------------------------------------------------
    def list_presets(self) -> list[str]:
        """저장된 프리셋 이름 목록.

        Returns:
            이름 리스트
        """
        return [r["name"] for r in self._fetch_all("SELECT name FROM config_presets ORDER BY name")]

    def save_preset(self, name: str, factory: FactoryConfig, changed_by: str = "config_editor") -> None:
        """현재 구성을 프리셋으로 저장한다 (같은 이름이면 덮어씀).

        Args:
            name: 프리셋 이름
            factory: 저장할 구성
            changed_by: 변경 주체
        """
        payload = factory.to_json()
        with self._transaction() as conn:
            exists = conn.execute("SELECT 1 FROM config_presets WHERE name = ?", (name,)).fetchone()
            if exists:
                conn.execute(
                    "UPDATE config_presets SET payload = ?, updated_at = ? WHERE name = ?",
                    (payload, _now_str(), name),
                )
                self._log_change(conn, Tables.PRESETS, name, ChangeType.UPDATE, None, {"name": name}, changed_by)
            else:
                conn.execute("INSERT INTO config_presets(name, payload) VALUES (?, ?)", (name, payload))
                self._log_change(conn, Tables.PRESETS, name, ChangeType.INSERT, None, {"name": name}, changed_by)

    def load_preset(self, name: str) -> FactoryConfig | None:
        """프리셋을 조회한다.

        Args:
            name: 프리셋 이름

        Returns:
            FactoryConfig 또는 None
        """
        row = self._fetch_one("SELECT payload FROM config_presets WHERE name = ?", (name,))
        return FactoryConfig.from_json(row["payload"]) if row else None

    def delete_preset(self, name: str, changed_by: str = "config_editor") -> bool:
        """프리셋을 삭제한다.

        Args:
            name: 프리셋 이름
            changed_by: 변경 주체

        Returns:
            삭제 여부
        """
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM config_presets WHERE name = ?", (name,))
            if cur.rowcount:
                self._log_change(conn, Tables.PRESETS, name, ChangeType.DELETE, {"name": name}, None, changed_by)
            return bool(cur.rowcount)


if __name__ == "__main__":
    db = DBManager(":memory:")
    db.initialize()
    db.upsert_record(Tables.WORK_STATIONS, WorkStationConfig("WS-01", "조립", 100, 100))
    db.upsert_record(Tables.WORK_STATIONS, WorkStationConfig("WS-01", "조립", 150, 100))
    print(db.list_records(Tables.WORK_STATIONS))
    for change in db.get_changelog_since(0):
        print(change.change_type, change.table_name, change.record_id, change.new_values)
