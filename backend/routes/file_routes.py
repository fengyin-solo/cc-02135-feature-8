"""文件路由"""
import os
import re
import uuid
import time
import json
import logging
import sqlite3
from flask import request, jsonify, send_file
from werkzeug.utils import secure_filename
from routes import files_bp
from database import get_db
from auth import verify_token, get_username_from_token, login_required
from config import UPLOAD_FOLDER, MAX_FILE_SIZE, BLOCKED_EXTENSIONS, SHARE_LINK_EXPIRE_HOURS, SHARE_LINK_MAX_DOWNLOADS

logger = logging.getLogger(__name__)


def allowed_file(filename):
    """检查文件扩展名是否被禁止"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext not in BLOCKED_EXTENSIONS


@files_bp.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '未选择文件'}), 400

    if not allowed_file(file.filename):
        return jsonify({'error': '不支持的文件类型'}), 400

    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    if file_size > MAX_FILE_SIZE:
        return jsonify({'error': f'文件大小超过限制（最大{MAX_FILE_SIZE // 1024 // 1024}MB）'}), 400

    file_id = str(uuid.uuid4())
    # 保留原始文件名用于显示（去掉路径分隔符防止注入）
    original_name = re.sub(r'[/\\]', '_', file.filename).strip()
    if not original_name:
        original_name = file_id

    # 磁盘上用 UUID + 扩展名存储，避免文件名编码问题
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    safe_filename = f"{file_id}.{ext}" if ext else file_id
    filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
    file.save(filepath)

    file_size = os.path.getsize(filepath)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO files (id, name, path, size) VALUES (?, ?, ?, ?)',
        (file_id, original_name, filepath, file_size)
    )
    conn.commit()
    conn.close()

    logger.info(f"文件上传成功: {original_name} (ID: {file_id}, 大小: {file_size} bytes)")
    return jsonify({'success': True, 'file_id': file_id, 'filename': original_name})


@files_bp.route('/api/files', methods=['GET'])
def list_files():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, path, size FROM files')
    files = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(files)


@files_bp.route('/api/download/<file_id>', methods=['GET'])
def download_file(file_id):
    # 优先从 Authorization 头获取 token，兼容查询参数（已废弃）
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
    else:
        token = request.args.get('token')  # 向后兼容，建议前端迁移到 Authorization 头

    if not token or not verify_token(token):
        return jsonify({'error': '未授权或token已过期'}), 401

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT name, path FROM files WHERE id = ?', (file_id,))
    file_info = cursor.fetchone()
    conn.close()

    if not file_info:
        return jsonify({'error': '文件不存在'}), 404

    if not os.path.abspath(file_info['path']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        return jsonify({'error': '非法文件路径'}), 403

    if not os.path.exists(file_info['path']):
        return jsonify({'error': '文件不存在'}), 404

    logger.info(f"文件下载: {file_info['name']} (ID: {file_id})")
    return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])


def generate_short_id():
    """生成短的分享链接ID"""
    return uuid.uuid4().hex[:12]


def get_share_link_info(share_id):
    """获取分享链接信息，包含文件信息和有效性检查"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.file_id, s.created_by, s.expires_at, s.max_downloads, s.download_count, s.created_at,
               s.note, s.is_enabled, s.batch_id, s.batch_item_id, s.request_id,
               f.name as filename, f.size as filesize, f.path
        FROM share_links s
        JOIN files f ON s.file_id = f.id
        WHERE s.id = ?
    ''', (share_id,))
    share = cursor.fetchone()
    conn.close()
    return share


def is_share_valid(share):
    """检查分享链接是否有效"""
    if not share:
        return False, '分享链接不存在'

    if not share['is_enabled']:
        return False, '分享链接已停用'

    if share['expires_at'] is not None and share['expires_at'] < time.time():
        return False, '分享链接已过期'

    if share['max_downloads'] is not None and share['download_count'] >= share['max_downloads']:
        return False, '分享链接下载次数已用完'

    return True, None


def increment_download_count(share_id):
    """增加下载次数"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE share_links SET download_count = download_count + 1 WHERE id = ?',
        (share_id,)
    )
    conn.commit()
    conn.close()


def get_token_from_request():
    """从请求中获取 token"""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return request.args.get('token')


def policy_value_to_unlimited(value):
    """兼容旧接口：-1 表示不限制"""
    return None if value is not None and value < 0 else value


def normalize_single_policy(data):
    """单条分享沿用原有默认值和 -1 规则"""
    expire_hours = data.get('expire_hours')
    max_downloads = data.get('max_downloads')

    if expire_hours is None:
        expire_hours = SHARE_LINK_EXPIRE_HOURS
    elif not is_int_like(expire_hours):
        return None
    expire_hours = policy_value_to_unlimited(expire_hours)
    expires_at = time.time() + expire_hours * 3600 if expire_hours is not None else None

    if max_downloads is None:
        max_downloads = SHARE_LINK_MAX_DOWNLOADS
    elif not is_int_like(max_downloads):
        return None
    max_downloads = policy_value_to_unlimited(max_downloads)

    return {
        'expire_hours': expire_hours,
        'expires_at': expires_at,
        'max_downloads': max_downloads,
        'note': '',
        'is_enabled': True
    }


def is_int_like(value):
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_batch_policy(data):
    """校验批量策略；返回数据库值和用于追踪的原始策略值"""
    errors = []

    expire_hours = data.get('expire_hours', SHARE_LINK_EXPIRE_HOURS)
    if not is_int_like(expire_hours) or expire_hours == 0 or expire_hours < -1:
        errors.append('有效期必须是正整数，或使用 -1 表示永久有效')

    max_downloads = data.get('max_downloads', SHARE_LINK_MAX_DOWNLOADS)
    if not is_int_like(max_downloads) or max_downloads == 0 or max_downloads < -1:
        errors.append('取件次数上限必须是正整数，或使用 -1 表示无限制')

    note = data.get('note', '')
    if note is None:
        note = ''
    if not isinstance(note, str):
        errors.append('备注必须是文本')
        note = ''
    else:
        note = note.strip()
        if len(note) > 500:
            errors.append('备注不能超过 500 个字符')

    is_enabled = data.get('is_enabled', True)
    if not isinstance(is_enabled, bool):
        errors.append('启停状态必须是布尔值')

    if errors:
        return None, '；'.join(errors)

    db_expire_hours = None if expire_hours == -1 else expire_hours
    expires_at = time.time() + db_expire_hours * 3600 if db_expire_hours is not None else None
    db_max_downloads = None if max_downloads == -1 else max_downloads

    return {
        'expire_hours': db_expire_hours,
        'expires_at': expires_at,
        'max_downloads': db_max_downloads,
        'note': note,
        'is_enabled': is_enabled
    }, None


def policy_snapshot(policy):
    """用于接口响应和前端回填的策略快照"""
    return {
        'expire_hours': -1 if policy['expire_hours'] is None else policy['expire_hours'],
        'expires_at': policy['expires_at'],
        'max_downloads': -1 if policy['max_downloads'] is None else policy['max_downloads'],
        'note': policy.get('note', ''),
        'is_enabled': bool(policy.get('is_enabled', True))
    }


def insert_share_link(cursor, share_id, file_id, username, policy, batch_id=None,
                      batch_item_id=None, request_id=None):
    cursor.execute('''
        INSERT INTO share_links
            (id, file_id, created_by, expires_at, max_downloads, note, is_enabled,
             batch_id, batch_item_id, request_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        share_id, file_id, username, policy['expires_at'], policy['max_downloads'],
        policy.get('note', ''), 1 if policy.get('is_enabled', True) else 0,
        batch_id, batch_item_id, request_id
    ))


def error_result(item_id, file_id, filename, code, message, retryable):
    return {
        'item_id': item_id,
        'file_id': file_id,
        'filename': filename,
        'success': False,
        'status': 'failed',
        'error_code': code,
        'error_message': message,
        'retryable': retryable,
        'share_id': None
    }


def has_active_share(cursor, file_id, username, batch_id=None):
    cursor.execute('''
        SELECT id, expires_at, max_downloads, download_count, is_enabled, batch_id
        FROM share_links
        WHERE file_id = ? AND created_by = ?
    ''', (file_id, username))
    now = time.time()
    for share in cursor.fetchall():
        if batch_id and share['batch_id'] == batch_id:
            return share['id']
        if not share['is_enabled']:
            continue
        expired = share['expires_at'] is not None and share['expires_at'] < now
        exhausted = (
            share['max_downloads'] is not None
            and share['download_count'] >= share['max_downloads']
        )
        if not expired and not exhausted:
            return share['id']
    return None


def process_batch_item(cursor, item, username, policy):
    """处理单个批量项，失败时不影响其他项"""
    raw_file = item['file_id']
    item_id = item['item_id']

    if not isinstance(raw_file, str):
        return error_result(item_id, None, None, 'INVALID_ITEM', '文件ID必须是文本', False)

    file_id = raw_file.strip()
    if not file_id:
        return error_result(item_id, raw_file, None, 'INVALID_ITEM', '文件ID不能为空', False)

    cursor.execute('SELECT id, name, path FROM files WHERE id = ?', (file_id,))
    file_info = cursor.fetchone()
    if not file_info:
        return error_result(item_id, file_id, None, 'FILE_NOT_FOUND', '文件不存在或已被删除', True)

    if not os.path.exists(file_info['path']):
        return error_result(item_id, file_id, file_info['name'], 'FILE_UNAVAILABLE', '文件记录存在但存储文件已失效', True)

    active_share_id = has_active_share(cursor, file_id, username, item['batch_id'])
    if active_share_id:
        return error_result(
            item_id, file_id, file_info['name'], 'SHARE_CONFLICT',
            '该文件已有启用中的分享链接，请停用或删除后重试', True
        )

    share_id = generate_short_id()
    insert_share_link(
        cursor, share_id, file_id, username, policy,
        batch_id=item['batch_id'], batch_item_id=item_id, request_id=item['request_id']
    )
    return {
        'item_id': item_id,
        'file_id': file_id,
        'filename': file_info['name'],
        'success': True,
        'status': 'succeeded',
        'error_code': None,
        'error_message': None,
        'retryable': False,
        'share_id': share_id
    }


def serialize_batch_job(cursor, batch_id):
    cursor.execute('SELECT * FROM share_batch_jobs WHERE id = ?', (batch_id,))
    job = cursor.fetchone()
    if not job:
        return None

    cursor.execute('''
        SELECT item_id, file_id, filename, status, error_code, error_message, retryable, share_id
        FROM share_batch_items
        WHERE batch_id = ?
        ORDER BY id
    ''', (batch_id,))
    rows = cursor.fetchall()
    results = []
    for row in rows:
        results.append({
            'item_id': row['item_id'],
            'file_id': row['file_id'],
            'filename': row['filename'],
            'success': row['status'] == 'succeeded',
            'status': row['status'],
            'error_code': row['error_code'],
            'error_message': row['error_message'],
            'retryable': bool(row['retryable']),
            'share_id': row['share_id']
        })

    succeeded = sum(1 for item in results if item['success'])
    failed = len(results) - succeeded
    policy = {
        'expire_hours': job['expire_hours'],
        'expires_at': job['expires_at'],
        'max_downloads': job['max_downloads'],
        'note': job['note'] or '',
        'is_enabled': bool(job['is_enabled'])
    }
    return {
        'success': failed == 0,
        'batch_id': job['id'],
        'status': job['status'],
        'policy': policy_snapshot(policy),
        'summary': {
            'total': len(results),
            'succeeded': succeeded,
            'failed': failed
        },
        'results': results
    }


def store_batch_response(cursor, request_id, batch_id, status_code, response):
    cursor.execute('''
        INSERT OR REPLACE INTO share_batch_requests
            (request_id, batch_id, status_code, response, created_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (request_id, batch_id, status_code, json.dumps(response, ensure_ascii=False), time.time()))


def normalize_batch_items(items):
    """展开 file_ids/files/items，保留客户端用于追踪的 item_id"""
    normalized = []
    seen_item_ids = set()

    if items is None:
        return [], '批量文件列表必须是数组'

    for index, entry in enumerate(items):
        if isinstance(entry, dict):
            item_id = str(entry.get('item_id') or entry.get('client_item_id') or f'item-{index + 1}')
            file_id = entry.get('file_id')
        else:
            item_id = f'item-{index + 1}'
            file_id = entry

        if item_id in seen_item_ids:
            return None, f'批量项ID重复: {item_id}'
        seen_item_ids.add(item_id)
        normalized.append({'item_id': item_id, 'file_id': file_id})

    return normalized, None


@files_bp.route('/api/share/batch', methods=['POST'])
@login_required
def batch_create_shares():
    return _batch_create_shares(None)


@files_bp.route('/api/share/batch/<batch_id>/retry', methods=['POST'])
@login_required
def retry_batch_create_shares(batch_id):
    return _batch_create_shares(batch_id)


def _batch_create_shares(batch_id=None):
    """批量创建分享链接，逐项返回成功或可追踪的失败原因"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '无效的请求数据'}), 400

    raw_request_id = data.get('request_id')
    request_id = raw_request_id.strip() if isinstance(raw_request_id, str) else ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
        return jsonify({'error': 'request_id 必须是 8-100 位字母、数字、下划线或短横线'}), 400

    token = get_token_from_request()
    username = get_username_from_token(token)
    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute(
            'SELECT b.created_by, r.status_code, r.response FROM share_batch_requests r '
            'JOIN share_batch_jobs b ON r.batch_id = b.id WHERE r.request_id = ?',
            (request_id,)
        )
        duplicate = cursor.fetchone()
        if duplicate:
            if duplicate['created_by'] != username:
                return jsonify({'error': 'request_id 已被使用'}), 409
            payload = json.loads(duplicate['response'])
            return jsonify(payload), duplicate['status_code']

        now = time.time()
        if batch_id is None:
            items, item_error = normalize_batch_items(
                data.get('files') if 'files' in data else data.get('file_ids')
            )
            if item_error:
                return jsonify({'error': item_error}), 400
            if not items:
                return jsonify({'error': '请至少选择一个文件'}), 400

            policy, policy_error = normalize_batch_policy(data)
            if policy_error:
                return jsonify({'error': policy_error}), 400

            new_batch_id = str(uuid.uuid4())
            cursor.execute('''
                INSERT INTO share_batch_jobs
                    (id, created_by, expire_hours, expires_at, max_downloads, note,
                     is_enabled, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                new_batch_id, username, policy['expire_hours'], policy['expires_at'],
                policy['max_downloads'], policy['note'], 1 if policy['is_enabled'] else 0,
                'processing', now, now
            ))

            seen_file_ids = set()
            for item in items:
                file_id = item['file_id'] if isinstance(item['file_id'], str) else None
                duplicate_file = bool(file_id and file_id in seen_file_ids)
                if file_id:
                    seen_file_ids.add(file_id)

                status = 'failed' if duplicate_file else 'pending'
                error_code = 'DUPLICATE_ITEM' if duplicate_file else None
                error_message = '同一批量请求中重复选择了该文件' if duplicate_file else None
                retryable = 0
                cursor.execute('''
                    INSERT INTO share_batch_items
                        (batch_id, item_id, file_id, filename, status, error_code,
                         error_message, retryable, share_id, created_at, updated_at)
                        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)
                ''', (
                    new_batch_id, item['item_id'], file_id, status, error_code,
                    error_message, retryable, now, now
                ))
        else:
            cursor.execute('SELECT * FROM share_batch_jobs WHERE id = ?', (batch_id,))
            job = cursor.fetchone()
            if not job:
                return jsonify({'error': '批量任务不存在'}), 404
            if job['created_by'] != username:
                return jsonify({'error': '无权限访问此批量任务'}), 403

            retry_item_ids = None
            if 'files' in data or 'file_ids' in data:
                retry_items, retry_item_error = normalize_batch_items(
                    data.get('files') if 'files' in data else data.get('file_ids')
                )
                if retry_item_error:
                    return jsonify({'error': retry_item_error}), 400
                if not retry_items:
                    return jsonify({'error': '请选择需要重试的项目'}), 400
                retry_item_ids = {item['item_id'] for item in retry_items}

            policy_keys = {'expire_hours', 'max_downloads', 'note', 'is_enabled'}
            if policy_keys & set(data.keys()):
                retry_policy, policy_error = normalize_batch_policy(data)
                if policy_error:
                    return jsonify({'error': policy_error}), 400
                stored_policy = {
                    'expire_hours': job['expire_hours'],
                    'expires_at': job['expires_at'],
                    'max_downloads': job['max_downloads'],
                    'note': job['note'] or '',
                    'is_enabled': bool(job['is_enabled'])
                }
                comparable_stored = policy_snapshot(stored_policy)
                comparable_retry = policy_snapshot(retry_policy)
                if comparable_stored != comparable_retry:
                    return jsonify({'error': '重试必须使用原批量任务的相同策略，避免结果错配'}), 409

            policy = {
                'expire_hours': job['expire_hours'],
                'expires_at': job['expires_at'],
                'max_downloads': job['max_downloads'],
                'note': job['note'] or '',
                'is_enabled': bool(job['is_enabled'])
            }
            new_batch_id = batch_id
            if retry_item_ids is None:
                cursor.execute('''
                    UPDATE share_batch_items
                    SET status = 'pending', updated_at = ?
                    WHERE batch_id = ? AND status = 'failed' AND retryable = 1
                ''', (now, batch_id))
            else:
                placeholders = ','.join('?' for _ in retry_item_ids)
                cursor.execute(f'''
                    UPDATE share_batch_items
                    SET status = 'pending', updated_at = ?
                    WHERE batch_id = ? AND status = 'failed' AND retryable = 1
                      AND item_id IN ({placeholders})
                ''', (now, batch_id, *retry_item_ids))

        cursor.execute('''
            SELECT id, item_id, file_id
            FROM share_batch_items
            WHERE batch_id = ? AND status = 'pending'
            ORDER BY id
        ''', (new_batch_id,))
        pending_rows = cursor.fetchall()

        for row in pending_rows:
            result = process_batch_item(
                cursor,
                {
                    'item_id': row['item_id'],
                    'file_id': row['file_id'],
                    'batch_id': new_batch_id,
                    'request_id': request_id
                },
                username,
                policy
            )
            cursor.execute('''
                UPDATE share_batch_items
                SET file_id = ?, filename = ?, status = ?, error_code = ?,
                    error_message = ?, retryable = ?, share_id = ?, updated_at = ?
                WHERE id = ?
            ''', (
                result['file_id'], result['filename'], result['status'],
                result['error_code'], result['error_message'],
                1 if result['retryable'] else 0, result['share_id'], time.time(), row['id']
            ))

        cursor.execute('''
            SELECT SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   COUNT(*) AS total
            FROM share_batch_items WHERE batch_id = ?
        ''', (new_batch_id,))
        summary = cursor.fetchone()
        final_status = (
            'succeeded' if summary['failed'] == 0
            else 'partial' if summary['succeeded'] else 'failed'
        )
        cursor.execute(
            'UPDATE share_batch_jobs SET status = ?, updated_at = ? WHERE id = ?',
            (final_status, time.time(), new_batch_id)
        )

        response = serialize_batch_job(cursor, new_batch_id)
        store_batch_response(cursor, request_id, new_batch_id, 200, response)
        conn.commit()
        return jsonify(response), 200
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({'error': '请求正在处理或 request_id 冲突，请勿重复提交'}), 409
    except Exception:
        conn.rollback()
        logger.exception('批量创建分享链接失败')
        return jsonify({'error': '批量处理失败，可使用原选择和 request_id 重试'}), 500
    finally:
        conn.close()


@files_bp.route('/api/share/batch/<batch_id>', methods=['GET'])
@login_required
def get_batch_shares(batch_id):
    """刷新或返回后按 batch_id 取回同一条批量结果"""
    token = get_token_from_request()
    username = get_username_from_token(token)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT created_by FROM share_batch_jobs WHERE id = ?', (batch_id,))
        job = cursor.fetchone()
        if not job:
            return jsonify({'error': '批量任务不存在'}), 404
        if job['created_by'] != username:
            return jsonify({'error': '无权限访问此批量任务'}), 403
        response = serialize_batch_job(cursor, batch_id)
        return jsonify(response), 200
    finally:
        conn.close()


@files_bp.route('/api/share', methods=['POST'])
@login_required
def create_share():
    """创建分享链接"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '无效的请求数据'}), 400

    file_id = data.get('file_id', '')
    if not isinstance(file_id, str):
        return jsonify({'error': '文件ID不能为空'}), 400
    file_id = file_id.strip()

    if not file_id:
        return jsonify({'error': '文件ID不能为空'}), 400

    policy = normalize_single_policy(data)
    if policy is None:
        return jsonify({'error': '有效期和最大下载次数必须是整数'}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, path FROM files WHERE id = ?', (file_id,))
    file_info = cursor.fetchone()

    if not file_info:
        conn.close()
        return jsonify({'error': '文件不存在'}), 404

    if not os.path.exists(file_info['path']):
        conn.close()
        return jsonify({'error': '文件不存在'}), 404

    token = get_token_from_request()
    username = get_username_from_token(token)

    share_id = generate_short_id()

    insert_share_link(cursor, share_id, file_id, username, policy)

    conn.commit()
    conn.close()

    logger.info(f"分享链接创建成功: 文件 {file_info['name']}, 分享ID {share_id}, 创建者 {username}")

    return jsonify({
        'success': True,
        'share_id': share_id,
        'expires_at': policy['expires_at'],
        'max_downloads': policy['max_downloads'],
        'filename': file_info['name']
    })


@files_bp.route('/api/share/<share_id>', methods=['GET'])
def get_share(share_id):
    """获取分享链接信息（公开访问）"""
    share = get_share_link_info(share_id)
    valid, error_msg = is_share_valid(share)

    if not share:
        return jsonify({'error': '分享链接不存在'}), 404

    share_data = {
        'share_id': share['id'],
        'filename': share['filename'],
        'filesize': share['filesize'],
        'created_by': share['created_by'],
        'expires_at': share['expires_at'],
        'max_downloads': share['max_downloads'],
        'download_count': share['download_count'],
        'created_at': share['created_at'],
        'note': share['note'] or '',
        'is_enabled': bool(share['is_enabled']),
        'batch_id': share['batch_id'],
        'batch_item_id': share['batch_item_id'],
        'request_id': share['request_id'],
        'is_valid': valid,
        'error_msg': error_msg
    }

    return jsonify(share_data)


@files_bp.route('/api/share/<share_id>/download', methods=['GET'])
def download_by_share(share_id):
    """通过分享链接下载文件（公开访问）"""
    share = get_share_link_info(share_id)
    valid, error_msg = is_share_valid(share)

    if not valid:
        return jsonify({'error': error_msg}), 404

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT name, path FROM files WHERE id = ?', (share['file_id'],))
    file_info = cursor.fetchone()
    conn.close()

    if not file_info:
        return jsonify({'error': '文件不存在'}), 404

    if not os.path.abspath(file_info['path']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        return jsonify({'error': '非法文件路径'}), 403

    if not os.path.exists(file_info['path']):
        return jsonify({'error': '文件不存在'}), 404

    increment_download_count(share_id)

    logger.info(f"分享下载: 文件 {file_info['name']}, 分享ID {share_id}, 下载次数 {share['download_count'] + 1}")
    return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])


@files_bp.route('/api/shares', methods=['GET'])
@login_required
def list_shares():
    """获取当前用户的所有分享链接"""
    token = get_token_from_request()
    username = get_username_from_token(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.file_id, s.created_by, s.expires_at, s.max_downloads, s.download_count, s.created_at,
               s.note, s.is_enabled, s.batch_id, s.batch_item_id, s.request_id,
               f.name as filename, f.size as filesize, f.path
        FROM share_links s
        JOIN files f ON s.file_id = f.id
        WHERE s.created_by = ?
        ORDER BY s.created_at DESC
    ''', (username,))
    shares = cursor.fetchall()
    conn.close()

    result = []
    for share in shares:
        valid, error_msg = is_share_valid(share)
        result.append({
            'share_id': share['id'],
            'file_id': share['file_id'],
            'filename': share['filename'],
            'filesize': share['filesize'],
            'expires_at': share['expires_at'],
            'max_downloads': share['max_downloads'],
            'download_count': share['download_count'],
            'created_at': share['created_at'],
            'note': share['note'] or '',
            'is_enabled': bool(share['is_enabled']),
            'batch_id': share['batch_id'],
            'batch_item_id': share['batch_item_id'],
            'request_id': share['request_id'],
            'is_valid': valid,
            'error_msg': error_msg
        })

    return jsonify(result)


@files_bp.route('/api/share/<share_id>', methods=['PATCH'])
@login_required
def update_share_policy(share_id):
    """更新单条分享生命周期策略；未提供的字段保持不变"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '无效的请求数据'}), 400

    updates = {}
    errors = []
    if 'expire_hours' in data:
        expire_hours = data['expire_hours']
        if not is_int_like(expire_hours) or expire_hours == 0 or expire_hours < -1:
            errors.append('有效期必须是正整数，或使用 -1 表示永久有效')
        else:
            updates['expires_at'] = (
                None if expire_hours == -1 else time.time() + expire_hours * 3600
            )

    if 'max_downloads' in data:
        max_downloads = data['max_downloads']
        if not is_int_like(max_downloads) or max_downloads == 0 or max_downloads < -1:
            errors.append('取件次数上限必须是正整数，或使用 -1 表示无限制')
        else:
            updates['max_downloads'] = None if max_downloads == -1 else max_downloads

    if 'note' in data:
        note = data['note']
        if note is None:
            note = ''
        if not isinstance(note, str):
            errors.append('备注必须是文本')
        elif len(note.strip()) > 500:
            errors.append('备注不能超过 500 个字符')
        else:
            updates['note'] = note.strip()

    if 'is_enabled' in data:
        if not isinstance(data['is_enabled'], bool):
            errors.append('启停状态必须是布尔值')
        else:
            updates['is_enabled'] = 1 if data['is_enabled'] else 0

    if errors:
        return jsonify({'error': '；'.join(errors)}), 400
    if not updates:
        return jsonify({'error': '没有需要更新的策略字段'}), 400

    token = get_token_from_request()
    username = get_username_from_token(token)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT id, created_by FROM share_links WHERE id = ?',
            (share_id,)
        )
        share = cursor.fetchone()
        if not share:
            return jsonify({'error': '分享链接不存在'}), 404
        if share['created_by'] != username:
            return jsonify({'error': '无权限修改此分享链接'}), 403

        assignments = ', '.join(f'{column} = ?' for column in updates)
        values = list(updates.values()) + [share_id]
        cursor.execute(f'UPDATE share_links SET {assignments} WHERE id = ?', values)
        conn.commit()

        updated = get_share_link_info(share_id)
        valid, error_msg = is_share_valid(updated)
        return jsonify({
            'success': True,
            'share_id': updated['id'],
            'file_id': updated['file_id'],
            'filename': updated['filename'],
            'expires_at': updated['expires_at'],
            'max_downloads': updated['max_downloads'],
            'download_count': updated['download_count'],
            'note': updated['note'] or '',
            'is_enabled': bool(updated['is_enabled']),
            'batch_id': updated['batch_id'],
            'batch_item_id': updated['batch_item_id'],
            'request_id': updated['request_id'],
            'is_valid': valid,
            'error_msg': error_msg
        })
    finally:
        conn.close()


@files_bp.route('/api/share/<share_id>', methods=['DELETE'])
@login_required
def delete_share(share_id):
    """删除分享链接"""
    token = get_token_from_request()
    username = get_username_from_token(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT created_by, file_id FROM share_links WHERE id = ?', (share_id,))
    share = cursor.fetchone()

    if not share:
        conn.close()
        return jsonify({'error': '分享链接不存在'}), 404

    if share['created_by'] != username:
        conn.close()
        return jsonify({'error': '无权限删除此分享链接'}), 403

    cursor.execute('DELETE FROM share_links WHERE id = ?', (share_id,))
    conn.commit()
    conn.close()

    logger.info(f"分享链接删除: 分享ID {share_id}, 文件ID {share['file_id']}, 操作者 {username}")
    return jsonify({'success': True, 'message': '分享链接已删除'})
