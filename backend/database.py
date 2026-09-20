"""数据库操作"""
import sqlite3
import hashlib
from config import DB_FILE


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _table_columns(cursor, table):
    """返回表中已存在的列名集合"""
    cursor.execute(f'PRAGMA table_info({table})')
    return {row['name'] for row in cursor.fetchall()}


def init_db():
    """初始化数据库表（含增量迁移，兼容已存在的旧库）"""
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
            note TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            batch_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        )
    ''')

    # 旧库增量迁移：补齐备注、启停状态、批次溯源字段
    share_columns = _table_columns(cursor, 'share_links')
    if 'note' not in share_columns:
        cursor.execute('ALTER TABLE share_links ADD COLUMN note TEXT')
    if 'active' not in share_columns:
        cursor.execute('ALTER TABLE share_links ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
    if 'batch_id' not in share_columns:
        cursor.execute('ALTER TABLE share_links ADD COLUMN batch_id TEXT')

    # 批量分享批次：策略配置 -> 批量提交 -> 分享记录 -> 结果反馈 的溯源主线
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS share_batches (
            batch_id TEXT PRIMARY KEY,
            created_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            total_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            retryable_count INTEGER NOT NULL DEFAULT 0,
            policy_expires_at REAL,
            policy_max_downloads INTEGER,
            policy_note TEXT,
            policy_active INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 批次内逐条结果：以 client_ref 与前端选择项稳定对应，避免刷新/返回后错配
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS share_batch_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            client_ref TEXT NOT NULL,
            file_id TEXT,
            filename TEXT,
            status TEXT NOT NULL,
            retryable INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error_message TEXT,
            note TEXT,
            share_id TEXT,
            expires_at REAL,
            max_downloads INTEGER,
            seq INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(batch_id, client_ref),
            FOREIGN KEY (batch_id) REFERENCES share_batches(batch_id) ON DELETE CASCADE
        )
    ''')

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
