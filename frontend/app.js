// 从配置文件获取API地址
const API_BASE = CONFIG.API_BASE;

let currentShareFileId = null;
let currentShareLink = null;

// ---------------------------------------------------------------------------
// 批量分享链路状态
// - selectedFiles: 文件库中勾选的文件（以 file_id 为准）
// - batchSession: 当前批次（提交后保留结果；刷新/返回页面靠 localStorage 恢复）
// - client_ref 与结果条目一一对应，杜绝按位置/数组下标错配
// ---------------------------------------------------------------------------
let selectedFiles = new Map();       // file_id -> { file_id, name, size }
let currentBatchItems = [];          // 打开批量弹窗时的快照
let currentBatchSession = null;      // 最近一次批次结果
let currentEditShareId = null;

const BATCH_SESSION_KEY = 'share_batch_session';

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
    selectedFiles.clear();
    currentBatchSession = null;
    try {
        localStorage.removeItem(BATCH_SESSION_KEY);
    } catch { /* ignore */ }
    updateBatchBar();
    updateUserBar();
}

// 页面加载时获取文件列表和更新用户状态
document.addEventListener('DOMContentLoaded', async () => {
    // 检查token是否有效，无效则清除
    if (TokenManager.get() && !(await TokenManager.isValid())) {
        TokenManager.clear();
    }
    await updateUserBar();
    loadFileList();
    // 恢复尚未关闭的批量结果（刷新/返回页面场景）
    restoreBatchSession();
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

        // 清理已失效的选择项
        for (const fid of [...selectedFiles.keys()]) {
            if (!files.some(f => f.id === fid)) {
                selectedFiles.delete(fid);
            }
        }

        if (files.length === 0) {
            fileList.innerHTML = '<p class="empty-msg">暂无可下载文件</p>';
        } else {
            fileList.innerHTML = files.map(file => {
                const checked = selectedFiles.has(file.id) ? 'checked' : '';
                return `
                <div class="file-item${checked ? ' selected' : ''}" data-file-id="${escapeHtml(file.id)}">
                    ${isLoggedIn ? `
                    <label class="file-select" title="选择后可批量设置分享策略">
                        <input type="checkbox" class="file-checkbox"
                               data-file-id="${escapeHtml(file.id)}"
                               data-file-name="${escapeHtml(file.name)}"
                               data-file-size="${file.size}" ${checked}
                               onclick="event.stopPropagation()">
                    </label>` : ''}
                    <div class="file-info">
                        <div class="file-icon">${getFileIcon(file.name)}</div>
                        <div class="file-details">
                            <div class="file-name">${escapeHtml(file.name)}</div>
                            <div class="file-size">${formatSize(file.size)}</div>
                        </div>
                    </div>
                    <div class="file-actions">
                        ${isLoggedIn ? `<button class="share-btn" onclick="openShareModal('${escapeHtml(file.id)}', '${escapeHtml(file.name)}')">分享</button>` : ''}
                        <button class="download-btn" onclick="requestDownload('${escapeHtml(file.id)}')">
                            下载
                        </button>
                    </div>
                </div>
            `;
            }).join('');

            fileList.querySelectorAll('.file-checkbox').forEach(cb => {
                cb.addEventListener('change', (e) => {
                    const fid = e.target.dataset.fileId;
                    const item = e.target.closest('.file-item');
                    if (e.target.checked) {
                        selectedFiles.set(fid, {
                            file_id: fid,
                            name: e.target.dataset.fileName,
                            size: Number(e.target.dataset.fileSize)
                        });
                        item?.classList.add('selected');
                    } else {
                        selectedFiles.delete(fid);
                        item?.classList.remove('selected');
                    }
                    updateBatchBar();
                });
            });
        }
        updateBatchBar();
    } catch (error) {
        document.getElementById('fileList').innerHTML =
            `<p class="empty-msg">加载失败: ${escapeHtml(error.message)}</p>`;
    } finally {
        hideLoading();
    }
}

// 更新批量操作按钮的计数与可用状态
function updateBatchBar() {
    const btn = document.getElementById('batchShareBtn');
    if (!btn) return;
    const isLoggedIn = !!TokenManager.get();
    btn.classList.toggle('hidden', !isLoggedIn);
    document.getElementById('selectedCount').textContent = selectedFiles.size;
    btn.disabled = selectedFiles.size === 0;
}

// 生成稳定的 client_ref：同一文件在一次批量会话中身份不变
function makeClientRef(fileId) {
    return `f_${fileId.replace(/[^a-zA-Z0-9]/g, '')}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

// HTML转义防止XSS
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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

// ===========================================================================
// 批量分享：策略配置 -> 批量提交 -> 分享记录 -> 结果反馈
// ===========================================================================

// 打开批量策略弹窗（基于当前选择生成不可变快照）
function openBatchShareModal() {
    if (selectedFiles.size === 0) return;

    currentBatchItems = [...selectedFiles.values()].map(f => ({
        ...f,
        client_ref: makeClientRef(f.file_id),
        note: ''
    }));

    document.getElementById('batchFileCount').textContent = currentBatchItems.length;
    document.getElementById('batchExpireHours').value = '24';
    document.getElementById('batchMaxDownloads').value = '10';
    document.getElementById('batchNote').value = '';
    document.getElementById('batchActive').checked = true;
    document.getElementById('batchShareError').textContent = '';
    renderBatchFileList();
    document.getElementById('batchShareModal').classList.add('active');
}

function closeBatchShareModal() {
    document.getElementById('batchShareModal').classList.remove('active');
}

// 弹窗内逐条文件 + 可覆盖备注
function renderBatchFileList() {
    const container = document.getElementById('batchFileList');
    container.innerHTML = currentBatchItems.map((item, idx) => `
        <div class="batch-file-row" data-client-ref="${escapeHtml(item.client_ref)}">
            <div class="batch-file-meta">
                <span class="batch-file-icon">${getFileIcon(item.name)}</span>
                <span class="batch-file-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
                <span class="batch-file-size">${formatSize(item.size)}</span>
            </div>
            <input type="text" class="batch-item-note" maxlength="200"
                   placeholder="逐条备注（留空使用统一备注）"
                   value="${escapeHtml(item.note || '')}"
                   data-idx="${idx}">
        </div>
    `).join('');

    container.querySelectorAll('.batch-item-note').forEach(input => {
        input.addEventListener('input', (e) => {
            currentBatchItems[Number(e.target.dataset.idx)].note = e.target.value;
        });
    });
}

// 提交批量策略
async function confirmBatchShare() {
    if (currentBatchItems.length === 0) {
        document.getElementById('batchShareError').textContent = '请先选择至少一个文件';
        return;
    }

    const batchId = makeClientRef('batch').replace(/^f_/, 'b_');
    const payload = {
        batch_id: batchId,
        policy: {
            expire_hours: parseInt(document.getElementById('batchExpireHours').value, 10),
            max_downloads: parseInt(document.getElementById('batchMaxDownloads').value, 10),
            note: document.getElementById('batchNote').value.trim(),
            active: document.getElementById('batchActive').checked
        },
        items: currentBatchItems.map(item => ({
            file_id: item.file_id,
            client_ref: item.client_ref,
            note: item.note.trim()
        }))
    };

    const submitBtn = document.getElementById('batchSubmitBtn');
    submitBtn.disabled = true;
    submitBtn.textContent = '提交中...';
    document.getElementById('batchShareError').textContent = '';

    try {
        const response = await fetch(`${API_BASE}/shares/batch`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${TokenManager.get()}`
            },
            body: JSON.stringify(payload)
        });
        const result = await response.json();

        if (!response.ok) {
            // 整批请求级错误（空选择/非法策略/重复ref），弹窗保留以便修正重试
            document.getElementById('batchShareError').textContent = result.error || '批量提交失败';
            return;
        }

        closeBatchShareModal();
        showBatchResult(result);
        loadMyShares();
    } catch (error) {
        document.getElementById('batchShareError').textContent =
            `网络错误：${error.message}。请直接点击“提交批量策略”重试（不会重复创建）。`;
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = '提交批量策略';
    }
}

// 结果反馈：逐条展示，并以 client_ref 为键保存会话（刷新/返回后精确恢复）
function showBatchResult(result) {
    currentBatchSession = result;
    persistBatchSession();

    document.getElementById('batchResultSummary').innerHTML = renderBatchSummary(result.summary, result.idempotent_rehit);
    document.getElementById('batchRetryCount').textContent = result.summary.retryable;
    document.getElementById('batchRetryBtn').style.display =
        result.summary.retryable > 0 ? 'inline-flex' : 'none';

    // 结果按提交顺序（seq / client_ref）渲染，与选择项一一对应
    const list = document.getElementById('batchResultList');
    list.innerHTML = result.results.map(renderBatchResultRow).join('');

    document.getElementById('batchResultModal').classList.add('active');
}

function renderBatchSummary(summary, idempotentRehit) {
    return `
        <div class="summary-pill summary-total">共 ${summary.total} 条</div>
        <div class="summary-pill summary-success">✅ 成功 ${summary.succeeded}</div>
        <div class="summary-pill summary-failed">❌ 失败 ${summary.failed}</div>
        <div class="summary-pill summary-retry">🔁 可重试 ${summary.retryable}</div>
        ${idempotentRehit ? '<div class="summary-pill summary-rehit">重复提交，已返回首次结果</div>' : ''}
    `;
}

function renderBatchResultRow(r) {
    const ok = r.status === 'succeeded';
    const link = ok
        ? `<a class="batch-result-link" href="${window.location.origin}/share.html#${encodeURIComponent(r.share_id)}"
              target="_blank" rel="noopener">查看分享 ↗</a>`
        : '';
    const copyBtn = ok
        ? `<button class="copy-link-btn small" onclick="copyBatchShareLink('${r.share_id}')">🔗 复制</button>`
        : '';
    const tag = ok
        ? '<span class="result-tag tag-success">成功</span>'
        : (r.retryable
            ? '<span class="result-tag tag-retry">失败 · 可重试</span>'
            : '<span class="result-tag tag-fail">失败</span>');

    return `
        <div class="batch-result-row ${ok ? 'is-ok' : 'is-fail'}" data-client-ref="${escapeHtml(r.client_ref)}">
            <div class="batch-result-main">
                <div class="batch-result-title">
                    ${tag}
                    <span class="batch-file-icon">${getFileIcon(r.filename || r.file_id || '')}</span>
                    <span class="batch-file-name">${escapeHtml(r.filename || r.file_id || '(未知文件)')}</span>
                </div>
                ${ok
                    ? `<div class="batch-result-detail">
                         有效期至 ${formatTimestamp(r.expires_at)} ·
                         取件上限 ${r.max_downloads ? r.max_downloads + ' 次' : '无限制'} ·
                         备注：${escapeHtml(r.note || '无')}
                       </div>`
                    : `<div class="batch-result-detail error-text">
                         ${escapeHtml(r.error_message || '未知错误')}
                         ${r.retryable ? '<em>（该项已保留，可直接重试）</em>' : '<em>（该项不可自动重试，请修正选择）</em>'}
                       </div>`}
            </div>
            <div class="batch-result-actions">${link}${copyBtn}</div>
        </div>
    `;
}

async function copyBatchShareLink(shareId) {
    const link = `${window.location.origin}/share.html#${shareId}`;
    try {
        await navigator.clipboard.writeText(link);
    } catch {
        prompt('请手动复制链接:', link);
    }
}

function closeBatchResultModal() {
    document.getElementById('batchResultModal').classList.remove('active');
    // 用户已主动关闭：清除恢复标记，避免下次刷新再次弹出（结果仍可在“我的分享”中追踪）
    try {
        localStorage.removeItem(BATCH_SESSION_KEY);
    } catch { /* ignore */ }
}

// 仅重试可重试项：以失败项为新选择打开策略弹窗；成功项保持不动
function retryFailedBatchItems() {
    if (!currentBatchSession) return;
    const retryables = currentBatchSession.results.filter(r => r.retryable);
    if (retryables.length === 0) return;

    // 用失败项重建选择（保留文件名快照；file_id 是权威）
    selectedFiles = new Map();
    currentBatchItems = retryables.map(r => ({
        file_id: r.file_id,
        name: r.filename || r.file_id,
        size: 0,
        client_ref: makeClientRef(r.file_id),
        note: r.note || ''
    }));

    // 关闭结果弹窗，打开策略弹窗预填上次策略
    closeBatchResultModal();
    document.getElementById('batchFileCount').textContent = currentBatchItems.length;
    document.getElementById('batchNote').value = currentBatchSession.policy.note || '';
    document.getElementById('batchActive').checked = !!currentBatchSession.policy.active;
    document.getElementById('batchExpireHours').value =
        policyHoursFromExpiresAt(currentBatchSession.policy.expires_at);
    document.getElementById('batchMaxDownloads').value =
        currentBatchSession.policy.max_downloads == null
            ? '-1'
            : String(currentBatchSession.policy.max_downloads);
    document.getElementById('batchShareError').textContent = '';
    renderBatchFileList();
    document.getElementById('batchShareModal').classList.add('active');
}

// 把结果中的绝对过期时间近似还原为下拉框选项
function policyHoursFromExpiresAt(expiresAt) {
    if (!expiresAt) return '-1';
    const hours = Math.round((expiresAt - Date.now() / 1000) / 3600);
    const options = [1, 6, 24, 72, 168];
    const nearest = options.reduce((prev, cur) =>
        Math.abs(cur - hours) < Math.abs(prev - hours) ? cur : prev
    );
    return String(nearest);
}

// ---- 批次会话持久化：刷新/返回页面后回查服务端，结果不丢失、不错配 ----
function persistBatchSession() {
    if (!currentBatchSession) return;
    try {
        localStorage.setItem(BATCH_SESSION_KEY, JSON.stringify({
            batch_id: currentBatchSession.batch_id,
            saved_at: Date.now()
        }));
    } catch { /* 隐私模式下忽略 */ }
}

async function restoreBatchSession() {
    let saved;
    try {
        saved = JSON.parse(localStorage.getItem(BATCH_SESSION_KEY) || 'null');
    } catch {
        saved = null;
    }
    if (!saved || !saved.batch_id) return;
    if (!(await TokenManager.isValid())) return;

    try {
        const response = await fetch(`${API_BASE}/shares/batch/${encodeURIComponent(saved.batch_id)}`, {
            headers: { 'Authorization': `Bearer ${TokenManager.get()}` }
        });
        if (!response.ok) {
            // 批次不属于本用户或已不存在：清理，避免错配到别人的结果
            localStorage.removeItem(BATCH_SESSION_KEY);
            return;
        }
        const result = await response.json();
        currentBatchSession = result;
        showBatchResult(result);
    } catch {
        // 网络问题保持静默，不阻塞页面
    }
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

// ===========================================================================
// 分享记录生命周期：快捷启停 + 局部编辑（未修改的字段不会被覆盖）
// ===========================================================================

let mySharesCache = [];

// 加载我的分享列表（结果缓存供编辑弹窗回填）
async function loadMyShares() {
    const section = document.getElementById('mySharesSection');
    if (!(await TokenManager.isValid())) {
        section.style.display = 'none';
        mySharesCache = [];
        return;
    }
    section.style.display = 'block';
    try {
        const response = await fetch(`${API_BASE}/shares`, {
            headers: { 'Authorization': `Bearer ${TokenManager.get()}` }
        });
        mySharesCache = await response.json();
        renderMyShares(mySharesCache);
    } catch (error) {
        document.getElementById('mySharesList').innerHTML =
            `<p class="empty-msg">加载失败: ${escapeHtml(error.message)}</p>`;
    }
};

function renderMyShares(shares) {
    const list = document.getElementById('mySharesList');
    if (shares.length === 0) {
        list.innerHTML = '<p class="empty-msg">暂无分享链接</p>';
        return;
    }

    list.innerHTML = shares.map(share => {
        const statusClass = share.is_valid ? 'valid' : 'invalid';
        const statusText = share.is_valid ? '有效' : (share.error_msg || '无效');

        return `
            <div class="share-item ${share.active ? '' : 'is-disabled'}">
                <div class="share-item-header">
                    <span class="share-item-filename">${escapeHtml(share.filename || '(文件已失效)')}</span>
                    <span class="share-item-status ${statusClass}">
                        ${share.active ? statusText : '已停用'}
                    </span>
                </div>
                ${share.note ? `<div class="share-item-note">📝 ${escapeHtml(share.note)}</div>` : ''}
                <div class="share-item-details">
                    <div class="share-item-detail">
                        <span class="share-item-detail-label">剩余时间</span>
                        <span class="share-item-detail-value">${formatRemainingTime(share.expires_at)}</span>
                    </div>
                    <div class="share-item-detail">
                        <span class="share-item-detail-label">已取件</span>
                        <span class="share-item-detail-value">${share.download_count} / ${share.max_downloads || '∞'}</span>
                    </div>
                    <div class="share-item-detail">
                        <span class="share-item-detail-label">创建时间</span>
                        <span class="share-item-detail-value">${new Date(share.created_at).toLocaleString('zh-CN')}</span>
                    </div>
                </div>
                <div class="share-item-actions">
                    <button class="copy-link-btn" onclick="copyShareLinkFromList('${share.share_id}')">🔗 复制链接</button>
                    <button class="toggle-share-btn" onclick="quickToggleShare('${share.share_id}', ${!share.active})">
                        ${share.active ? '⏸️ 停用' : '▶️ 启用'}
                    </button>
                    <button class="edit-share-btn" onclick="openShareEditModal('${share.share_id}')">✏️ 编辑</button>
                    <button class="delete-share-btn" onclick="deleteShare('${share.share_id}')">🗑️ 删除</button>
                </div>
            </div>
        `;
    }).join('');
}

async function patchShare(shareId, fields) {
    const response = await fetch(`${API_BASE}/share/${shareId}`, {
        method: 'PATCH',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${TokenManager.get()}`
        },
        body: JSON.stringify(fields)
    });
    return { response, body: await response.json().catch(() => ({})) };
}

async function quickToggleShare(shareId, enable) {
    const { response, body } = await patchShare(shareId, { active: enable });
    if (!response.ok) {
        alert(`${enable ? '启用' : '停用'}失败: ${body.error || '未知错误'}`);
    }
    loadMyShares();
}

// 打开编辑弹窗：用服务端当前值回填，作为修改基线
async function openShareEditModal(shareId) {
    const share = mySharesCache.find(s => s.share_id === shareId);
    if (!share) return;

    currentEditShareId = shareId;
    document.getElementById('shareEditFileName').textContent =
        `${share.filename || '(文件已失效)'} · 已取件 ${share.download_count} 次`;
    // 有效期默认留空 = 不修改；避免打开弹窗再保存就被无意“续期”
    const expireInput = document.getElementById('editExpireHours');
    expireInput.value = '';
    expireInput.placeholder = share.expires_at
        ? `不修改（当前剩余 ${formatRemainingTime(share.expires_at)}），-1 改为永久`
        : '不修改（当前永久有效）';
    document.getElementById('editMaxDownloads').value = share.max_downloads == null ? -1 : share.max_downloads;
    document.getElementById('editNote').value = share.note || '';
    document.getElementById('editActive').checked = share.active;
    document.getElementById('shareEditError').textContent = '';
    document.getElementById('shareEditModal').classList.add('active');
}

function closeShareEditModal() {
    document.getElementById('shareEditModal').classList.remove('active');
    currentEditShareId = null;
}

// 保存编辑：只提交用户实际改动的字段，保证未修改记录不被覆盖
async function confirmShareEdit() {
    if (!currentEditShareId) return;

    const share = mySharesCache.find(s => s.share_id === currentEditShareId);
    const fields = {};

    const newMax = parseInt(document.getElementById('editMaxDownloads').value, 10);
    if (Number.isNaN(newMax)) {
        document.getElementById('shareEditError').textContent = '请填写取件次数上限';
        return;
    }
    const normalizedNewMax = newMax < 0 ? null : newMax;
    const oldMax = share.max_downloads == null ? null : share.max_downloads;
    if (normalizedNewMax !== oldMax) {
        fields.max_downloads = newMax;
    }

    const newNote = document.getElementById('editNote').value.trim();
    if (newNote !== (share.note || '')) {
        fields.note = newNote;
    }

    const newActive = document.getElementById('editActive').checked;
    if (newActive !== share.active) {
        fields.active = newActive;
    }

    // 有效期：留空表示不修改；-1 表示永久；正整数表示从现在起 N 小时
    const newHoursRaw = document.getElementById('editExpireHours').value.trim();
    if (newHoursRaw !== '') {
        const newHours = parseInt(newHoursRaw, 10);
        if (newHoursRaw === '-1') {
            if (share.expires_at != null) fields.expire_hours = -1;
        } else if (Number.isNaN(newHours) || newHours <= 0) {
            document.getElementById('shareEditError').textContent = '有效期需为正整数，或 -1 表示永久';
            return;
        } else {
            fields.expire_hours = newHours;
        }
    }

    if (Object.keys(fields).length === 0) {
        document.getElementById('shareEditError').textContent = '没有检测到修改';
        return;
    }

    showLoading('保存策略中...');
    try {
        const { response, body } = await patchShare(currentEditShareId, fields);
        if (response.ok) {
            closeShareEditModal();
            loadMyShares();
        } else {
            document.getElementById('shareEditError').textContent =
                body.error_code === 'POLICY_CONFLICT'
                    ? `${body.error}（可重试：调整上限后再次保存即可）`
                    : (body.error || '保存失败');
        }
    } catch (error) {
        document.getElementById('shareEditError').textContent = `网络错误: ${error.message}`;
    } finally {
        hideLoading();
    }
}


