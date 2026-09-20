"""批量分享链接生命周期测试"""
import io
import time


def _upload(client, name='batch.txt', content=b'batch content'):
    resp = client.post(
        '/api/upload',
        data={'file': (io.BytesIO(content), name)},
        content_type='multipart/form-data'
    )
    return resp.get_json()['file_id']


def _batch(client, token, items, policy=None, batch_id=None):
    payload = {'items': items}
    if policy is not None:
        payload['policy'] = policy
    if batch_id is not None:
        payload['batch_id'] = batch_id
    return client.post(
        '/api/shares/batch',
        json=payload,
        headers={'Authorization': f'Bearer {token}'}
    )


# ---------------------------------------------------------------------------
# 请求校验
# ---------------------------------------------------------------------------

def test_batch_without_auth(client):
    """未登录禁止批量提交"""
    resp = client.post('/api/shares/batch', json={'items': []})
    assert resp.status_code == 401


def test_batch_empty_selection(client, auth_token):
    """空选择直接拒绝，不落任何记录"""
    resp = _batch(client, auth_token, [])
    assert resp.status_code == 400
    assert '至少选择' in resp.get_json()['error']


def test_batch_items_not_list(client, auth_token):
    resp = client.post('/api/shares/batch', json={'items': 'x'},
                       headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.status_code == 400


def test_batch_missing_client_ref(client, auth_token):
    """缺少 client_ref 无法对齐结果，整批拒绝"""
    fid = _upload(client, 'noref.txt')
    resp = _batch(client, auth_token, [{'file_id': fid}])
    assert resp.status_code == 400
    assert 'client_ref' in resp.get_json()['error']


def test_batch_duplicate_client_ref(client, auth_token):
    """批次内 client_ref 重复，整批拒绝"""
    fid = _upload(client, 'dupref.txt')
    resp = _batch(
        client, auth_token,
        [{'file_id': fid, 'client_ref': 'r1'},
         {'file_id': fid, 'client_ref': 'r1'}]
    )
    assert resp.status_code == 400


def test_batch_invalid_policy_expire_zero(client, auth_token):
    """策略冲突/非法属于整批请求错误"""
    fid = _upload(client, 'policy0.txt')
    resp = _batch(client, auth_token,
                  [{'file_id': fid, 'client_ref': 'r1'}],
                  policy={'expire_hours': 0})
    assert resp.status_code == 400
    assert '策略校验失败' in resp.get_json()['error']


def test_batch_invalid_policy_max_downloads(client, auth_token):
    fid = _upload(client, 'policydl.txt')
    resp = _batch(client, auth_token,
                  [{'file_id': fid, 'client_ref': 'r1'}],
                  policy={'max_downloads': 0})
    assert resp.status_code == 400


def test_batch_bad_batch_id(client, auth_token):
    fid = _upload(client, 'badid.txt')
    resp = _batch(client, auth_token,
                  [{'file_id': fid, 'client_ref': 'r1'}],
                  batch_id='../evil')
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 逐条成功 / 失败
# ---------------------------------------------------------------------------

def test_batch_all_success(client, auth_token):
    fid1 = _upload(client, 'b1.txt', b'1')
    fid2 = _upload(client, 'b2.txt', b'22')

    resp = _batch(client, auth_token, [
        {'file_id': fid1, 'client_ref': 'c1'},
        {'file_id': fid2, 'client_ref': 'c2'}
    ], policy={'expire_hours': 12, 'max_downloads': 3,
               'note': '统一备注', 'active': False})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data['summary'] == {
        'total': 2, 'succeeded': 2, 'failed': 0, 'retryable': 0
    }
    # 结果顺序与 client_ref 严格对齐
    assert [r['client_ref'] for r in data['results']] == ['c1', 'c2']
    assert all(r['status'] == 'succeeded' for r in data['results'])
    assert all(r['note'] == '统一备注' for r in data['results'])

    # 记录以停用状态创建
    shares = client.get('/api/shares',
                        headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    by_id = {s['share_id']: s for s in shares}
    for r in data['results']:
        share = by_id[r['share_id']]
        assert share['active'] is False
        assert share['note'] == '统一备注'
        assert share['batch_id'] == data['batch_id']
        assert share['max_downloads'] == 3


def test_batch_partial_failure_retryable(client, auth_token):
    fid = _upload(client, 'ok.txt', b'ok')

    resp = _batch(client, auth_token, [
        {'file_id': fid, 'client_ref': 'good'},
        {'file_id': 'missing-file-id', 'client_ref': 'gone'}
    ], policy={'expire_hours': 24, 'max_downloads': 5})

    data = resp.get_json()
    assert data['summary']['succeeded'] == 1
    assert data['summary']['failed'] == 1
    assert data['summary']['retryable'] == 1

    by_ref = {r['client_ref']: r for r in data['results']}
    assert by_ref['good']['status'] == 'succeeded'
    failed = by_ref['gone']
    assert failed['status'] == 'failed'
    assert failed['retryable'] is True
    assert failed['error_code'] == 'FILE_NOT_FOUND'
    assert failed['share_id'] is None


def test_batch_duplicate_file_in_selection(client, auth_token):
    """同批重复选择同一文件：首项成功，重复项不可重试失败"""
    fid = _upload(client, 'twice.txt')
    resp = _batch(client, auth_token, [
        {'file_id': fid, 'client_ref': 'first'},
        {'file_id': fid, 'client_ref': 'second'}
    ])
    data = resp.get_json()
    by_ref = {r['client_ref']: r for r in data['results']}
    assert by_ref['first']['status'] == 'succeeded'
    assert by_ref['second']['status'] == 'failed'
    assert by_ref['second']['retryable'] is False
    assert by_ref['second']['error_code'] == 'DUPLICATE_FILE'


def test_batch_policy_conflict_with_existing_active_share(client, auth_token):
    """已有启用中的分享时，批量不覆盖，保留为可重试项"""
    fid = _upload(client, 'conflict.txt')

    first = client.post('/api/share', json={'file_id': fid},
                        headers={'Authorization': f'Bearer {auth_token}'})
    assert first.status_code == 200

    resp = _batch(client, auth_token,
                  [{'file_id': fid, 'client_ref': 'c1'}])
    item = resp.get_json()['results'][0]
    assert item['status'] == 'failed'
    assert item['retryable'] is True
    assert item['error_code'] == 'POLICY_CONFLICT'


def test_batch_conflict_cleared_after_disabling_old_share(client, auth_token):
    """停用旧分享后可重试成功，旧记录不被覆盖（新策略生成新链接）"""
    fid = _upload(client, 'retry.txt')
    old = client.post('/api/share', json={'file_id': fid, 'max_downloads': 2},
                      headers={'Authorization': f'Bearer {auth_token}'}).get_json()

    resp1 = _batch(client, auth_token,
                   [{'file_id': fid, 'client_ref': 'c1'}],
                   policy={'max_downloads': 9})
    assert resp1.get_json()['results'][0]['error_code'] == 'POLICY_CONFLICT'

    client.patch(f"/api/share/{old['share_id']}", json={'active': False},
                 headers={'Authorization': f'Bearer {auth_token}'})

    resp2 = _batch(client, auth_token,
                   [{'file_id': fid, 'client_ref': 'c1'}],
                   batch_id='retry-batch-1',
                   policy={'max_downloads': 9})
    assert resp2.status_code == 200
    item = resp2.get_json()['results'][0]
    assert item['status'] == 'succeeded'

    # 旧链接仍保持原策略，没有被覆盖
    old_info = client.get(f"/api/share/{old['share_id']}").get_json()
    assert old_info['max_downloads'] == 2


def test_batch_item_note_overrides_policy_note(client, auth_token):
    fid = _upload(client, 'notes.txt')
    resp = _batch(client, auth_token, [{
        'file_id': fid, 'client_ref': 'c1', 'note': '单独备注'
    }], policy={'note': '统一备注'})
    item = resp.get_json()['results'][0]
    assert item['note'] == '单独备注'


def test_batch_unlimited_policy(client, auth_token):
    fid = _upload(client, 'forever.txt')
    resp = _batch(client, auth_token,
                  [{'file_id': fid, 'client_ref': 'c1'}],
                  policy={'expire_hours': -1, 'max_downloads': -1})
    item = resp.get_json()['results'][0]
    assert item['status'] == 'succeeded'
    assert item['expires_at'] is None
    assert item['max_downloads'] is None


def test_batch_file_entity_missing_is_retryable(client, auth_token):
    """磁盘实体丢失的文件：标记失败但可重试，文件名仍带回以便对齐"""
    import os
    fid = _upload(client, 'ghost.txt')

    conn = __import__('database').get_db()
    path = conn.execute('SELECT path FROM files WHERE id=?', (fid,)).fetchone()['path']
    conn.close()
    os.remove(path)

    resp = _batch(client, auth_token,
                  [{'file_id': fid, 'client_ref': 'c1'}])
    item = resp.get_json()['results'][0]
    assert item['status'] == 'failed'
    assert item['retryable'] is True
    assert item['error_code'] == 'FILE_UNAVAILABLE'
    assert item['filename'] == 'ghost.txt'


def test_list_shares_marks_missing_file_invalid(client, auth_token):
    """文件实体失效后，既有分享记录在列表中显示为失效而不是消失/错配"""
    import os
    fid = _upload(client, 'vanish.txt')
    sid = client.post('/api/share', json={'file_id': fid},
                      headers={'Authorization': f'Bearer {auth_token}'}).get_json()['share_id']

    conn = __import__('database').get_db()
    path = conn.execute('SELECT path FROM files WHERE id=?', (fid,)).fetchone()['path']
    conn.close()
    os.remove(path)

    shares = client.get('/api/shares',
                        headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    mine = [s for s in shares if s['share_id'] == sid][0]
    assert mine['is_valid'] is False
    assert '失效' in mine['error_msg']


# ---------------------------------------------------------------------------
# 幂等：重复提交 / 刷新回查
# ---------------------------------------------------------------------------

def test_batch_idempotent_duplicate_submit(client, auth_token):
    """相同 batch_id 重复提交返回首次结果，不重复建链接"""
    fid = _upload(client, 'idempotent.txt')
    items = [{'file_id': fid, 'client_ref': 'c1'}]

    first = _batch(client, auth_token, items, batch_id='idem-1')
    second = _batch(client, auth_token, items, batch_id='idem-1')

    assert first.status_code == 200
    assert second.status_code == 200
    d1, d2 = first.get_json(), second.get_json()
    assert d1['results'][0]['share_id'] == d2['results'][0]['share_id']
    assert d2['idempotent_rehit'] is True

    shares = client.get('/api/shares',
                        headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    batch_shares = [s for s in shares if s['batch_id'] == 'idem-1']
    assert len(batch_shares) == 1


def test_batch_result_persisted_and_requery_matches(client, auth_token):
    """刷新/返回后按 batch_id 回查，结果与首次完全一致且不串位"""
    fid1 = _upload(client, 'p1.txt', b'1')
    fid2 = _upload(client, 'p2.txt', b'2')

    first = _batch(client, auth_token, [
        {'file_id': fid1, 'client_ref': 'alpha'},
        {'file_id': fid2, 'client_ref': 'beta', 'note': 'B'},
        {'file_id': 'ghost', 'client_ref': 'gamma'}
    ], batch_id='persist-1', policy={'note': 'A', 'expire_hours': 48})
    original = first.get_json()

    reloaded = client.get('/api/shares/batch/persist-1',
                          headers={'Authorization': f'Bearer {auth_token}'})
    assert reloaded.status_code == 200
    again = reloaded.get_json()

    assert again['idempotent_rehit'] is True
    assert again['summary'] == original['summary']
    assert [r['client_ref'] for r in again['results']] == ['alpha', 'beta', 'gamma']
    for a, b in zip(original['results'], again['results']):
        assert a == b


def test_batch_requery_forbidden_for_other_user(client, auth_token, db_conn):
    fid = _upload(client, 'owner.txt')
    _batch(client, auth_token, [{'file_id': fid, 'client_ref': 'c1'}],
           batch_id='owner-batch')

    # 以另一个用户身份访问
    from auth import rate_limit_store
    rate_limit_store.clear()
    other = client.post('/api/auth', json={'username': 'user', 'password': 'user123'})
    other_token = other.get_json()['token']
    resp = client.get('/api/shares/batch/owner-batch',
                      headers={'Authorization': f'Bearer {other_token}'})
    assert resp.status_code == 404


def test_batch_requery_missing(client, auth_token):
    resp = client.get('/api/shares/batch/no-such-batch',
                      headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 单条生命周期：PATCH 局部更新
# ---------------------------------------------------------------------------

def test_patch_share_partial_update_keeps_untouched(client, auth_token):
    """PATCH 只更新传入字段，未修改字段不能被覆盖"""
    fid = _upload(client, 'patch.txt')
    created = client.post('/api/share', json={
        'file_id': fid, 'expire_hours': 24, 'max_downloads': 5, 'note': '原备注'
    }, headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    sid = created['share_id']
    original_expire = created['expires_at']

    resp = client.patch(f'/api/share/{sid}', json={'active': False},
                        headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.status_code == 200
    share = resp.get_json()['share']
    assert share['active'] is False
    assert share['note'] == '原备注'
    assert share['max_downloads'] == 5
    assert abs(share['expires_at'] - original_expire) < 1

    # 再只改备注
    resp2 = client.patch(f'/api/share/{sid}', json={'note': '新备注'},
                         headers={'Authorization': f'Bearer {auth_token}'})
    share2 = resp2.get_json()['share']
    assert share2['note'] == '新备注'
    assert share2['active'] is False
    assert share2['max_downloads'] == 5


def test_patch_share_clear_note(client, auth_token):
    fid = _upload(client, 'clearnote.txt')
    created = client.post('/api/share', json={'file_id': fid, 'note': 'x'},
                          headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    resp = client.patch(f"/api/share/{created['share_id']}", json={'note': ''},
                        headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.get_json()['share']['note'] is None


def test_patch_share_conflict_max_below_used(client, auth_token):
    """取件上限低于已取件次数属于策略冲突，可重试"""
    fid = _upload(client, 'used.txt', b'x')
    created = client.post('/api/share',
                          json={'file_id': fid, 'max_downloads': 5},
                          headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    sid = created['share_id']
    for _ in range(3):
        client.get(f'/api/share/{sid}/download')

    # 已取件 3 次，再把上限压到 2：策略冲突，原记录不被覆盖
    resp = client.patch(f'/api/share/{sid}', json={'max_downloads': 2},
                        headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body['error_code'] == 'POLICY_CONFLICT'
    assert body['retryable'] is True
    assert body['current_download_count'] == 3

    info = client.get('/api/shares',
                      headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    current = [s for s in info if s['share_id'] == sid][0]
    assert current['max_downloads'] == 5


def test_patch_share_set_max_equal_used_is_allowed(client, auth_token):
    """上限等于已取件次数合法（链接恰好用完），不属于冲突"""
    fid = _upload(client, 'edge.txt', b'x')
    created = client.post('/api/share',
                          json={'file_id': fid, 'max_downloads': 5},
                          headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    sid = created['share_id']
    client.get(f'/api/share/{sid}/download')

    resp = client.patch(f'/api/share/{sid}', json={'max_downloads': 1},
                        headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.status_code == 200
    share = resp.get_json()['share']
    assert share['max_downloads'] == 1
    assert share['is_valid'] is False
    assert '用完' in share['error_msg']


def test_patch_share_unknown_field(client, auth_token):
    fid = _upload(client, 'unknownfield.txt')
    created = client.post('/api/share', json={'file_id': fid},
                          headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    resp = client.patch(f"/api/share/{created['share_id']}",
                        json={'created_by': 'hacker'},
                        headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.status_code == 400


def test_patch_share_not_found(client, auth_token):
    resp = client.patch('/api/share/nope', json={'active': False},
                        headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.status_code == 404


def test_patch_share_other_user_forbidden(client, auth_token, db_conn):
    fid = _upload(client, 'otherowner.txt')
    cursor = db_conn.cursor()
    cursor.execute(
        'INSERT INTO share_links (id, file_id, created_by, expires_at, max_downloads, active)'
        ' VALUES (?, ?, ?, ?, ?, 1)',
        ('patch-other', fid, 'someoneelse', None, 10)
    )
    db_conn.commit()
    resp = client.patch('/api/share/patch-other', json={'active': False},
                        headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 启停状态生命周期
# ---------------------------------------------------------------------------

def test_disabled_share_cannot_download(client, auth_token):
    fid = _upload(client, 'disabled.txt', b'locked')
    created = client.post('/api/share', json={'file_id': fid},
                          headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    sid = created['share_id']

    client.patch(f'/api/share/{sid}', json={'active': False},
                 headers={'Authorization': f'Bearer {auth_token}'})

    resp = client.get(f'/api/share/{sid}/download')
    assert resp.status_code == 404
    assert '停用' in resp.get_json()['error']

    info = client.get(f'/api/share/{sid}').get_json()
    assert info['is_valid'] is False
    assert '停用' in info['error_msg']

    # 重新启用后恢复可用
    client.patch(f'/api/share/{sid}', json={'active': True},
                 headers={'Authorization': f'Bearer {auth_token}'})
    resp2 = client.get(f'/api/share/{sid}/download')
    assert resp2.status_code == 200


def test_batch_creates_active_by_default(client, auth_token):
    fid = _upload(client, 'defaultactive.txt')
    data = _batch(client, auth_token,
                  [{'file_id': fid, 'client_ref': 'c1'}]).get_json()
    sid = data['results'][0]['share_id']
    info = client.get(f'/api/share/{sid}').get_json()
    assert info['is_valid'] is True


# ---------------------------------------------------------------------------
# 既有单条规则回归
# ---------------------------------------------------------------------------

def test_single_share_keeps_original_rules(client, auth_token):
    fid = _upload(client, 'legacy.txt')
    # 不传任何策略：默认 24 小时 / 10 次（由配置决定）
    resp = client.post('/api/share', json={'file_id': fid},
                       headers={'Authorization': f'Bearer {auth_token}'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['max_downloads'] == 10
    assert body['expires_at'] is not None
    assert body['active'] is True

    # -1 永久/无限 仍按旧语义
    resp2 = client.post('/api/share',
                        json={'file_id': fid, 'expire_hours': -1, 'max_downloads': -1},
                        headers={'Authorization': f'Bearer {auth_token}'})
    body2 = resp2.get_json()
    assert body2['expires_at'] is None
    assert body2['max_downloads'] is None


def test_shares_list_includes_note_active_batch_id(client, auth_token):
    fid = _upload(client, 'listfields.txt')
    _batch(client, auth_token, [{'file_id': fid, 'client_ref': 'c1', 'note': 'N'}],
           batch_id='list-1')
    shares = client.get('/api/shares',
                        headers={'Authorization': f'Bearer {auth_token}'}).get_json()
    mine = [s for s in shares if s['batch_id'] == 'list-1']
    assert len(mine) == 1
    assert set(['note', 'active', 'batch_id']).issubset(mine[0].keys())
