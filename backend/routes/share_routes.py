"""分享链接路由：单条分享（原规则）+ 批量策略 + 生命周期管理"""
import os
import re
import uuid
import time
import logging
from flask import request, jsonify, send_file
from routes import shares_bp
from database import get_db
from auth import get_username_from_token, login_required
from config import (
    UPLOAD_FOLDER,
    SHARE_LINK_EXPIRE_HOURS,
    SHARE_LINK_MAX_DOWNLOADS,
)

logger = logging.getLogger(__name__)

NOTE_MAX_LENGTH = 200
BATCH_MAX_ITEMS = 100
BATCH_ID_PATTERN = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')


def generate_short_id():
    """生成短的分享链接ID（极小概率碰撞时重试）"""
    conn = None
    for _ in range(5):
        candidate = uuid.uuid4().hex[:12]
        if conn is None:
            conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM share_links WHERE id = ?', (candidate,))
        if cursor.fetchone() is None:
            conn.close()
            return candidate
    conn.close()
    return uuid.uuid4().hex[:12]


def get_token_from_request():
    """从请求中获取 token"""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return request.args.get('token')


def is_share_valid(share):
    """检查分享链接是否有效：停用 -> 过期 -> 取件次数用尽"""
    if not share:
        return False, '分享链接不存在'

    if not share['active']:
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


def normalize_note(note):
    """备注清洗：非字符串视为空，超长截断由上层校验拦截"""
    if note is None:
        return None
    if not isinstance(note, str):
        raise ValueError('备注必须是文本')
    note = note.strip()
    if len(note) > NOTE_MAX_LENGTH:
        raise ValueError(f'备注不能超过 {NOTE_MAX_LENGTH} 个字符')
    return note or None


def normalize_expire_hours(value, default_hours):
    """统一有效期规则：缺省走默认值；-1/None 表示永久；只接受正数小时"""
    if value is None:
        value = default_hours
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError('有效期必须是数字（小时）')
    if value < 0:
        return None
    if value == 0:
        raise ValueError('有效期必须大于 0，或传入 -1 表示永久')
    return time.time() + value * 3600


def normalize_max_downloads(value, default_value):
    """统一次数上限规则：缺省走默认值；-1/None 表示不限；只接受正整数"""
    if value is None:
        value = default_value
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('取件次数上限必须是整数（-1 表示不限）')
    if value < 0:
        return None
    if value == 0:
        raise ValueError('取件次数上限必须大于 0，或传入 -1 表示不限')
    return value


def get_share_row(share_id):
    """读取分享记录 + 文件信息（LEFT JOIN：文件记录被删除时仍可识别为失效）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.file_id, s.created_by, s.expires_at, s.max_downloads,
               s.download_count, s.note, s.active, s.batch_id, s.created_at,
               f.name as filename, f.size as filesize, f.path as filepath
        FROM share_links s
        LEFT JOIN files f ON s.file_id = f.id
        WHERE s.id = ?
    ''', (share_id,))
    share = cursor.fetchone()
    conn.close()
    return share


def serialize_share(share, *, is_valid=None, error_msg=None):
    """分享记录序列化（列表/详情共用，保证字段不漂移）"""
    if is_valid is None:
        is_valid, error_msg = is_share_valid(share)
    return {
        'share_id': share['id'],
        'file_id': share['file_id'],
        'filename': share['filename'],
        'filesize': share['filesize'],
        'created_by': share['created_by'],
        'expires_at': share['expires_at'],
        'max_downloads': share['max_downloads'],
        'download_count': share['download_count'],
        'note': share['note'],
        'active': bool(share['active']),
        'batch_id': share['batch_id'],
        'created_at': share['created_at'],
        'is_valid': is_valid,
        'error_msg': error_msg
    }


# ---------------------------------------------------------------------------
# 单条分享：完全沿用既有规则，仅在响应中附带新增的备注/启停字段
# ---------------------------------------------------------------------------

@shares_bp.route('/api/share', methods=['POST'])
@login_required
def create_share():
    """创建分享链接（单条，原规则）"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': '无效的请求数据'}), 400

    file_id = (data.get('file_id') or '').strip()
    expire_hours = data.get('expire_hours')
    max_downloads = data.get('max_downloads')

    if not file_id:
        return jsonify({'error': '文件ID不能为空'}), 400

    try:
        note = normalize_note(data.get('note'))
        active = data.get('active', True)
        if not isinstance(active, bool):
            raise ValueError('启停状态必须是布尔值')
        expires_at = normalize_expire_hours(expire_hours, SHARE_LINK_EXPIRE_HOURS)
        max_downloads = normalize_max_downloads(max_downloads, SHARE_LINK_MAX_DOWNLOADS)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, path FROM files WHERE id = ?', (file_id,))
    file_info = cursor.fetchone()

    if not file_info:
        conn.close()
        return jsonify({'error': '文件不存在'}), 404

    if not os.path.exists(file_info['path']):
        conn.close()
        return jsonify({'error': '文件数据已失效，无法创建分享'}), 409

    token = get_token_from_request()
    username = get_username_from_token(token)
    share_id = generate_short_id()

    cursor.execute('''
        INSERT INTO share_links
            (id, file_id, created_by, expires_at, max_downloads, note, active)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (share_id, file_id, username, expires_at, max_downloads, note, int(active)))

    conn.commit()
    conn.close()

    logger.info(
        f"分享链接创建成功: 文件 {file_info['name']}, 分享ID {share_id}, 创建者 {username}"
    )

    return jsonify({
        'success': True,
        'share_id': share_id,
        'expires_at': expires_at,
        'max_downloads': max_downloads,
        'note': note,
        'active': active,
        'filename': file_info['name']
    })


@shares_bp.route('/api/share/<share_id>', methods=['GET'])
def get_share(share_id):
    """获取分享链接信息（公开访问）"""
    share = get_share_row(share_id)

    if not share:
        return jsonify({'error': '分享链接不存在'}), 404

    valid, error_msg = is_share_valid(share)

    # 文件记录/实体缺失：链接本身已无意义
    file_missing = share['filename'] is None or not (
        share['filepath'] and os.path.exists(share['filepath'])
    )
    if file_missing:
        valid, error_msg = False, '分享的文件已失效'

    data = serialize_share(share, is_valid=valid, error_msg=error_msg)
    # 公开页不展示管理备注
    data.pop('note', None)
    data.pop('batch_id', None)
    return jsonify(data)


@shares_bp.route('/api/share/<share_id>/download', methods=['GET'])
def download_by_share(share_id):
    """通过分享链接下载文件（公开访问）"""
    share = get_share_row(share_id)
    valid, error_msg = is_share_valid(share)

    if not valid:
        return jsonify({'error': error_msg}), 404

    if share['filename'] is None:
        return jsonify({'error': '分享的文件已失效'}), 404

    if not os.path.abspath(share['filepath']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        return jsonify({'error': '非法文件路径'}), 403

    if not os.path.exists(share['filepath']):
        return jsonify({'error': '分享的文件已失效'}), 404

    increment_download_count(share_id)

    logger.info(
        f"分享下载: 文件 {share['filename']}, 分享ID {share_id}, "
        f"下载次数 {share['download_count'] + 1}"
    )
    return send_file(
        share['filepath'], as_attachment=True, download_name=share['filename']
    )


@shares_bp.route('/api/shares', methods=['GET'])
@login_required
def list_shares():
    """获取当前用户的所有分享链接"""
    token = get_token_from_request()
    username = get_username_from_token(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.file_id, s.created_by, s.expires_at, s.max_downloads,
               s.download_count, s.note, s.active, s.batch_id, s.created_at,
               f.name as filename, f.size as filesize, f.path as filepath
        FROM share_links s
        LEFT JOIN files f ON s.file_id = f.id
        WHERE s.created_by = ?
        ORDER BY s.created_at DESC
    ''', (username,))
    shares = cursor.fetchall()
    conn.close()

    result = []
    for share in shares:
        valid, error_msg = is_share_valid(share)
        if share['filename'] is None:
            valid, error_msg = False, '文件已失效'
        elif not (share['filepath'] and os.path.exists(share['filepath'])):
            valid, error_msg = False, '文件数据已失效'
        result.append(serialize_share(share, is_valid=valid, error_msg=error_msg))

    return jsonify(result)


@shares_bp.route('/api/share/<share_id>', methods=['PATCH'])
@login_required
def update_share(share_id):
    """局部更新单条分享的生命周期策略；未提供的字段一律保持原值"""
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({'error': '无效的请求数据'}), 400

    token = get_token_from_request()
    username = get_username_from_token(token)

    share = get_share_row(share_id)
    if not share:
        return jsonify({'error': '分享链接不存在'}), 404
    if share['created_by'] != username:
        return jsonify({'error': '无权限修改此分享链接'}), 403

    updatable = {'expire_hours', 'max_downloads', 'note', 'active'}
    provided = {k for k in data.keys() if k in updatable}
    ignored = set(data.keys()) - updatable
    if not provided:
        return jsonify({'error': '没有需要更新的字段'}), 400
    if ignored:
        return jsonify({'error': f'不支持的字段: {sorted(ignored)}'}), 400

    expires_at = share['expires_at']
    max_downloads = share['max_downloads']
    note = share['note']
    active = bool(share['active'])

    try:
        if 'expire_hours' in provided:
            value = data['expire_hours']
            # 显式 null 也表示永久有效
            expires_at = None if value is None else normalize_expire_hours(value, None)
        if 'max_downloads' in provided:
            value = data['max_downloads']
            max_downloads = None if value is None else normalize_max_downloads(value, None)
        if 'note' in provided:
            # 传空字符串清空备注
            note = normalize_note(data['note'])
        if 'active' in provided:
            if not isinstance(data['active'], bool):
                raise ValueError('启停状态必须是布尔值')
            active = data['active']
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    # 策略冲突：新上限不能低于已发生的取件次数
    if max_downloads is not None and max_downloads < share['download_count']:
        return jsonify({
            'error': f"取件次数上限不能低于已取件次数（{share['download_count']}），请调整后重试",
            'error_code': 'POLICY_CONFLICT',
            'retryable': True,
            'current_download_count': share['download_count']
        }), 409

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE share_links
        SET expires_at = ?, max_downloads = ?, note = ?, active = ?
        WHERE id = ? AND created_by = ?
    ''', (expires_at, max_downloads, note, int(active), share_id, username))
    conn.commit()
    conn.close()

    logger.info(f"分享链接策略更新: 分享ID {share_id}, 字段 {sorted(provided)}, 操作者 {username}")

    updated = get_share_row(share_id)
    return jsonify({'success': True, 'share': serialize_share(updated)})


@shares_bp.route('/api/share/<share_id>', methods=['DELETE'])
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


# ---------------------------------------------------------------------------
# 批量分享：统一策略 -> 逐条处理 -> 逐条结果（含可重试标记）-> 批次持久化
# ---------------------------------------------------------------------------

def _build_batch_result(batch_id, policy, rows, idempotent_rehit, created_at=None):
    """根据批次主表 + 明细表行构造稳定的批量响应结构"""
    succeeded = sum(1 for r in rows if r['status'] == 'succeeded')
    failed = sum(1 for r in rows if r['status'] == 'failed')
    retryable = sum(1 for r in rows if r['status'] == 'failed' and r['retryable'])

    results = [{
        'client_ref': r['client_ref'],
        'file_id': r['file_id'],
        'filename': r['filename'],
        'status': r['status'],
        'retryable': bool(r['retryable']),
        'error_code': r['error_code'],
        'error_message': r['error_message'],
        'share_id': r['share_id'],
        'note': r['note'],
        'expires_at': r['expires_at'],
        'max_downloads': r['max_downloads']
    } for r in rows]

    return {
        'success': True,
        'batch_id': batch_id,
        'idempotent_rehit': idempotent_rehit,
        'created_at': created_at,
        'summary': {
            'total': len(rows),
            'succeeded': succeeded,
            'failed': failed,
            'retryable': retryable
        },
        'policy': policy,
        'results': results
    }


@shares_bp.route('/api/shares/batch', methods=['POST'])
@login_required
def create_share_batch():
    """批量创建分享链接，逐条返回成功/失败原因；批次结果持久化可回查"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '无效的请求数据'}), 400

    token = get_token_from_request()
    username = get_username_from_token(token)

    # 幂等键：前端重复提交（双击/网络重试）同 batch_id 时直接返回首次结果
    batch_id = data.get('batch_id')
    if batch_id is not None:
        if not isinstance(batch_id, str) or not BATCH_ID_PATTERN.match(batch_id):
            return jsonify({
                'error': 'batch_id 仅允许字母、数字、下划线和短横线，长度不超过 64'
            }), 400
        existing = _load_batch(batch_id, username)
        if existing is not None:
            return jsonify(existing), 200
    else:
        batch_id = uuid.uuid4().hex

    raw_items = data.get('items')
    if not isinstance(raw_items, list) or len(raw_items) == 0:
        return jsonify({'error': '请至少选择一个文件'}), 400
    if len(raw_items) > BATCH_MAX_ITEMS:
        return jsonify({'error': f'单次最多批量处理 {BATCH_MAX_ITEMS} 个文件'}), 400

    policy_data = data.get('policy') or {}
    if not isinstance(policy_data, dict):
        return jsonify({'error': '策略配置格式不正确'}), 400

    # 统一策略校验：策略非法属于整批请求错误，不落任何记录
    try:
        expires_at = normalize_expire_hours(
            policy_data.get('expire_hours'), SHARE_LINK_EXPIRE_HOURS
        )
        max_downloads = normalize_max_downloads(
            policy_data.get('max_downloads'), SHARE_LINK_MAX_DOWNLOADS
        )
        policy_note = normalize_note(policy_data.get('note'))
        active = policy_data.get('active', True)
        if not isinstance(active, bool):
            raise ValueError('启停状态必须是布尔值')
    except ValueError as exc:
        return jsonify({'error': f'策略校验失败: {exc}'}), 400

    # client_ref 是结果与选择项对齐的唯一依据，缺失/重复直接拒绝整批
    refs = []
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            return jsonify({'error': f'第 {idx + 1} 项数据格式不正确'}), 400
        ref = item.get('client_ref')
        if not isinstance(ref, str) or not ref.strip():
            return jsonify({'error': f'第 {idx + 1} 项缺少 client_ref，结果无法对齐'}), 400
        refs.append(ref.strip())
    if len(refs) != len(set(refs)):
        return jsonify({'error': '批次内存在重复的 client_ref'}), 400

    # 逐条备注覆盖（为空则回落统一备注）
    item_notes = {}
    for item in raw_items:
        try:
            item_notes[item['client_ref'].strip()] = normalize_note(item.get('note'))
        except ValueError as exc:
            return jsonify(
                {'error': f"文件 {item.get('client_ref')} 的备注不合法: {exc}"}
            ), 400

    file_ids = [str(item.get('file_id', '')).strip() for item in raw_items]

    # 同一批次内重复选择同一文件：首个照常处理，后续重复项判为不可重试失败
    seen_file_ids = set()
    duplicate_refs = set()
    for item, fid in zip(raw_items, file_ids):
        if fid and fid in seen_file_ids:
            duplicate_refs.add(item['client_ref'].strip())
        if fid:
            seen_file_ids.add(fid)

    conn = get_db()
    cursor = conn.cursor()

    # 一次性取出全部目标文件，避免逐条查询
    placeholders = ','.join('?' for _ in seen_file_ids)
    file_map = {}
    if seen_file_ids:
        cursor.execute(
            f'SELECT id, name, path FROM files WHERE id IN ({placeholders})',
            tuple(seen_file_ids)
        )
        for row in cursor.fetchall():
            file_map[row['id']] = row

    # 策略冲突预检：该用户对同一文件已存在启用中的分享，批量不覆盖既有记录
    conflict_file_ids = set()
    if seen_file_ids:
        cursor.execute(f'''
            SELECT file_id FROM share_links
            WHERE created_by = ? AND active = 1 AND file_id IN ({placeholders})
        ''', (username, *tuple(seen_file_ids)))
        conflict_file_ids = {row['file_id'] for row in cursor.fetchall()}

    item_rows = []
    for seq, (item, fid) in enumerate(zip(raw_items, file_ids)):
        ref = item['client_ref'].strip()
        note = item_notes.get(ref) if item_notes.get(ref) is not None else policy_note
        row = {
            'client_ref': ref, 'file_id': fid or None, 'filename': None,
            'status': 'failed', 'retryable': False, 'error_code': None,
            'error_message': None, 'note': note, 'share_id': None,
            'expires_at': None, 'max_downloads': None, 'seq': seq
        }

        if not fid:
            row['error_code'] = 'INVALID_ITEM'
            row['error_message'] = '缺少文件ID'
        elif ref in duplicate_refs:
            row['error_code'] = 'DUPLICATE_FILE'
            row['error_message'] = '同一批次内重复选择了该文件，请去掉重复项后重试'
        else:
            file_info = file_map.get(fid)
            if file_info is None:
                row['error_code'] = 'FILE_NOT_FOUND'
                row['error_message'] = '文件不存在或已被删除'
                row['retryable'] = True
            elif not os.path.exists(file_info['path']):
                row['filename'] = file_info['name']
                row['error_code'] = 'FILE_UNAVAILABLE'
                row['error_message'] = '文件数据已失效，请重新上传后重试'
                row['retryable'] = True
            elif fid in conflict_file_ids:
                row['filename'] = file_info['name']
                row['error_code'] = 'POLICY_CONFLICT'
                row['error_message'] = '该文件已有启用中的分享链接，批量操作不会覆盖既有记录'
                row['retryable'] = True
            else:
                # 通过全部校验：落分享记录
                share_id = generate_short_id()
                cursor.execute('''
                    INSERT INTO share_links
                        (id, file_id, created_by, expires_at, max_downloads,
                         download_count, note, active, batch_id)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                ''', (share_id, fid, username, expires_at, max_downloads,
                      note, int(active), batch_id))
                row.update({
                    'status': 'succeeded',
                    'filename': file_info['name'],
                    'share_id': share_id,
                    'expires_at': expires_at,
                    'max_downloads': max_downloads
                })

        item_rows.append(row)

    succeeded = sum(1 for r in item_rows if r['status'] == 'succeeded')
    failed = len(item_rows) - succeeded
    retryable = sum(1 for r in item_rows if r['status'] == 'failed' and r['retryable'])

    # 批次主表：策略配置与统计快照
    cursor.execute('''
        INSERT INTO share_batches
            (batch_id, created_by, status, total_count, success_count,
             failed_count, retryable_count, policy_expires_at,
             policy_max_downloads, policy_note, policy_active)
        VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (batch_id, username, len(item_rows), succeeded, failed, retryable,
          expires_at, max_downloads, policy_note, int(active)))

    # 批次明细：逐条结果持久化，刷新/返回页面后按 batch_id + client_ref 精确回查
    cursor.executemany('''
        INSERT INTO share_batch_items
            (batch_id, client_ref, file_id, filename, status, retryable,
             error_code, error_message, note, share_id, expires_at,
             max_downloads, seq)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', [(
        batch_id, r['client_ref'], r['file_id'], r['filename'], r['status'],
        int(r['retryable']), r['error_code'], r['error_message'], r['note'],
        r['share_id'], r['expires_at'], r['max_downloads'], r['seq']
    ) for r in item_rows])

    conn.commit()

    cursor.execute('SELECT created_at FROM share_batches WHERE batch_id = ?', (batch_id,))
    created_at = cursor.fetchone()['created_at']
    conn.close()

    logger.info(
        f"批量分享提交: 批次 {batch_id}, 用户 {username}, "
        f"共 {len(item_rows)} 条，成功 {succeeded}，失败 {failed}（可重试 {retryable}）"
    )

    policy = {
        'expires_at': expires_at,
        'max_downloads': max_downloads,
        'note': policy_note,
        'active': active
    }
    response_rows = [
        {k: r[k] for k in (
            'client_ref', 'file_id', 'filename', 'status', 'retryable',
            'error_code', 'error_message', 'note', 'share_id',
            'expires_at', 'max_downloads'
        )}
        for r in item_rows
    ]
    result = _build_batch_result(batch_id, policy, response_rows, False)
    result['created_at'] = created_at
    return jsonify(result), 200


def _load_batch(batch_id, username):
    """读取本用户的历史批次结果（用于刷新/返回后恢复，杜绝结果错配）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM share_batches WHERE batch_id = ? AND created_by = ?',
        (batch_id, username)
    )
    batch = cursor.fetchone()
    if batch is None:
        conn.close()
        return None

    cursor.execute('''
        SELECT client_ref, file_id, filename, status, retryable, error_code,
               error_message, note, share_id, expires_at, max_downloads
        FROM share_batch_items
        WHERE batch_id = ?
        ORDER BY seq ASC, id ASC
    ''', (batch_id,))
    rows = cursor.fetchall()
    conn.close()

    policy = {
        'expires_at': batch['policy_expires_at'],
        'max_downloads': batch['policy_max_downloads'],
        'note': batch['policy_note'],
        'active': bool(batch['policy_active'])
    }
    return _build_batch_result(
        batch_id, policy, rows, True, created_at=batch['created_at']
    )


@shares_bp.route('/api/shares/batch/<batch_id>', methods=['GET'])
@login_required
def get_share_batch(batch_id):
    """回查批次结果（仅创建者本人）"""
    token = get_token_from_request()
    username = get_username_from_token(token)

    result = _load_batch(batch_id, username)
    if result is None:
        return jsonify({'error': '批次不存在或无权查看'}), 404
    return jsonify(result)
