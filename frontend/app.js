// 从配置文件获取API地址
const API_BASE = CONFIG.API_BASE;

let currentShareFileId = null;
let currentShareLink = null;
let fileCache = new Map();
let selectedFileIds = new Set();
let batchState = createInitialBatchState();
let batchSubmitting = false;
let batchRequestSeq = 0;

function createInitialBatchState() {
    return {
        mode: 'create',
        batchId: null,
        requestId: null,
        policy: null,
        selected: [],
        summary: { total: 0, succeeded: 0, failed: 0 },
        results: []
    };
}

function makeRequestId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function makeBatchStorageKey(batchId) {
    return `share_batch_state_${batchId}`;
}

function persistBatchState() {
    if (!batchState.batchId) return;
    try {
        sessionStorage.setItem(
            makeBatchStorageKey(batchState.batchId),
            JSON.stringify(batchState)
        );
    } catch {
        // 隐私模式或存储满时仍可依赖后端 batch_id 查询
    }
}

function readPersistedBatchState(batchId) {
    try {
        const raw = sessionStorage.getItem(makeBatchStorageKey(batchId));
        return raw ? JSON.parse(raw) : null;
    } catch {
        return null;
    }
}

function clearPersistedBatchState(batchId) {
    try {
        sessionStorage.removeItem(makeBatchStorageKey(batchId));
    } catch {
        // ignore storage failures
    }
}

// Token 管理
const TokenManager = {
    TOKEN_KEY: 'auth_token',
    USER_KEY: 'auth_user',
    
    save(token, username) {
        localStorage.setItem(this.TOKEN_KEY, token);
        localStorage.setItem(this.USER_KEY, username);
    },
    
    get() {
        return localStorage.getItem(this.TOKEN_KEY);
    },
    
    getUser() {
        return localStorage.getItem(this.USER_KEY);
    },
    
    clear() {
        localStorage.removeItem(this.TOKEN_KEY);
        localStorage.removeItem(this.USER_KEY);
    },
    
    async isValid() {
        const token = this.get();
        if (!token) return false;
        
        try {
            const response = await fetch(`${API_BASE}/refresh-token?token=${token}`, {
                method: 'POST'
            });
            return response.ok;
        } catch {
            return false;
        }
    }
};

// 更新用户状态栏
async function updateUserBar() {
    const userBar = document.getElementById('userBar');
    const currentUser = document.getElementById('currentUser');
    const userAvatar = document.getElementById('userAvatar');
    const user = TokenManager.getUser();
    
    if (user && TokenManager.get() && await TokenManager.isValid()) {
        currentUser.textContent = user;
        userAvatar.textContent = user.charAt(0).toUpperCase();
        userBar.classList.remove('hidden');
        loadMyShares();
    } else {
        userBar.classList.add('hidden');
        const shareSection = document.getElementById('mySharesSection');
        if (shareSection) {
            shareSection.style.display = 'none';
        }
    }
}

// 退出登录
function logout() {
    TokenManager.clear();
    selectedFileIds.clear();
    updateBatchSelectionUI();
    updateUserBar().then(loadFileList);
}

// 页面加载时获取文件列表和更新用户状态
document.addEventListener('DOMContentLoaded', async () => {
    // 检查token是否有效，无效则清除
    if (TokenManager.get() && !(await TokenManager.isValid())) {
        TokenManager.clear();
    }
    await updateUserBar();
    await loadFileList();
    await restoreBatchFromHash();
});

// 验证文件
function validateFile(file) {
    if (file.size > CONFIG.MAX_FILE_SIZE) {
        return `文件大小超过限制（最大${CONFIG.MAX_FILE_SIZE / 1024 / 1024}MB）`;
    }
    return null;
}

// 上传文件处理函数
async function uploadFile(file) {
    const validationError = validateFile(file);
    if (validationError) {
        document.getElementById('uploadStatus').textContent = `❌ ${validationError}`;
        return;
    }

    showLoading('上传中...');
    
    const formData = new FormData();
    formData.append('file', file);

    try {
        const response = await fetch(`${API_BASE}/upload`, {
            method: 'POST',
            body: formData
        });
        const result = await response.json();
        
        if (response.ok) {
            document.getElementById('uploadStatus').textContent = `✅ ${file.name} 上传成功！`;
            loadFileList();
        } else {
            document.getElementById('uploadStatus').textContent = `❌ 上传失败: ${result.error}`;
        }
    } catch (error) {
        document.getElementById('uploadStatus').textContent = `❌ 上传失败: ${error.message}`;
    } finally {
        hideLoading();
    }
}

// 文件选择上传
document.getElementById('fileInput').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    await uploadFile(file);
    e.target.value = '';
});

// 拖拽上传
const uploadZone = document.querySelector('.upload-zone');

uploadZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadZone.classList.add('drag-over');
});

uploadZone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    uploadZone.classList.remove('drag-over');
});

uploadZone.addEventListener('drop', async (e) => {
    e.preventDefault();
    uploadZone.classList.remove('drag-over');
    
    const file = e.dataTransfer.files[0];
    if (file) {
        await uploadFile(file);
    }
});

// 加载文件列表
async function loadFileList() {
    showLoading('加载文件列表...');
    
    try {
        const response = await fetch(`${API_BASE}/files`);
        const files = await response.json();
        
        const fileList = document.getElementById('fileList');
        const isLoggedIn = TokenManager.get() && (await TokenManager.isValid());
        
        if (files.length === 0) {
            fileList.innerHTML = '<p class="empty-msg">暂无可下载文件</p>';
        } else {
            fileCache = new Map(files.map(file => [file.id, file]));
            fileList.innerHTML = files.map(file => {
                const checked = selectedFileIds.has(file.id) ? 'checked' : '';
                return `
                <div class="file-item ${checked ? 'selected' : ''}" data-file-id="${escapeHtml(file.id)}">
                    <div class="file-info">
                        ${isLoggedIn ? `<label class="file-select-control" title="加入批量分享">
                            <input type="checkbox" class="fileSelectCheckbox" value="${escapeHtml(file.id)}" ${checked}>
                        </label>` : ''}
                        <div class="file-icon">${getFileIcon(file.name)}</div>
                        <div class="file-details">
                            <div class="file-name">${escapeHtml(file.name)}</div>
                            <div class="file-size">${formatSize(file.size)}</div>
                        </div>
                    </div>
                    <div class="file-actions">
                        ${isLoggedIn ? `<button class="share-btn" onclick="openShareModal('${escapeJs(file.id)}', '${escapeJs(file.name)}')">分享</button>` : ''}
                        <button class="download-btn" onclick="requestDownload('${escapeJs(file.id)}')">
                            下载
                        </button>
                    </div>
                </div>
            `;
            }).join('');
            updateBatchSelectionUI(files);
        }
    } catch (error) {
        document.getElementById('fileList').innerHTML = 
            `<p class="empty-msg">加载失败: ${escapeHtml(error.message)}</p>`;
    } finally {
        hideLoading();
    }
}

// HTML转义防止XSS
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}

// 安全生成 HTML 属性中的 JavaScript 字符串
function escapeJs(text) {
    return JSON.stringify(text == null ? '' : String(text)).slice(1, -1)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// 请求下载 - 检查token是否有效，有效则直接下载
async function requestDownload(fileId) {
    showLoading('检查授权...');
    
    // 检查是否有有效的token
    if (await TokenManager.isValid()) {
        // token有效，使用 fetch + Authorization 头下载
        document.getElementById('loadingText').textContent = '正在下载...';
        try {
            const response = await fetch(`${API_BASE}/download/${fileId}`, {
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${TokenManager.get()}`
                }
            });
            if (response.ok) {
                const blob = await response.blob();
                const contentDisposition = response.headers.get('Content-Disposition');
                let filename = 'download';
                if (contentDisposition) {
                    const match = contentDisposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';\n]+)/i);
                    if (match) filename = decodeURIComponent(match[1]);
                }
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
                a.remove();
            } else {
                const result = await response.json();
                alert(`下载失败: ${result.error || '未知错误'}`);
            }
        } catch (error) {
            alert(`下载失败: ${error.message}`);
        } finally {
            hideLoading();
        }
        return;
    }
    
    // token无效或不存在，弹出登录框
    hideLoading();
    TokenManager.clear();
    document.getElementById('downloadFileId').value = fileId;
    document.getElementById('authModal').classList.add('active');
    document.getElementById('authError').textContent = '';
    document.getElementById('username').value = '';
    document.getElementById('password').value = '';
    document.getElementById('username').focus();
}

// 关闭验证弹窗
function closeAuthModal() {
    document.getElementById('authModal').classList.remove('active');
}

// 身份验证表单提交
document.getElementById('authForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    const fileId = document.getElementById('downloadFileId').value;

    if (!username || !password) {
        document.getElementById('authError').textContent = '请输入用户名和密码';
        return;
    }

    showLoading('验证身份...');
    
    try {
        const response = await fetch(`${API_BASE}/auth`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        
        const result = await response.json();
        
        if (response.ok && result.success) {
            // 保存token和用户名到本地
            TokenManager.save(result.token, username);
            await updateUserBar();
            loadFileList();
            
            closeAuthModal();
            document.getElementById('loadingText').textContent = '验证成功，正在下载...';
            
            // 使用 fetch + Authorization 头下载
            try {
                const downloadResponse = await fetch(`${API_BASE}/download/${fileId}`, {
                    method: 'GET',
                    headers: {
                        'Authorization': `Bearer ${result.token}`
                    }
                });
                if (downloadResponse.ok) {
                    const blob = await downloadResponse.blob();
                    const contentDisposition = downloadResponse.headers.get('Content-Disposition');
                    let filename = 'download';
                    if (contentDisposition) {
                        const match = contentDisposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';\n]+)/i);
                        if (match) filename = decodeURIComponent(match[1]);
                    }
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = filename;
                    document.body.appendChild(a);
                    a.click();
                    window.URL.revokeObjectURL(url);
                    a.remove();
                } else {
                    const errResult = await downloadResponse.json();
                    document.getElementById('authError').textContent = `下载失败: ${errResult.error || '未知错误'}`;
                }
            } catch (downloadError) {
                document.getElementById('authError').textContent = `下载失败: ${downloadError.message}`;
            } finally {
                hideLoading();
            }
        } else if (response.status === 429) {
            hideLoading();
            document.getElementById('authError').textContent = '请求过于频繁，请稍后再试';
        } else {
            hideLoading();
            document.getElementById('authError').textContent = result.error || '验证失败，请检查账号密码';
        }
    } catch (error) {
        hideLoading();
        document.getElementById('authError').textContent = `验证失败: ${error.message}`;
    }
});

// 显示加载动画
function showLoading(text = '加载中...') {
    document.getElementById('loadingText').textContent = text;
    document.getElementById('loadingOverlay').classList.add('active');
}

// 隐藏加载动画
function hideLoading() {
    document.getElementById('loadingOverlay').classList.remove('active');
}

// 获取文件图标
function getFileIcon(filename) {
    const ext = filename.split('.').pop().toLowerCase();
    const icons = {
        pdf: '📄', doc: '📝', docx: '📝', txt: '📃',
        jpg: '🖼️', jpeg: '🖼️', png: '🖼️', gif: '🖼️',
        mp3: '🎵', wav: '🎵', mp4: '🎬', avi: '🎬',
        zip: '📦', rar: '📦', '7z': '📦',
        js: '💻', py: '🐍', html: '🌐', css: '🎨'
    };
    return icons[ext] || '📁';
}

// 格式化文件大小
function formatSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// 格式化时间戳
function formatTimestamp(timestamp) {
    if (!timestamp) return '永久有效';
    const date = new Date(timestamp * 1000);
    return date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
    });
}

// 格式化剩余时间
function formatRemainingTime(expiresAt) {
    if (!expiresAt) return '永久';
    const remaining = expiresAt - (Date.now() / 1000);
    if (remaining <= 0) return '已过期';
    
    const hours = Math.floor(remaining / 3600);
    const minutes = Math.floor((remaining % 3600) / 60);
    
    if (hours > 24) {
        const days = Math.floor(hours / 24);
        return `${days} 天 ${hours % 24} 小时`;
    } else if (hours > 0) {
        return `${hours} 小时 ${minutes} 分钟`;
    } else {
        return `${minutes} 分钟`;
    }
}

// 批量分享选择和策略配置
function updateBatchSelectionUI(files = []) {
    const batchBtn = document.getElementById('batchShareBtn');
    const bar = document.getElementById('batchSelectBar');
    const countEl = document.getElementById('selectedFileCount');
    const selectAll = document.getElementById('selectAllFiles');
    const currentFiles = files.length ? files : Array.from(fileCache.values());
    const visibleIds = currentFiles.map(file => file.id);
    selectedFileIds = new Set(Array.from(selectedFileIds).filter(id => fileCache.has(id)));

    const count = selectedFileIds.size;
    if (countEl) countEl.textContent = count;
    if (batchBtn) {
        batchBtn.disabled = count === 0;
        batchBtn.classList.toggle('has-selection', count > 0);
    }
    if (bar) bar.classList.toggle('hidden', visibleIds.length === 0);
    if (selectAll) {
        selectAll.checked = visibleIds.length > 0 && visibleIds.every(id => selectedFileIds.has(id));
        selectAll.indeterminate = count > 0 && !selectAll.checked;
    }
}

function toggleFileSelection(fileId, checked) {
    if (checked) {
        selectedFileIds.add(fileId);
    } else {
        selectedFileIds.delete(fileId);
    }
    const row = document.querySelector(`.file-item[data-file-id="${CSS.escape(fileId)}"]`);
    if (row) row.classList.toggle('selected', checked);
    updateBatchSelectionUI();
}

document.getElementById('fileList').addEventListener('change', event => {
    const checkbox = event.target.closest('.fileSelectCheckbox');
    if (!checkbox) return;
    toggleFileSelection(checkbox.value, checkbox.checked);
});

document.getElementById('selectAllFiles').addEventListener('change', event => {
    document.querySelectorAll('.fileSelectCheckbox').forEach(checkbox => {
        checkbox.checked = event.target.checked;
        toggleFileSelection(checkbox.value, event.target.checked);
    });
});

document.getElementById('batchShareBtn').addEventListener('click', () => {
    if (selectedFileIds.size === 0) {
        alert('请先勾选要分享的文件');
        return;
    }
    openBatchShareModal();
});

document.getElementById('batchNote').addEventListener('input', event => {
    document.getElementById('batchNoteCount').textContent = event.target.value.length;
});

function getSelectedFiles() {
    return Array.from(selectedFileIds)
        .map(id => fileCache.get(id))
        .filter(Boolean);
}

function readBatchPolicyFromForm() {
    return {
        expire_hours: Number(document.getElementById('batchExpireHours').value),
        max_downloads: Number(document.getElementById('batchMaxDownloads').value),
        note: document.getElementById('batchNote').value.trim(),
        is_enabled: document.getElementById('batchEnabled').checked
    };
}

function fillBatchPolicyForm(policy) {
    const expireHours = policy.expire_hours === null || policy.expire_hours === undefined ? -1 : policy.expire_hours;
    const maxDownloads = policy.max_downloads === null || policy.max_downloads === undefined ? -1 : policy.max_downloads;
    document.getElementById('batchExpireHours').value = String(expireHours);
    document.getElementById('batchMaxDownloads').value = String(maxDownloads);
    document.getElementById('batchNote').value = policy.note || '';
    document.getElementById('batchNoteCount').textContent = (policy.note || '').length;
    document.getElementById('batchEnabled').checked = policy.is_enabled !== false;
}

function openBatchShareModal(retry = false) {
    const selectedFiles = getSelectedFiles();
    if (!retry && selectedFiles.length === 0) {
        alert('请先勾选要分享的文件');
        return;
    }

    document.getElementById('batchModalTitle').textContent = retry ? '重试批量分享' : '批量创建分享链接';
    if (!retry) {
        batchState.mode = 'create';
        batchState.batchId = null;
        batchState.requestId = null;
    }
    document.getElementById('batchSelectedSummary').textContent = retry
        ? `将重试 ${selectedFileIds.size} 个可重试项，成功项保持不变`
        : `将为 ${selectedFileIds.size} 个文件应用同一条策略`;
    document.getElementById('batchPolicyError').textContent = '';
    document.getElementById('batchFilePreview').innerHTML = selectedFiles.map(file => `
        <span class="batch-file-chip">${escapeHtml(file.name)}</span>
    `).join('');

    if (!retry || !batchState.policy) {
        fillBatchPolicyForm({ expire_hours: 24, max_downloads: 10, note: '', is_enabled: true });
    }
    document.getElementById('batchSubmitBtn').disabled = false;
    document.getElementById('batchShareModal').classList.add('active');
}

function closeBatchShareModal() {
    document.getElementById('batchShareModal').classList.remove('active');
}

function setBatchSubmitting(submitting) {
    batchSubmitting = submitting;
    document.getElementById('batchSubmitBtn').disabled = submitting;
    showLoading(submitting ? '批量提交中...' : '加载中...');
    if (!submitting) hideLoading();
}

async function submitBatchShare() {
    const retry = batchState.mode === 'retry' && batchState.batchId;
    let selectedFiles;
    if (retry) {
        const retryableItems = batchState.results.filter(item => !item.success && item.retryable);
        selectedFiles = retryableItems.map(item => ({
            id: item.file_id,
            name: item.filename || fileCache.get(item.file_id)?.name || item.file_id,
            itemId: item.item_id
        }));
    } else {
        selectedFiles = getSelectedFiles();
    }

    if (selectedFiles.length === 0) {
        document.getElementById('batchPolicyError').textContent = retry ? '没有可重试的项目' : '请至少选择一个文件';
        return;
    }

    const policy = readBatchPolicyFromForm();
    if (!Number.isInteger(policy.expire_hours) || !Number.isInteger(policy.max_downloads)) {
        document.getElementById('batchPolicyError').textContent = '请选择有效的有效期和取件次数';
        return;
    }

    const seq = ++batchRequestSeq;
    const requestId = makeRequestId();
    const payload = {
        request_id: requestId,
        files: selectedFiles.map((file, index) => ({
            item_id: retry ? (file.itemId || `item-${index + 1}`) : `item-${index + 1}`,
            file_id: file.id
        }))
    };

    if (retry) {
        payload.expire_hours = policy.expire_hours;
        payload.max_downloads = policy.max_downloads;
        payload.note = policy.note;
        payload.is_enabled = policy.is_enabled;
    }

    setBatchSubmitting(true);
    document.getElementById('batchPolicyError').textContent = '';

    const url = retry
        ? `${API_BASE}/share/batch/${encodeURIComponent(batchState.batchId)}/retry`
        : `${API_BASE}/share/batch`;

    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${TokenManager.get()}`
            },
            body: JSON.stringify(payload)
        });
        const result = await response.json();

        if (seq !== batchRequestSeq) return;

        if (!response.ok) {
            document.getElementById('batchPolicyError').textContent = result.error || '批量提交失败';
            return;
        }

        const context = retry ? { requestId, mode: 'retry' } : { requestId, selectedIds: selectedFiles.map(file => file.id) };
        applyBatchResult(result, context);
        closeBatchShareModal();
        selectedFileIds.clear();
        await loadFileList();
    } catch (error) {
        if (seq === batchRequestSeq) {
            document.getElementById('batchPolicyError').textContent = `提交失败: ${error.message}`;
        }
    } finally {
        if (seq === batchRequestSeq) setBatchSubmitting(false);
    }
}

function applyBatchResult(result, context = {}) {
    batchState = {
        mode: context.mode || (batchState.mode === 'retry' ? 'retry' : 'create'),
        batchId: result.batch_id,
        requestId: context.requestId || batchState.requestId || null,
        policy: result.policy || batchState.policy,
        selected: context.selectedIds
            ? context.selectedIds.map(id => ({
                file_id: id,
                filename: fileCache.get(id)?.name || result.results.find(item => item.file_id === id)?.filename || id
            }))
            : batchState.selected,
        summary: result.summary,
        results: result.results
    };
    persistBatchState();
    if (window.location.hash !== `#batch/${result.batch_id}`) {
        history.pushState({ batchId: result.batch_id }, '', `#batch/${result.batch_id}`);
    }
    renderBatchResult(result);
    loadMyShares();
}

function renderBatchResult(result) {
    const retryable = result.results.filter(item => !item.success && item.retryable);
    document.getElementById('batchResultSummary').innerHTML = `
        <span class="result-pill success">成功 ${result.summary.succeeded}</span>
        <span class="result-pill ${result.summary.failed ? 'error' : 'success'}">失败 ${result.summary.failed}</span>
        <span class="result-pill ${retryable.length ? 'warning' : 'muted'}">可重试 ${retryable.length}</span>
    `;
    document.getElementById('batchResultTrace').textContent =
        `追踪ID：${result.batch_id}｜策略批次：${result.batch_id}`;
    document.getElementById('batchResultList').innerHTML = result.results.map(item => {
        const file = fileCache.get(item.file_id);
        const name = item.filename || file?.name || item.file_id;
        return `
            <div class="batch-result-item ${item.success ? 'ok' : 'bad'}">
                <div>
                    <strong>${escapeHtml(name)}</strong>
                    <small>${item.success ? `分享ID：${item.share_id}` : `${item.error_code}：${item.error_message}`}</small>
                </div>
                <span>${item.success ? '成功' : (item.retryable ? '失败·可重试' : '失败')}</span>
            </div>
        `;
    }).join('');
    document.getElementById('batchRetryBtn').disabled = retryable.length === 0;
    document.getElementById('batchResultModal').classList.add('active');
}

function closeBatchResultModal() {
    document.getElementById('batchResultModal').classList.remove('active');
}

document.getElementById('batchRetryBtn').addEventListener('click', () => {
    const retryable = batchState.results.filter(item => !item.success && item.retryable);
    if (!retryable.length) return;
    selectedFileIds = new Set(retryable.map(item => item.file_id).filter(Boolean));
    batchState.mode = 'retry';
    fillBatchPolicyForm(batchState.policy);
    closeBatchResultModal();
    openBatchShareModal(true);
});

async function restoreBatchFromHash() {
    const match = window.location.hash.match(/^#batch\/([^/?#]+)/);
    if (!match) return;
    const batchId = decodeURIComponent(match[1]);
    const local = readPersistedBatchState(batchId);

    if (local && Array.isArray(local.results) && local.results.length) {
        batchState = local;
        renderBatchResult({
            batch_id: batchId,
            policy: local.policy,
            summary: local.summary,
            results: local.results
        });
        return;
    }

    if (!(await TokenManager.isValid())) return;
    try {
        const response = await fetch(`${API_BASE}/share/batch/${encodeURIComponent(batchId)}`, {
            headers: { 'Authorization': `Bearer ${TokenManager.get()}` }
        });
        if (!response.ok) return;
        const result = await response.json();
        applyBatchResult(result, { mode: 'view' });
    } catch {
        // 保留页面内容，不将旧批量失败错配到新选择
    }
}

window.addEventListener('popstate', restoreBatchFromHash);

// 打开分享设置弹窗
function openShareModal(fileId, fileName) {
    currentShareFileId = fileId;
    document.getElementById('shareFileName').textContent = fileName;
    document.getElementById('shareError').textContent = '';
    document.getElementById('expireHours').value = '24';
    document.getElementById('maxDownloads').value = '10';
    document.getElementById('shareModal').classList.add('active');
}

// 关闭分享设置弹窗
function closeShareModal() {
    document.getElementById('shareModal').classList.remove('active');
    currentShareFileId = null;
}

// 确认创建分享链接
async function confirmCreateShare() {
    if (!currentShareFileId) return;
    
    const expireHours = parseInt(document.getElementById('expireHours').value);
    const maxDownloads = parseInt(document.getElementById('maxDownloads').value);
    
    showLoading('生成分享链接...');
    document.getElementById('shareError').textContent = '';
    
    try {
        const response = await fetch(`${API_BASE}/share`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${TokenManager.get()}`
            },
            body: JSON.stringify({
                file_id: currentShareFileId,
                expire_hours: expireHours,
                max_downloads: maxDownloads
            })
        });
        
        const result = await response.json();
        
        if (response.ok && result.success) {
            closeShareModal();
            showShareSuccessModal(result);
            loadMyShares();
        } else {
            document.getElementById('shareError').textContent = result.error || '生成分享链接失败';
        }
    } catch (error) {
        document.getElementById('shareError').textContent = `错误: ${error.message}`;
    } finally {
        hideLoading();
    }
}

// 显示分享成功弹窗
function showShareSuccessModal(result) {
    currentShareLink = `${window.location.origin}/share.html#${result.share_id}`;
    
    document.getElementById('shareLinkInput').value = currentShareLink;
    document.getElementById('shareInfoName').textContent = result.filename;
    document.getElementById('shareInfoExpire').textContent = formatTimestamp(result.expires_at);
    document.getElementById('shareInfoDownloads').textContent = result.max_downloads ? `${result.max_downloads} 次` : '无限制';
    document.getElementById('copyBtnText').textContent = '复制';
    
    const copyBtn = document.querySelector('.copy-btn');
    copyBtn.classList.remove('copied');
    
    document.getElementById('shareSuccessModal').classList.add('active');
}

// 关闭分享成功弹窗
function closeShareSuccessModal() {
    document.getElementById('shareSuccessModal').classList.remove('active');
    currentShareLink = null;
}

// 复制分享链接
async function copyShareLink() {
    const linkInput = document.getElementById('shareLinkInput');
    const copyBtnText = document.getElementById('copyBtnText');
    const copyBtn = document.querySelector('.copy-btn');
    
    try {
        await navigator.clipboard.writeText(linkInput.value);
        copyBtnText.textContent = '已复制';
        copyBtn.classList.add('copied');
        
        setTimeout(() => {
            copyBtnText.textContent = '复制';
            copyBtn.classList.remove('copied');
        }, 2000);
    } catch (error) {
        linkInput.select();
        document.execCommand('copy');
        copyBtnText.textContent = '已复制';
        copyBtn.classList.add('copied');
        
        setTimeout(() => {
            copyBtnText.textContent = '复制';
            copyBtn.classList.remove('copied');
        }, 2000);
    }
}

// 加载我的分享列表
async function loadMyShares() {
    const section = document.getElementById('mySharesSection');
    const list = document.getElementById('mySharesList');
    
    if (!(await TokenManager.isValid())) {
        section.style.display = 'none';
        return;
    }
    
    section.style.display = 'block';
    
    try {
        const response = await fetch(`${API_BASE}/shares`, {
            headers: {
                'Authorization': `Bearer ${TokenManager.get()}`
            }
        });
        
        const shares = await response.json();
        
        if (shares.length === 0) {
            list.innerHTML = '<p class="empty-msg">暂无分享链接</p>';
            return;
        }
        
        list.innerHTML = shares.map(share => {
            const statusClass = share.is_valid ? 'valid' : 'invalid';
            const statusText = share.is_valid ? '有效' : (share.error_msg || '无效');
            const note = share.note
                ? `<div class="share-item-note">📝 ${escapeHtml(share.note)}</div>`
                : '';
            const trace = share.batch_id
                ? `<div class="share-item-trace">批次：${escapeHtml(share.batch_id)} / 项：${escapeHtml(share.batch_item_id || '-')}</div>`
                : '<div class="share-item-trace">单条分享</div>';

            return `
                <div class="share-item">
                    <div class="share-item-header">
                        <span class="share-item-filename">${escapeHtml(share.filename)}</span>
                        <span class="share-item-status ${statusClass}">${statusText}</span>
                    </div>
                    ${note}
                    <div class="share-item-details">
                        <div class="share-item-detail">
                            <span class="share-item-detail-label">启停</span>
                            <span class="share-item-detail-value">${share.is_enabled ? '已启用' : '已停用'}</span>
                        </div>
                        <div class="share-item-detail">
                            <span class="share-item-detail-label">剩余时间</span>
                            <span class="share-item-detail-value">${formatRemainingTime(share.expires_at)}</span>
                        </div>
                        <div class="share-item-detail">
                            <span class="share-item-detail-label">已下载</span>
                            <span class="share-item-detail-value">${share.download_count} / ${share.max_downloads || '∞'}</span>
                        </div>
                        <div class="share-item-detail">
                            <span class="share-item-detail-label">创建时间</span>
                            <span class="share-item-value-small">${new Date(share.created_at).toLocaleString('zh-CN')}</span>
                        </div>
                    </div>
                    ${trace}
                    <div class="share-item-actions">
                        <button type="button" class="copy-link-btn" onclick="copyShareLinkFromList('${escapeJs(share.share_id)}')">
                            🔗 复制链接
                        </button>
                        <button type="button" class="toggle-share-btn" onclick="toggleShareEnabled('${escapeJs(share.share_id)}', ${share.is_enabled ? 'false' : 'true'})">
                            ${share.is_enabled ? '⏸️ 停用' : '▶️ 启用'}
                        </button>
                        <button type="button" class="delete-share-btn" onclick="deleteShare('${escapeJs(share.share_id)}')">
                            🗑️ 删除
                        </button>
                    </div>
                </div>
            `;
        }).join('');
    } catch (error) {
        list.innerHTML = `<p class="empty-msg">加载失败: ${escapeHtml(error.message)}</p>`;
    }
}

// 从分享列表复制链接
async function copyShareLinkFromList(shareId) {
    const link = `${window.location.origin}/share.html#${shareId}`;
    try {
        await navigator.clipboard.writeText(link);
        alert('分享链接已复制到剪贴板');
    } catch (error) {
        prompt('请手动复制链接:', link);
    }
}

// 启停分享链接
async function toggleShareEnabled(shareId, enabled) {
    const action = enabled ? '启用' : '停用';
    if (!confirm(`确定要${action}此分享链接吗？`)) return;

    showLoading(`${action}中...`);
    try {
        const response = await fetch(`${API_BASE}/share/${encodeURIComponent(shareId)}`, {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${TokenManager.get()}`
            },
            body: JSON.stringify({ is_enabled: enabled })
        });

        if (!response.ok) {
            const result = await response.json();
            alert(`${action}失败: ${result.error || '未知错误'}`);
            return;
        }
        loadMyShares();
    } catch (error) {
        alert(`${action}失败: ${error.message}`);
    } finally {
        hideLoading();
    }
}

// 删除分享链接
async function deleteShare(shareId) {
    if (!confirm('确定要删除此分享链接吗？删除后链接将立即失效。')) {
        return;
    }
    
    showLoading('删除中...');
    
    try {
        const response = await fetch(`${API_BASE}/share/${shareId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${TokenManager.get()}`
            }
        });
        
        if (response.ok) {
            loadMyShares();
        } else {
            const result = await response.json();
            alert(`删除失败: ${result.error || '未知错误'}`);
        }
    } catch (error) {
        alert(`删除失败: ${error.message}`);
    } finally {
        hideLoading();
    }
}


