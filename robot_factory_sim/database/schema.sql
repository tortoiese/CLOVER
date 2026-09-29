-- Robot Factory Simulator — factory_config.db 스키마
-- 스키마 변경 시 db_manager.py 의 SCHEMA_VERSION / _MIGRATIONS 도 함께 수정할 것.

-- 공장 메타 정보
CREATE TABLE IF NOT EXISTS factory_profile (
    id INTEGER PRIMARY KEY,
    factory_name TEXT NOT NULL,
    floor_width REAL DEFAULT 800,
    floor_height REAL DEFAULT 600,
    grid_cell_size REAL DEFAULT 50,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 설비(작업 스테이션) 배치
CREATE TABLE IF NOT EXISTS work_stations (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    width REAL DEFAULT 80,
    height REAL DEFAULT 60,
    task_type TEXT DEFAULT 'assembly',
    avg_process_time REAL DEFAULT 5.0,
    process_time_std REAL DEFAULT 1.0,
    is_active INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 충전 스테이션 배치
CREATE TABLE IF NOT EXISTS charging_stations (
    id TEXT PRIMARY KEY,
    x REAL NOT NULL,
    y REAL NOT NULL,
    capacity INTEGER DEFAULT 2,
    charge_rate REAL DEFAULT 2.0,
    is_active INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 투입 로봇 설정
CREATE TABLE IF NOT EXISTS robots (
    id TEXT PRIMARY KEY,
    robot_type TEXT DEFAULT 'AGV',
    battery_capacity REAL DEFAULT 100,
    move_speed REAL DEFAULT 2.0,
    battery_drain_rate REAL DEFAULT 0.5,
    charge_threshold REAL DEFAULT 20,
    is_active INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 공장 장애물/벽/문 배치
CREATE TABLE IF NOT EXISTS obstacles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obstacle_type TEXT NOT NULL,
    x1 REAL NOT NULL,
    y1 REAL NOT NULL,
    x2 REAL NOT NULL,
    y2 REAL NOT NULL,
    is_passable INTEGER DEFAULT 0,
    label TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 이동 경로 노드
CREATE TABLE IF NOT EXISTS path_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    x REAL NOT NULL,
    y REAL NOT NULL,
    node_type TEXT DEFAULT 'waypoint',
    linked_station_id TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 경로 엣지
CREATE TABLE IF NOT EXISTS path_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_node INTEGER REFERENCES path_nodes(id),
    to_node INTEGER REFERENCES path_nodes(id),
    distance REAL,
    is_bidirectional INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 설정 변경 이력
CREATE TABLE IF NOT EXISTS config_changelog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    record_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    old_values TEXT,
    new_values TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    changed_by TEXT DEFAULT 'system'
);

-- 생산 주문 큐
CREATE TABLE IF NOT EXISTS production_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT UNIQUE NOT NULL,
    product_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    completed INTEGER DEFAULT 0,
    priority TEXT DEFAULT 'normal',
    deadline TIMESTAMP,
    required_stations TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 실시간 생산 이벤트
CREATE TABLE IF NOT EXISTS production_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    order_id TEXT REFERENCES production_orders(order_id),
    payload TEXT NOT NULL,
    is_processed INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 업체별 공장 구성 프리셋 (schema v2)
CREATE TABLE IF NOT EXISTS config_presets (
    name TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_changelog_id ON config_changelog(id);
CREATE INDEX IF NOT EXISTS idx_events_processed ON production_events(is_processed, id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON production_orders(status);
