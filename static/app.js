// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let currentPage = 1;
let currentTag = '';
let currentQuery = '';
let currentView = 'grid';
let currentDocId = null;
let pollingIntervals = {};
let currentUser = null;
let tsPollingInterval = null;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
    initTheme();
    initDropZone();
    await checkAuth();
});

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
function initTheme() {
    const saved = localStorage.getItem('dv-theme') || 'light';
    applyTheme(saved);
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-bs-theme', theme);
    const icon = document.getElementById('themeIcon');
    icon.className = theme === 'dark' ? 'bi bi-sun' : 'bi bi-moon-stars';
    localStorage.setItem('dv-theme', theme);
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-bs-theme');
    applyTheme(current === 'dark' ? 'light' : 'dark');
}

// ---------------------------------------------------------------------------
// Pages
// ---------------------------------------------------------------------------
function showPage(page) {
    document.querySelectorAll('[id^="page-"]').forEach(el => el.classList.add('d-none'));
    document.getElementById(`page-${page}`).classList.remove('d-none');
    if (page === 'tags') loadTagsPage();
    if (page === 'documents') loadDocuments();
    if (page === 'maintenance') {
        loadTailscaleStatus();
        loadUsers();
        loadSystemInfo();
        loadTrash();
    }
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
async function checkAuth() {
    try {
        const resp = await fetch('/api/auth/me');
        if (resp.status === 403) {
            document.getElementById('accessDenied').classList.remove('d-none');
            document.querySelectorAll('[id^="page-"]').forEach(el => el.classList.add('d-none'));
            return;
        }
        if (!resp.ok) throw new Error('Auth check failed');
        currentUser = await resp.json();

        // Show user info in navbar
        const navUser = document.getElementById('navUser');
        const navUserInfo = document.getElementById('navUserInfo');
        navUser.style.display = '';
        const roleBadge = currentUser.role === 'admin'
            ? '<span class="badge bg-danger ms-1">admin</span>'
            : '<span class="badge bg-secondary ms-1">user</span>';
        navUserInfo.innerHTML = `<i class="bi bi-person-circle"></i> ${escapeHtml(currentUser.display_name)} ${roleBadge}`;

        // Show maintenance link for admins
        if (currentUser.role === 'admin') {
            document.getElementById('navMaintenance').classList.remove('d-none');
        }

        // Load main content
        loadDocuments();
        loadTags();
    } catch (err) {
        // On network error, still load app (localhost fallback)
        loadDocuments();
        loadTags();
    }
}

// ---------------------------------------------------------------------------
// Tailscale Management
// ---------------------------------------------------------------------------
async function loadTailscaleStatus() {
    const loading = document.getElementById('tsLoading');
    const disabled = document.getElementById('tsDisabled');
    const needsAuth = document.getElementById('tsNeedsAuth');
    const running = document.getElementById('tsRunning');

    loading.classList.remove('d-none');
    disabled.classList.add('d-none');
    needsAuth.classList.add('d-none');
    running.classList.add('d-none');

    try {
        const data = await api('/api/admin/tailscale/status');
        loading.classList.add('d-none');

        if (data.backend_state === 'Running') {
            running.classList.remove('d-none');
            document.getElementById('tsFqdn').textContent = data.fqdn || '—';
            document.getElementById('tsIp').textContent = data.tailscale_ip || '—';
            document.getElementById('tsServeStatus').innerHTML = data.serve_active
                ? '<span class="badge bg-success">Active</span> — proxying HTTPS to Flask'
                : '<span class="badge bg-warning text-dark">Inactive</span>';
            stopTsPolling();
        } else if (data.backend_state === 'NeedsLogin') {
            needsAuth.classList.remove('d-none');
            if (data.auth_url) {
                const link = document.getElementById('tsLoginUrl');
                link.href = data.auth_url;
                link.textContent = 'Open Tailscale Login';
            }
            startTsPolling();
        } else {
            disabled.classList.remove('d-none');
            if (data.hostname) {
                document.getElementById('tsHostnameInput').value = data.hostname;
            }
        }
    } catch (err) {
        loading.classList.add('d-none');
        disabled.classList.remove('d-none');
    }
}

async function enableTailscale() {
    const hostname = document.getElementById('tsHostnameInput').value.trim();
    if (!hostname) {
        showStatus('Please enter a hostname', 'warning');
        return;
    }

    try {
        const data = await api('/api/admin/tailscale/enable', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hostname }),
        });

        if (data.status === 'needs_auth' && data.login_url) {
            document.getElementById('tsDisabled').classList.add('d-none');
            document.getElementById('tsNeedsAuth').classList.remove('d-none');
            const link = document.getElementById('tsLoginUrl');
            link.href = data.login_url;
            link.textContent = 'Open Tailscale Login';
            // Start polling for auth completion
            startTsPolling();
        } else if (data.status === 'running') {
            showStatus('Tailscale connected!', 'success');
            loadTailscaleStatus();
        }
    } catch (err) {
        showStatus('Failed to enable Tailscale: ' + err.message, 'danger');
    }
}

function startTsPolling() {
    stopTsPolling();
    tsPollingInterval = setInterval(async () => {
        try {
            const data = await api('/api/admin/tailscale/status');
            if (data.backend_state === 'Running') {
                stopTsPolling();
                // Start serve now that we're connected
                try {
                    await api('/api/admin/tailscale/start-serve', { method: 'POST' });
                } catch { /* ignore */ }
                showStatus('Tailscale connected!', 'success');
                loadTailscaleStatus();
            }
        } catch {
            // keep polling
        }
    }, 3000);
}

function stopTsPolling() {
    if (tsPollingInterval) {
        clearInterval(tsPollingInterval);
        tsPollingInterval = null;
    }
}

async function disableTailscale() {
    if (!confirm('Disable Tailscale? The app will only be accessible via localhost.')) return;
    try {
        await api('/api/admin/tailscale/disable', { method: 'POST' });
        showStatus('Tailscale disabled', 'success');
        loadTailscaleStatus();
    } catch (err) {
        showStatus('Failed to disable Tailscale: ' + err.message, 'danger');
    }
}

// ---------------------------------------------------------------------------
// User Management
// ---------------------------------------------------------------------------
async function loadUsers() {
    try {
        const users = await api('/api/admin/users');
        const tbody = document.getElementById('usersTableBody');
        if (users.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No users configured. Users connecting via Tailscale need to be added here.</td></tr>';
            return;
        }
        tbody.innerHTML = users.map(u => `
            <tr>
                <td class="font-monospace small">${escapeHtml(u.login)}</td>
                <td>${escapeHtml(u.display_name || '')}</td>
                <td>
                    <select class="form-select form-select-sm" style="width:auto;display:inline-block;" onchange="updateUserRole('${escapeHtml(u.login)}', this.value)">
                        <option value="user" ${u.role === 'user' ? 'selected' : ''}>User</option>
                        <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option>
                    </select>
                </td>
                <td class="small text-muted">${formatDate(u.added_date)}</td>
                <td>
                    <button class="btn btn-sm btn-outline-danger" onclick="removeUser('${escapeHtml(u.login)}')">
                        <i class="bi bi-trash"></i>
                    </button>
                </td>
            </tr>`).join('');
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

async function addUser() {
    const login = document.getElementById('addUserLogin').value.trim().toLowerCase();
    const display_name = document.getElementById('addUserDisplayName').value.trim();
    const role = document.getElementById('addUserRole').value;

    if (!login) {
        showStatus('Please enter a Tailscale login email', 'warning');
        return;
    }

    try {
        await api('/api/admin/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ login, display_name, role }),
        });
        document.getElementById('addUserLogin').value = '';
        document.getElementById('addUserDisplayName').value = '';
        document.getElementById('addUserRole').value = 'user';
        showStatus(`User ${login} added`, 'success');
        loadUsers();
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

async function updateUserRole(login, role) {
    try {
        await api(`/api/admin/users/${encodeURIComponent(login)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role }),
        });
        showStatus(`Role updated for ${login}`, 'success');
    } catch (err) {
        showStatus(err.message, 'danger');
        loadUsers(); // revert UI
    }
}

async function removeUser(login) {
    if (!confirm(`Remove ${login} from allowed users?`)) return;
    try {
        await api(`/api/admin/users/${encodeURIComponent(login)}`, { method: 'DELETE' });
        showStatus(`User ${login} removed`, 'success');
        loadUsers();
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

// ---------------------------------------------------------------------------
// System Info
// ---------------------------------------------------------------------------
async function loadSystemInfo() {
    try {
        const data = await api('/api/admin/system');
        document.getElementById('sysDocCount').textContent = data.document_count;
        document.getElementById('sysTagCount').textContent = data.tag_count;
        document.getElementById('sysUserCount').textContent = data.user_count;
        document.getElementById('sysDbSize').textContent = formatBytes(data.db_size);
        document.getElementById('sysUploadSize').textContent = formatBytes(data.upload_size);
        document.getElementById('sysTailscale').innerHTML = data.tailscale_connected
            ? '<span class="badge bg-success">Connected</span>'
            : '<span class="badge bg-secondary">Not connected</span>';
    } catch (err) {
        // silent
    }
}

// ---------------------------------------------------------------------------
// Trash
// ---------------------------------------------------------------------------
async function loadTrash() {
    try {
        const data = await api('/api/admin/trash');
        const tbody = document.getElementById('trashTableBody');
        const emptyEl = document.getElementById('trashEmpty');
        const tableEl = document.getElementById('trashTable');
        if (!data.documents.length) {
            emptyEl.classList.remove('d-none');
            tableEl.classList.add('d-none');
            return;
        }
        emptyEl.classList.add('d-none');
        tableEl.classList.remove('d-none');
        tbody.innerHTML = data.documents.map(doc => `
            <tr>
                <td>${escapeHtml(doc.title)}</td>
                <td><small class="text-muted">${escapeHtml(doc.original_filename)}</small></td>
                <td><small>${new Date(doc.deleted_date).toLocaleDateString()}</small></td>
                <td><span class="badge ${doc.days_until_purge <= 7 ? 'bg-danger' : 'bg-secondary'}">${doc.days_until_purge}d</span></td>
                <td class="text-end">
                    <button class="btn btn-outline-success btn-sm me-1" onclick="restoreDocument(${doc.id})" title="Restore">
                        <i class="bi bi-arrow-counterclockwise"></i>
                    </button>
                    <button class="btn btn-outline-danger btn-sm" onclick="permanentDeleteDocument(${doc.id})" title="Delete permanently">
                        <i class="bi bi-x-lg"></i>
                    </button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        // silent
    }
}

async function restoreDocument(docId) {
    try {
        await api(`/api/admin/trash/${docId}/restore`, { method: 'POST' });
        showStatus('Document restored');
        loadTrash();
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

async function permanentDeleteDocument(docId) {
    if (!confirm('Permanently delete this document? This cannot be undone.')) return;
    try {
        await api(`/api/admin/trash/${docId}`, { method: 'DELETE' });
        showStatus('Document permanently deleted');
        loadTrash();
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

async function emptyTrash() {
    if (!confirm('Permanently delete ALL documents in the trash? This cannot be undone.')) return;
    try {
        const data = await api('/api/admin/trash', { method: 'DELETE' });
        showStatus(data.message);
        loadTrash();
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

// ---------------------------------------------------------------------------
// Status messages
// ---------------------------------------------------------------------------
function showStatus(msg, type = 'success') {
    const area = document.getElementById('statusArea');
    const id = 'status-' + Date.now();
    area.innerHTML += `
        <div id="${id}" class="alert alert-${type} alert-dismissible fade show shadow-sm" role="alert" style="min-width:280px;">
            ${msg}
            <button type="button" class="btn-close btn-close-sm" data-bs-dismiss="alert"></button>
        </div>`;
    setTimeout(() => {
        const el = document.getElementById(id);
        if (el) el.remove();
    }, 4000);
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function api(url, options = {}) {
    const resp = await fetch(url, options);
    if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || `Request failed (${resp.status})`);
    }
    return resp.json();
}

function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso + 'Z');
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Documents — Load & Render
// ---------------------------------------------------------------------------
async function loadDocuments() {
    const sort = document.getElementById('sortSelect').value;
    const [sortField, sortOrder] = sort.split(':');
    const params = new URLSearchParams({
        page: currentPage,
        per_page: 20,
        sort: sortField,
        order: sortOrder,
    });
    if (currentTag) params.set('tag', currentTag);
    if (currentQuery) params.set('q', currentQuery);

    try {
        const data = await api(`/api/documents?${params}`);
        renderDocuments(data);
        renderPagination(data);
        document.getElementById('resultsInfo').textContent =
            `${data.total} document${data.total !== 1 ? 's' : ''}` +
            (currentQuery ? ` matching "${currentQuery}"` : '') +
            (currentTag ? ` tagged "${currentTag}"` : '');
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

function renderDocuments(data) {
    const gridContainer = document.getElementById('documentContainer');
    const listContainer = document.getElementById('documentListContainer');

    if (data.documents.length === 0) {
        const empty = `<div class="col-12 text-center text-muted py-5">
            <i class="bi bi-archive display-1"></i>
            <p class="mt-3">${currentQuery || currentTag ? 'No documents match your search.' : 'No documents yet. Upload your first document!'}</p>
        </div>`;
        gridContainer.innerHTML = empty;
        listContainer.innerHTML = empty;
        return;
    }

    // Grid view
    gridContainer.innerHTML = data.documents.map(doc => `
        <div class="col">
            <div class="card h-100 doc-card" onclick="openDocument(${doc.id})" role="button">
                <img src="/api/documents/${doc.id}/thumbnail" class="card-img-top doc-thumb" alt="thumbnail" loading="lazy">
                <div class="card-body p-2">
                    <h6 class="card-title mb-1 text-truncate" title="${escapeHtml(doc.title)}">${escapeHtml(doc.title)}</h6>
                    <div class="small text-muted">${doc.file_type.toUpperCase()} &bull; ${formatBytes(doc.file_size)}</div>
                    <div class="mt-1">${doc.tags.map(t => `<span class="badge bg-primary-subtle text-primary-emphasis me-1">${escapeHtml(t.name)}</span>`).join('')}</div>
                </div>
            </div>
        </div>
    `).join('');

    // List view
    listContainer.innerHTML = `<div class="list-group">${data.documents.map(doc => `
        <a href="#" class="list-group-item list-group-item-action d-flex align-items-center gap-3" onclick="openDocument(${doc.id}); return false;">
            <img src="/api/documents/${doc.id}/thumbnail" class="rounded" style="width:48px;height:48px;object-fit:cover;" alt="" loading="lazy">
            <div class="flex-grow-1 min-w-0">
                <div class="fw-semibold text-truncate">${escapeHtml(doc.title)}</div>
                <div class="small text-muted">${doc.file_type.toUpperCase()} &bull; ${formatBytes(doc.file_size)} &bull; ${formatDate(doc.upload_date)}</div>
            </div>
            <div>${doc.tags.map(t => `<span class="badge bg-primary-subtle text-primary-emphasis me-1">${escapeHtml(t.name)}</span>`).join('')}</div>
        </a>
    `).join('')}</div>`;
}

function renderPagination(data) {
    const nav = document.querySelector('#paginationNav .pagination');
    if (data.pages <= 1) { nav.innerHTML = ''; return; }
    let html = '';
    html += `<li class="page-item ${data.page <= 1 ? 'disabled' : ''}"><a class="page-link" href="#" onclick="goToPage(${data.page - 1}); return false;">&laquo;</a></li>`;
    for (let i = 1; i <= data.pages; i++) {
        if (data.pages > 7 && i > 2 && i < data.pages - 1 && Math.abs(i - data.page) > 1) {
            if (i === 3 || i === data.pages - 2) html += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
            continue;
        }
        html += `<li class="page-item ${i === data.page ? 'active' : ''}"><a class="page-link" href="#" onclick="goToPage(${i}); return false;">${i}</a></li>`;
    }
    html += `<li class="page-item ${data.page >= data.pages ? 'disabled' : ''}"><a class="page-link" href="#" onclick="goToPage(${data.page + 1}); return false;">&raquo;</a></li>`;
    nav.innerHTML = html;
}

function goToPage(page) {
    currentPage = page;
    loadDocuments();
    window.scrollTo(0, 0);
}

function setView(view) {
    currentView = view;
    document.getElementById('viewGrid').classList.toggle('active', view === 'grid');
    document.getElementById('viewList').classList.toggle('active', view === 'list');
    document.getElementById('documentContainer').classList.toggle('d-none', view !== 'grid');
    document.getElementById('documentListContainer').classList.toggle('d-none', view !== 'list');
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------
function handleSearch(e) {
    e.preventDefault();
    currentQuery = document.getElementById('searchInput').value.trim();
    currentPage = 1;
    document.getElementById('clearSearchBtn').style.display = currentQuery ? '' : 'none';
    loadDocuments();
}

function clearSearch() {
    document.getElementById('searchInput').value = '';
    currentQuery = '';
    currentPage = 1;
    document.getElementById('clearSearchBtn').style.display = 'none';
    loadDocuments();
}

// ---------------------------------------------------------------------------
// Tags sidebar
// ---------------------------------------------------------------------------
async function loadTags() {
    try {
        const tags = await api('/api/tags');
        const sidebar = document.getElementById('tagSidebar');
        sidebar.innerHTML = `<a href="#" class="list-group-item list-group-item-action ${!currentTag ? 'active' : ''}" onclick="filterByTag(''); return false;">
            All Documents
        </a>`;
        tags.forEach(t => {
            sidebar.innerHTML += `<a href="#" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center ${currentTag === t.name ? 'active' : ''}" onclick="filterByTag('${escapeHtml(t.name)}'); return false;">
                ${escapeHtml(t.name)}
                <span class="badge bg-primary rounded-pill">${t.doc_count}</span>
            </a>`;
        });
    } catch (err) {
        // silent
    }
}

function filterByTag(tag) {
    currentTag = tag;
    currentPage = 1;
    loadTags();
    loadDocuments();
}

// ---------------------------------------------------------------------------
// Tags page
// ---------------------------------------------------------------------------
async function loadTagsPage() {
    try {
        const tags = await api('/api/tags');
        const tbody = document.getElementById('tagsTableBody');
        if (tags.length === 0) {
            tbody.innerHTML = '<tr><td colspan="3" class="text-center text-muted">No tags yet</td></tr>';
            return;
        }
        tbody.innerHTML = tags.map(t => `
            <tr>
                <td><span class="badge bg-primary-subtle text-primary-emphasis">${escapeHtml(t.name)}</span></td>
                <td>${t.doc_count}</td>
                <td><button class="btn btn-sm btn-outline-danger" onclick="deleteTag(${t.id}, '${escapeHtml(t.name)}')"><i class="bi bi-trash"></i></button></td>
            </tr>`).join('');
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

async function deleteTag(id, name) {
    if (!confirm(`Delete tag "${name}"? It will be removed from all documents.`)) return;
    try {
        await api(`/api/tags/${id}`, { method: 'DELETE' });
        showStatus('Tag deleted');
        loadTagsPage();
        loadTags();
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------
function showUploadModal() {
    const modal = new bootstrap.Modal(document.getElementById('uploadModal'));
    document.getElementById('uploadQueue').classList.add('d-none');
    document.getElementById('uploadItems').innerHTML = '';
    modal.show();
}

function initDropZone() {
    const zone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const cameraInput = document.getElementById('cameraInput');

    zone.addEventListener('click', (e) => {
        if (e.target.closest('button')) return;
        fileInput.click();
    });

    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('drag-over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
    zone.addEventListener('drop', (e) => {
        e.preventDefault();
        zone.classList.remove('drag-over');
        handleFiles(e.dataTransfer.files);
    });

    fileInput.addEventListener('change', () => { handleFiles(fileInput.files); fileInput.value = ''; });
    cameraInput.addEventListener('change', () => { handleFiles(cameraInput.files); cameraInput.value = ''; });
}

function handleFiles(fileList) {
    if (!fileList.length) return;
    document.getElementById('uploadQueue').classList.remove('d-none');
    Array.from(fileList).forEach(file => addUploadItem(file));
}

function addUploadItem(file) {
    const container = document.getElementById('uploadItems');
    const itemId = 'upload-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7);

    container.innerHTML += `
        <div id="${itemId}" class="card mb-2">
            <div class="card-body p-2">
                <div class="d-flex align-items-center gap-2 mb-2">
                    <i class="bi bi-file-earmark"></i>
                    <span class="fw-semibold small text-truncate">${escapeHtml(file.name)}</span>
                    <span class="text-muted small">${formatBytes(file.size)}</span>
                </div>
                <div class="mb-2">
                    <input type="text" class="form-control form-control-sm" placeholder="Title (optional)" id="${itemId}-title">
                </div>
                <div class="mb-2">
                    <input type="text" class="form-control form-control-sm" placeholder="Tags (comma-separated)" id="${itemId}-tags">
                </div>
                <div class="mb-2">
                    <textarea class="form-control form-control-sm" placeholder="Notes (optional)" rows="1" id="${itemId}-notes"></textarea>
                </div>
                <div class="progress mb-2" style="height:6px; display:none;" id="${itemId}-progress">
                    <div class="progress-bar" style="width:0%"></div>
                </div>
                <div class="d-flex justify-content-between align-items-center">
                    <small class="text-muted" id="${itemId}-status"></small>
                    <button class="btn btn-sm btn-primary" id="${itemId}-btn" onclick="uploadFile('${itemId}')">
                        <i class="bi bi-upload"></i> Upload
                    </button>
                </div>
            </div>
        </div>`;

    // store file reference
    const el = document.getElementById(itemId);
    el._file = file;
}

async function uploadFile(itemId) {
    const el = document.getElementById(itemId);
    const file = el._file;
    const title = document.getElementById(`${itemId}-title`).value;
    const tags = document.getElementById(`${itemId}-tags`).value;
    const notes = document.getElementById(`${itemId}-notes`).value;
    const btn = document.getElementById(`${itemId}-btn`);
    const progressWrap = document.getElementById(`${itemId}-progress`);
    const progressBar = progressWrap.querySelector('.progress-bar');
    const statusEl = document.getElementById(`${itemId}-status`);

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    progressWrap.style.display = '';

    const formData = new FormData();
    formData.append('file', file);
    if (title) formData.append('title', title);
    if (tags) formData.append('tags', tags);
    if (notes) formData.append('notes', notes);

    try {
        // Upload with XHR for progress
        const docData = await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/documents');
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) {
                    const pct = Math.round((e.loaded / e.total) * 100);
                    progressBar.style.width = pct + '%';
                    statusEl.textContent = `Uploading... ${pct}%`;
                }
            };
            xhr.onload = () => {
                if (xhr.status === 201) {
                    resolve(JSON.parse(xhr.responseText));
                } else {
                    const err = JSON.parse(xhr.responseText);
                    reject(new Error(err.error || 'Upload failed'));
                }
            };
            xhr.onerror = () => reject(new Error('Network error'));
            xhr.send(formData);
        });

        statusEl.textContent = 'Processing OCR...';
        progressBar.style.width = '100%';
        progressBar.classList.add('progress-bar-striped', 'progress-bar-animated');

        // Poll OCR status
        pollOcrStatus(docData.id, itemId);
    } catch (err) {
        statusEl.textContent = err.message;
        statusEl.classList.add('text-danger');
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-arrow-repeat"></i> Retry';
    }
}

function pollOcrStatus(docId, itemId) {
    const statusEl = document.getElementById(`${itemId}-status`);
    const progressBar = document.querySelector(`#${itemId}-progress .progress-bar`);
    const btn = document.getElementById(`${itemId}-btn`);

    const interval = setInterval(async () => {
        try {
            const data = await api(`/api/documents/${docId}/status`);
            if (statusEl) statusEl.textContent = data.message || data.status;
            if (progressBar) progressBar.style.width = data.progress + '%';

            if (data.status === 'complete') {
                clearInterval(interval);
                if (progressBar) {
                    progressBar.classList.remove('progress-bar-striped', 'progress-bar-animated');
                    progressBar.classList.add('bg-success');
                }
                if (statusEl) statusEl.textContent = 'Done!';
                if (btn) { btn.innerHTML = '<i class="bi bi-check-lg"></i>'; btn.classList.replace('btn-primary', 'btn-success'); }
                loadDocuments();
                loadTags();
            } else if (data.status === 'error') {
                clearInterval(interval);
                if (progressBar) progressBar.classList.add('bg-danger');
                if (statusEl) { statusEl.textContent = data.message; statusEl.classList.add('text-danger'); }
                if (btn) { btn.innerHTML = '<i class="bi bi-x-lg"></i>'; btn.classList.replace('btn-primary', 'btn-danger'); }
                loadDocuments();
            }
        } catch {
            clearInterval(interval);
        }
    }, 1000);
}

// ---------------------------------------------------------------------------
// Document detail
// ---------------------------------------------------------------------------
async function openDocument(id) {
    currentDocId = id;
    try {
        const doc = await api(`/api/documents/${id}`);
        renderDocumentDetail(doc);
        const modal = new bootstrap.Modal(document.getElementById('docDetailModal'));
        modal.show();

        // Check OCR status
        try {
            const status = await api(`/api/documents/${id}/status`);
            renderOcrStatus(status);
            if (status.status === 'processing' || status.status === 'pending') {
                startDetailPolling(id);
            }
        } catch { /* ignore */ }
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

function renderDocumentDetail(doc) {
    document.getElementById('docDetailTitle').textContent = doc.title;
    document.getElementById('docDetailThumb').src = `/api/documents/${doc.id}/thumbnail`;
    document.getElementById('docDetailDownload').href = `/api/documents/${doc.id}/file`;

    // Store file type and ID for the viewer
    document.getElementById('docViewer')._docId = doc.id;
    document.getElementById('docViewer')._fileType = doc.file_type;

    // Reset viewer state
    closeDocViewer();

    document.getElementById('docFieldTitle').textContent = doc.title;
    document.getElementById('docFieldFilename').textContent = doc.original_filename;
    document.getElementById('docFieldType').textContent = doc.file_type.toUpperCase();
    document.getElementById('docFieldSize').textContent = formatBytes(doc.file_size);
    document.getElementById('docFieldDate').textContent = formatDate(doc.upload_date);
    document.getElementById('docFieldModified').textContent = formatDate(doc.modified_date);
    document.getElementById('docFieldTags').innerHTML = doc.tags.length
        ? doc.tags.map(t => `<span class="badge bg-primary-subtle text-primary-emphasis me-1">${escapeHtml(t.name)}</span>`).join('')
        : '<span class="text-muted">None</span>';
    document.getElementById('docFieldNotes').textContent = doc.notes || '—';
    document.getElementById('docOcrText').textContent = doc.ocr_text || 'No OCR text extracted yet.';

    document.getElementById('docDetailView').classList.remove('d-none');
    document.getElementById('docDetailEdit').classList.add('d-none');
}

function renderOcrStatus(status) {
    const el = document.getElementById('docOcrStatus');
    if (status.status === 'complete') {
        el.innerHTML = '<span class="badge bg-success"><i class="bi bi-check-circle"></i> Complete</span>';
    } else if (status.status === 'processing' || status.status === 'pending') {
        el.innerHTML = `<span class="badge bg-warning text-dark"><span class="spinner-border spinner-border-sm"></span> ${status.message || 'Processing...'}</span>`;
    } else if (status.status === 'error') {
        el.innerHTML = `<span class="badge bg-danger">${escapeHtml(status.message)}</span>`;
    }
}

function startDetailPolling(docId) {
    if (pollingIntervals[docId]) clearInterval(pollingIntervals[docId]);
    pollingIntervals[docId] = setInterval(async () => {
        if (currentDocId !== docId) { clearInterval(pollingIntervals[docId]); return; }
        try {
            const status = await api(`/api/documents/${docId}/status`);
            renderOcrStatus(status);
            if (status.status === 'complete') {
                clearInterval(pollingIntervals[docId]);
                const doc = await api(`/api/documents/${docId}`);
                document.getElementById('docOcrText').textContent = doc.ocr_text || 'No OCR text extracted.';
            } else if (status.status === 'error') {
                clearInterval(pollingIntervals[docId]);
            }
        } catch {
            clearInterval(pollingIntervals[docId]);
        }
    }, 1500);
}

// ---------------------------------------------------------------------------
// Document edit
// ---------------------------------------------------------------------------
function openDocViewer() {
    const viewer = document.getElementById('docViewer');
    const content = document.getElementById('docViewerContent');
    const columns = document.getElementById('docDetailColumns');
    const docId = viewer._docId;
    const fileType = viewer._fileType;
    const fileUrl = `/api/documents/${docId}/file`;

    if (fileType === 'pdf') {
        content.innerHTML = `<iframe src="${fileUrl}" style="width:100%; height:80vh; border:none;"></iframe>`;
    } else {
        content.innerHTML = `<img src="${fileUrl}" style="max-width:100%; max-height:80vh;" class="d-block mx-auto">`;
    }

    viewer.classList.remove('d-none');
    columns.classList.add('d-none');
}

function closeDocViewer() {
    const viewer = document.getElementById('docViewer');
    const content = document.getElementById('docViewerContent');
    const columns = document.getElementById('docDetailColumns');
    content.innerHTML = '';
    viewer.classList.add('d-none');
    columns.classList.remove('d-none');
}

function startEditDocument() {
    const doc = {
        title: document.getElementById('docFieldTitle').textContent,
        tags: Array.from(document.querySelectorAll('#docFieldTags .badge')).map(b => b.textContent),
        notes: document.getElementById('docFieldNotes').textContent === '—' ? '' : document.getElementById('docFieldNotes').textContent,
    };
    document.getElementById('editTitle').value = doc.title;
    document.getElementById('editTags').value = doc.tags.join(', ');
    document.getElementById('editNotes').value = doc.notes;
    document.getElementById('docDetailView').classList.add('d-none');
    document.getElementById('docDetailEdit').classList.remove('d-none');
}

function cancelEditDocument() {
    document.getElementById('docDetailView').classList.remove('d-none');
    document.getElementById('docDetailEdit').classList.add('d-none');
}

async function saveEditDocument() {
    const payload = {
        title: document.getElementById('editTitle').value.trim(),
        tags: document.getElementById('editTags').value.split(',').map(s => s.trim()).filter(Boolean),
        notes: document.getElementById('editNotes').value.trim(),
    };
    try {
        const doc = await api(`/api/documents/${currentDocId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        renderDocumentDetail(doc);
        showStatus('Document updated');
        loadDocuments();
        loadTags();
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}

// ---------------------------------------------------------------------------
// Document actions
// ---------------------------------------------------------------------------
function deleteCurrentDocument() {
    const deleteModal = new bootstrap.Modal(document.getElementById('deleteConfirmModal'));
    document.getElementById('confirmDeleteBtn').onclick = async () => {
        try {
            await api(`/api/documents/${currentDocId}`, { method: 'DELETE' });
            deleteModal.hide();
            bootstrap.Modal.getInstance(document.getElementById('docDetailModal')).hide();
            showStatus('Document moved to trash');
            loadDocuments();
            loadTags();
        } catch (err) {
            showStatus(err.message, 'danger');
        }
    };
    deleteModal.show();
}

async function reocrDocument() {
    try {
        await api(`/api/documents/${currentDocId}/reocr`, { method: 'POST' });
        showStatus('OCR re-processing started');
        renderOcrStatus({ status: 'processing', message: 'Re-running OCR...' });
        startDetailPolling(currentDocId);
    } catch (err) {
        showStatus(err.message, 'danger');
    }
}
