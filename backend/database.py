"""数据库操作"""
import sqlite3
import hashlib
from config import DB_FILE


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(cursor, table, column, definition):
    """旧版本数据库增量添加字段"""
    cursor.execute(f'PRAGMA table_info({table})')
    columns = {row['name'] for row in cursor.fetchall()}
    if column not in columns:
        cursor.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def init_db():
    """初始化数据库表"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS share_links (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            created_by TEXT NOT NULL,
            expires_at REAL,
            max_downloads INTEGER,
            download_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            note TEXT,
            is_enabled INTEGER NOT NULL DEFAULT 1,
            batch_id TEXT,
            batch_item_id TEXT,
            request_id TEXT,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        )
    ''')
    ensure_column(cursor, 'share_links', 'note', 'TEXT')
    ensure_column(cursor, 'share_links', 'is_enabled', 'INTEGER NOT NULL DEFAULT 1')
    ensure_column(cursor, 'share_links', 'batch_id', 'TEXT')
    ensure_column(cursor, 'share_links', 'batch_item_id', 'TEXT')
    ensure_column(cursor, 'share_links', 'request_id', 'TEXT')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS share_batch_jobs (
            id TEXT PRIMARY KEY,
            created_by TEXT NOT NULL,
            expire_hours INTEGER,
            expires_at REAL,
            max_downloads INTEGER,
            note TEXT,
            is_enabled INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS share_batch_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            file_id TEXT,
            filename TEXT,
            status TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            retryable INTEGER NOT NULL DEFAULT 0,
            share_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(batch_id, item_id),
            FOREIGN KEY (batch_id) REFERENCES share_batch_jobs(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS share_batch_requests (
            request_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            response TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (batch_id) REFERENCES share_batch_jobs(id)
        )
    ''')

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_share_links_created_by ON share_links(created_by)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_share_links_file_enabled ON share_links(file_id, created_by, is_enabled)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_batch_jobs_user ON share_batch_jobs(created_by, created_at DESC)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON share_batch_items(batch_id, id)'
    )

    default_users = [
        ('admin', hashlib.sha256('admin123'.encode()).hexdigest()),
        ('user', hashlib.sha256('user123'.encode()).hexdigest()),
        ('test', hashlib.sha256('test123'.encode()).hexdigest())
    ]
    for username, password_hash in default_users:
        cursor.execute(
            'INSERT OR IGNORE INTO users (username, password_hash) VALUES (?, ?)',
            (username, password_hash)
        )

    conn.commit()
    conn.close()
