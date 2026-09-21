"""文件模块测试"""
import io
import time


def test_upload_file(client):
    """测试文件上传"""
    data = {
        'file': (io.BytesIO(b'test content'), 'test.txt')
    }
    response = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert response.status_code == 200
    result = response.get_json()
    assert result['success'] is True
    assert 'file_id' in result


def test_upload_no_file(client):
    """测试无文件上传"""
    response = client.post('/api/upload', data={}, content_type='multipart/form-data')
    assert response.status_code == 400


def test_upload_invalid_extension(client):
    """测试不允许的文件类型"""
    data = {
        'file': (io.BytesIO(b'test'), 'test.exe')
    }
    response = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert response.status_code == 400


def test_list_files(client):
    """测试文件列表"""
    response = client.get('/api/files')
    assert response.status_code == 200
    assert isinstance(response.get_json(), list)


def test_download_without_token(client):
    """测试无 token 下载"""
    response = client.get('/api/download/some-id')
    assert response.status_code == 401


def test_download_file_not_found(client, auth_token):
    """测试下载不存在的文件"""
    response = client.get(f'/api/download/nonexistent-id?token={auth_token}')
    assert response.status_code == 404


def test_upload_and_download(client, auth_token):
    """测试上传后下载"""
    # 上传
    data = {
        'file': (io.BytesIO(b'hello world'), 'hello.txt')
    }
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    # 下载
    download_resp = client.get(f'/api/download/{file_id}?token={auth_token}')
    assert download_resp.status_code == 200
    assert download_resp.data == b'hello world'


def test_create_share_without_auth(client):
    """测试未授权创建分享链接"""
    response = client.post('/api/share', json={'file_id': 'test'})
    assert response.status_code == 401


def test_create_share_invalid_file(client, auth_token):
    """测试为不存在的文件创建分享链接"""
    response = client.post(
        '/api/share',
        json={'file_id': 'nonexistent'},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert response.status_code == 404


def test_create_share_success(client, auth_token):
    """测试创建分享链接成功"""
    data = {'file': (io.BytesIO(b'test content'), 'test_share.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    response = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': 24, 'max_downloads': 5},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert response.status_code == 200
    result = response.get_json()
    assert result['success'] is True
    assert 'share_id' in result
    assert result['max_downloads'] == 5
    assert result['filename'] == 'test_share.txt'


def test_create_share_default_values(client, auth_token):
    """测试使用默认值创建分享链接"""
    data = {'file': (io.BytesIO(b'test content'), 'test_default.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    response = client.post(
        '/api/share',
        json={'file_id': file_id},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert response.status_code == 200
    result = response.get_json()
    assert result['success'] is True
    assert result['max_downloads'] == 10


def test_get_share_info(client, auth_token):
    """测试获取分享链接信息"""
    data = {'file': (io.BytesIO(b'test content'), 'test_get.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    create_resp = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': 24, 'max_downloads': 5},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    share_id = create_resp.get_json()['share_id']

    response = client.get(f'/api/share/{share_id}')
    assert response.status_code == 200
    result = response.get_json()
    assert result['share_id'] == share_id
    assert result['filename'] == 'test_get.txt'
    assert result['is_valid'] is True
    assert result['download_count'] == 0


def test_get_nonexistent_share(client):
    """测试获取不存在的分享链接"""
    response = client.get('/api/share/nonexistent')
    assert response.status_code == 404


def test_download_by_share_success(client, auth_token):
    """测试通过分享链接下载文件成功"""
    data = {'file': (io.BytesIO(b'share download test'), 'test_share_dl.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    create_resp = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': 24, 'max_downloads': 5},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    share_id = create_resp.get_json()['share_id']

    download_resp = client.get(f'/api/share/{share_id}/download')
    assert download_resp.status_code == 200
    assert download_resp.data == b'share download test'

    info_resp = client.get(f'/api/share/{share_id}')
    assert info_resp.get_json()['download_count'] == 1


def test_download_by_share_exceed_max(client, auth_token):
    """测试超过下载次数限制"""
    data = {'file': (io.BytesIO(b'limited content'), 'test_limited.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    create_resp = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': 24, 'max_downloads': 1},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    share_id = create_resp.get_json()['share_id']

    download_resp1 = client.get(f'/api/share/{share_id}/download')
    assert download_resp1.status_code == 200

    download_resp2 = client.get(f'/api/share/{share_id}/download')
    assert download_resp2.status_code == 404
    assert '下载次数已用完' in download_resp2.get_json()['error']


def test_download_expired_share(client, auth_token, db_conn):
    """测试下载已过期的分享链接"""
    data = {'file': (io.BytesIO(b'expired content'), 'test_expired.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    create_resp = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': 1, 'max_downloads': 5},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    share_id = create_resp.get_json()['share_id']

    cursor = db_conn.cursor()
    cursor.execute(
        'UPDATE share_links SET expires_at = ? WHERE id = ?',
        (time.time() - 3600, share_id)
    )
    db_conn.commit()

    download_resp = client.get(f'/api/share/{share_id}/download')
    assert download_resp.status_code == 404
    assert '已过期' in download_resp.get_json()['error']


def test_create_share_unlimited(client, auth_token):
    """测试创建无限制的分享链接"""
    data = {'file': (io.BytesIO(b'unlimited content'), 'test_unlimited.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    create_resp = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': -1, 'max_downloads': -1},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    result = create_resp.get_json()
    assert result['expires_at'] is None
    assert result['max_downloads'] is None

    for i in range(3):
        download_resp = client.get(f'/api/share/{result["share_id"]}/download')
        assert download_resp.status_code == 200

    info_resp = client.get(f'/api/share/{result["share_id"]}')
    assert info_resp.get_json()['download_count'] == 3
    assert info_resp.get_json()['is_valid'] is True


def test_list_shares(client, auth_token):
    """测试获取用户的分享列表"""
    for i in range(2):
        data = {'file': (io.BytesIO(f'content {i}'.encode()), f'test_list_{i}.txt')}
        upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
        file_id = upload_resp.get_json()['file_id']

        client.post(
            '/api/share',
            json={'file_id': file_id},
            headers={'Authorization': f'Bearer {auth_token}'}
        )

    response = client.get(
        '/api/shares',
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert response.status_code == 200
    shares = response.get_json()
    assert len(shares) >= 2
    assert 'filename' in shares[0]
    assert 'is_valid' in shares[0]


def test_list_shares_without_auth(client):
    """测试未授权获取分享列表"""
    response = client.get('/api/shares')
    assert response.status_code == 401


def test_delete_share_success(client, auth_token):
    """测试删除分享链接成功"""
    data = {'file': (io.BytesIO(b'to delete'), 'test_delete.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    create_resp = client.post(
        '/api/share',
        json={'file_id': file_id},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    share_id = create_resp.get_json()['share_id']

    delete_resp = client.delete(
        f'/api/share/{share_id}',
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert delete_resp.status_code == 200
    assert delete_resp.get_json()['success'] is True

    get_resp = client.get(f'/api/share/{share_id}')
    assert get_resp.status_code == 404


def test_delete_share_unauthorized(client, auth_token, db_conn):
    """测试删除他人的分享链接"""
    data = {'file': (io.BytesIO(b'other content'), 'test_other.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    cursor = db_conn.cursor()
    cursor.execute(
        'INSERT INTO share_links (id, file_id, created_by, expires_at, max_downloads) VALUES (?, ?, ?, ?, ?)',
        ('testshare123', file_id, 'otheruser', None, 10)
    )
    db_conn.commit()

    delete_resp = client.delete(
        '/api/share/testshare123',
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    assert delete_resp.status_code == 403



def upload_test_file(client, filename, content=b'test'):
    response = client.post(
        '/api/upload',
        data={'file': (io.BytesIO(content), filename)},
        content_type='multipart/form-data'
    )
    return response.get_json()['file_id']


def auth_headers(auth_token):
    return {'Authorization': f'Bearer {auth_token}'}


def test_batch_create_shares_partial_results(client, auth_token):
    """批量提交逐条返回，部分失败不影响成功项"""
    first_id = upload_test_file(client, 'batch-first.txt', b'first')
    second_id = upload_test_file(client, 'batch-second.txt', b'second')

    response = client.post(
        '/api/share/batch',
        json={
            'request_id': 'request-partial-001',
            'expire_hours': 6,
            'max_downloads': 2,
            'note': '批量策略备注',
            'is_enabled': True,
            'files': [
                {'item_id': 'first', 'file_id': first_id},
                {'item_id': 'missing', 'file_id': 'missing-file-id'},
                {'item_id': 'second', 'file_id': second_id}
            ]
        },
        headers=auth_headers(auth_token)
    )

    assert response.status_code == 200
    result = response.get_json()
    assert result['success'] is False
    assert result['status'] == 'partial'
    assert result['summary'] == {'total': 3, 'succeeded': 2, 'failed': 1}
    assert [item['item_id'] for item in result['results']] == ['first', 'missing', 'second']

    failed = result['results'][1]
    assert failed['success'] is False
    assert failed['error_code'] == 'FILE_NOT_FOUND'
    assert failed['retryable'] is True

    success = result['results'][0]
    assert success['share_id']
    share_info = client.get(f"/api/share/{success['share_id']}").get_json()
    assert share_info['note'] == '批量策略备注'
    assert share_info['is_enabled'] is True
    assert share_info['max_downloads'] == 2
    assert share_info['batch_id'] == result['batch_id']
    assert share_info['batch_item_id'] == 'first'
    assert share_info['request_id'] == 'request-partial-001'


def test_batch_empty_selection_rejected(client, auth_token):
    response = client.post(
        '/api/share/batch',
        json={'request_id': 'request-empty-0001', 'files': []},
        headers=auth_headers(auth_token)
    )
    assert response.status_code == 400
    assert '至少选择一个文件' in response.get_json()['error']


def test_batch_invalid_policy_rejected_without_creating_records(client, auth_token):
    file_id = upload_test_file(client, 'bad-policy.txt')
    response = client.post(
        '/api/share/batch',
        json={
            'request_id': 'request-policy-0001',
            'expire_hours': 0,
            'max_downloads': 0,
            'files': [{'file_id': file_id}]
        },
        headers=auth_headers(auth_token)
    )

    assert response.status_code == 400
    shares = client.get('/api/shares', headers=auth_headers(auth_token)).get_json()
    assert all(share['file_id'] != file_id for share in shares)


def test_batch_duplicate_request_returns_same_result(client, auth_token):
    file_id = upload_test_file(client, 'duplicate-request.txt')
    payload = {
        'request_id': 'request-duplicate-01',
        'files': [{'item_id': 'same', 'file_id': file_id}]
    }

    first = client.post('/api/share/batch', json=payload, headers=auth_headers(auth_token))
    second = client.post('/api/share/batch', json=payload, headers=auth_headers(auth_token))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json() == second.get_json()
    shares = client.get('/api/shares', headers=auth_headers(auth_token)).get_json()
    assert sum(share['file_id'] == file_id for share in shares) == 1


def test_batch_conflict_then_retry_success(client, auth_token):
    file_id = upload_test_file(client, 'conflict-file.txt', b'conflict')
    single_share = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': 1, 'max_downloads': 1},
        headers=auth_headers(auth_token)
    ).get_json()['share_id']

    batch_response = client.post(
        '/api/share/batch',
        json={
            'request_id': 'request-conflict-01',
            'files': [{'item_id': 'conflict', 'file_id': file_id}]
        },
        headers=auth_headers(auth_token)
    )
    batch = batch_response.get_json()
    assert batch['summary']['failed'] == 1
    assert batch['results'][0]['error_code'] == 'SHARE_CONFLICT'
    assert batch['results'][0]['retryable'] is True

    client.delete(f'/api/share/{single_share}', headers=auth_headers(auth_token))
    retry_response = client.post(
        f"/api/share/batch/{batch['batch_id']}/retry",
        json={
            'request_id': 'request-conflict-02',
            'files': [{'item_id': 'conflict', 'file_id': file_id}]
        },
        headers=auth_headers(auth_token)
    )

    assert retry_response.status_code == 200
    retry_result = retry_response.get_json()
    assert retry_result['summary'] == {'total': 1, 'succeeded': 1, 'failed': 0}
    assert retry_result['results'][0]['success'] is True


def test_batch_result_can_be_fetched_after_refresh(client, auth_token):
    file_id = upload_test_file(client, 'refresh-result.txt')
    create_result = client.post(
        '/api/share/batch',
        json={'request_id': 'request-refresh-001', 'files': [file_id]},
        headers=auth_headers(auth_token)
    ).get_json()

    refreshed = client.get(
        f"/api/share/batch/{create_result['batch_id']}",
        headers=auth_headers(auth_token)
    )

    assert refreshed.status_code == 200
    assert refreshed.get_json() == create_result


def test_batch_disabled_share_cannot_be_downloaded(client, auth_token):
    file_id = upload_test_file(client, 'disabled-share.txt', b'disabled')
    result = client.post(
        '/api/share/batch',
        json={
            'request_id': 'request-disabled-01',
            'is_enabled': False,
            'files': [{'item_id': 'disabled', 'file_id': file_id}]
        },
        headers=auth_headers(auth_token)
    ).get_json()
    share_id = result['results'][0]['share_id']

    response = client.get(f'/api/share/{share_id}/download')
    assert response.status_code == 404
    assert '已停用' in response.get_json()['error']


def test_update_single_share_policy_does_not_overwrite_other_fields(client, auth_token):
    file_id = upload_test_file(client, 'patch-policy.txt', b'patch')
    share_id = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': 12, 'max_downloads': 7},
        headers=auth_headers(auth_token)
    ).get_json()['share_id']

    before = client.get(f'/api/share/{share_id}').get_json()
    response = client.patch(
        f'/api/share/{share_id}',
        json={'note': '只修改备注', 'is_enabled': False},
        headers=auth_headers(auth_token)
    )

    assert response.status_code == 200
    after = response.get_json()
    assert after['note'] == '只修改备注'
    assert after['is_enabled'] is False
    assert after['max_downloads'] == 7
    assert after['expires_at'] == before['expires_at']


def test_single_share_remains_original_default_policy(client, auth_token):
    file_id = upload_test_file(client, 'single-original.txt')
    result = client.post(
        '/api/share',
        json={
            'file_id': file_id,
            'note': '单条接口不应写入该备注',
            'is_enabled': False
        },
        headers=auth_headers(auth_token)
    ).get_json()

    info = client.get(f"/api/share/{result['share_id']}").get_json()
    assert info['note'] == ''
    assert info['is_enabled'] is True
    assert info['batch_id'] is None


def test_download_by_share_no_auth_needed(client, auth_token):
    """测试访客无需登录即可通过分享链接下载"""
    data = {'file': (io.BytesIO(b'public content'), 'test_public.txt')}
    upload_resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    file_id = upload_resp.get_json()['file_id']

    create_resp = client.post(
        '/api/share',
        json={'file_id': file_id, 'expire_hours': 24, 'max_downloads': 5},
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    share_id = create_resp.get_json()['share_id']

    download_resp = client.get(f'/api/share/{share_id}/download')
    assert download_resp.status_code == 200
    assert download_resp.data == b'public content'
