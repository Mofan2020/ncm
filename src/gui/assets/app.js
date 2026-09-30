/**
 * NetEase Music Downloader - Frontend Application
 * Handles UI interactions and communicates with Python backend via pywebview bridge
 */

// ===== State =====
const state = {
    translations: {},
    currentPlaylist: null,
    playlistSongs: [],
    selectedSongs: new Set(),
    downloadTasks: new Map(),
    isDownloading: false,
    isPaused: false,
    settings: null,
};

// ===== Initialization =====
document.addEventListener('DOMContentLoaded', () => {
    initializeApp();
});

async function initializeApp() {
    try {
        // Load translations
        state.translations = await getTranslations();
        applyTranslations();
        
        // Load settings
        state.settings = await getSettings();
        applySettings();
        
        // Check login status
        await checkLoginStatus();
        
        // Setup event listeners
        setupEventListeners();
        
        showToast('success', '应用已就绪');
    } catch (error) {
        console.error('Initialization error:', error);
        showToast('error', '初始化失败: ' + error.message);
    }
}

// ===== Translation System =====
async function getTranslations() {
    if (window.pywebview) {
        return await window.pywebview.api.get_translations();
    }
    return {};
}

function t(key) {
    return state.translations[key] || key;
}

function applyTranslations() {
    // Update all elements with data-i18n attribute
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        el.textContent = t(key);
    });
    
    // Update placeholders
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.getAttribute('data-i18n-placeholder');
        el.placeholder = t(key);
    });
    
    // Update specific elements
    const elements = {
        'app-title': 'app.title',
        'app-subtitle': 'app.subtitle',
        'download-title': 'download.title',
        'playlist-input': 'download.playlist_placeholder',
        'btn-fetch': 'download.fetch_btn',
        'quality-label': 'download.quality_label',
        'concurrent-label': 'download.concurrent_label',
        'overwrite-label': 'download.overwrite_label',
        'skip-check-label': 'download.skip_check_label',
        'btn-start': 'download.start_btn',
        'btn-pause': 'download.pause_btn',
        'btn-cancel': 'download.cancel_btn',
        'btn-open-folder': 'download.open_folder',
        'playlist-title': 'playlist.title',
        'tracks-title': 'playlist.tracks',
        'download-queue-title': 'download.title',
        'status-text': 'status.ready',
    };
    
    for (const [id, key] of Object.entries(elements)) {
        const el = document.getElementById(id);
        if (el) el.textContent = t(key);
    }
    
    // Update quality options
    const qualitySelect = document.getElementById('quality-select');
    if (qualitySelect) {
        const options = qualitySelect.querySelectorAll('option');
        const keys = ['download.quality_standard', 'download.quality_higher', 'download.quality_exhigh', 'download.quality_lossless', 'download.quality_hires'];
        options.forEach((opt, i) => {
            if (keys[i]) opt.textContent = t(keys[i]);
        });
    }
}

// ===== Settings =====
async function getSettings() {
    if (window.pywebview) {
        return await window.pywebview.api.get_settings();
    }
    return null;
}

function applySettings() {
    if (!state.settings) return;
    
    // Apply download settings
    const qualitySelect = document.getElementById('quality-select');
    if (qualitySelect) qualitySelect.value = state.settings.download.quality;
    
    const concurrentSelect = document.getElementById('concurrent-select');
    if (concurrentSelect) concurrentSelect.value = state.settings.download.max_concurrent;
    
    const overwriteCheck = document.getElementById('overwrite-check');
    if (overwriteCheck) overwriteCheck.checked = state.settings.download.overwrite;
    
    const skipCheckCheck = document.getElementById('skip-check-check');
    if (skipCheckCheck) skipCheckCheck.checked = state.settings.download.skip_url_check;
    
    // Apply UI settings
    const languageSelect = document.getElementById('language-select');
    if (languageSelect) languageSelect.value = state.settings.ui.language;
    
    const themeSelect = document.getElementById('theme-select');
    if (themeSelect) themeSelect.value = state.settings.ui.theme;
    
    // Apply theme
    applyTheme(state.settings.ui.theme);
    
    // Apply advanced settings
    const debugCheck = document.getElementById('debug-check');
    if (debugCheck) debugCheck.checked = state.settings.debug;
    
    const rememberLoginCheck = document.getElementById('remember-login-check');
    if (rememberLoginCheck) rememberLoginCheck.checked = state.settings.auth.remember_login;
}

function applyTheme(theme) {
    if (theme === 'system') {
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        document.documentElement.setAttribute('data-theme', prefersDark ? 'dark' : 'light');
    } else {
        document.documentElement.setAttribute('data-theme', theme);
    }
}

// ===== Login System =====
async function checkLoginStatus() {
    if (!window.pywebview) return;
    
    try {
        const status = await window.pywebview.api.get_login_status();
        updateLoginUI(status);
    } catch (error) {
        console.error('Check login status error:', error);
    }
}

function updateLoginUI(status) {
    const btnLogin = document.getElementById('btn-login');
    if (!btnLogin) return;
    
    if (status.is_logged_in && status.user) {
        btnLogin.innerHTML = `
            <img src="${status.user.avatar_url || ''}" class="user-avatar" alt="">
            <span class="user-name">${status.user.nickname}</span>
            ${status.user.vip_type > 0 ? '<span class="vip-badge">VIP</span>' : ''}
        `;
        btnLogin.onclick = showLoginModal;
    } else {
        btnLogin.textContent = t('login.title');
        btnLogin.onclick = showLoginModal;
    }
}

function showLoginModal() {
    const modal = document.getElementById('login-modal');
    if (modal) modal.style.display = 'flex';
    
    // Start QR code login by default
    switchLoginTab('qrcode');
}

function hideLoginModal() {
    const modal = document.getElementById('login-modal');
    if (modal) modal.style.display = 'none';
    
    if (window.pywebview) {
        window.pywebview.api.cancel_login();
    }
}

function switchLoginTab(tab) {
    // Update tab buttons
    document.querySelectorAll('#login-modal .tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === tab);
    });
    
    // Update tab content
    document.querySelectorAll('#login-modal .tab-content').forEach(c => {
        c.classList.toggle('active', c.id === `tab-${tab}`);
    });
    
    // Start QR code login if QR tab selected
    if (tab === 'qrcode' && window.pywebview) {
        window.pywebview.api.login_qrcode();
    }
}

// QR Code Login
function onQrcodeUpdate(data) {
    const img = document.getElementById('qrcode-image');
    const status = document.getElementById('qrcode-status');
    
    if (img && data.image) {
        img.src = data.image;
    }
    
    if (status) {
        status.textContent = t('login.qrcode_waiting');
        status.className = 'qrcode-status';
    }
}

function onLoginStatusChange(data) {
    const statusEl = document.getElementById('qrcode-status');
    if (!statusEl) return;
    
    switch (data.status) {
        case 'pending':
            statusEl.textContent = t('login.qrcode_waiting');
            statusEl.className = 'qrcode-status';
            break;
        case 'scanned':
            statusEl.textContent = t('login.qrcode_scanned');
            statusEl.className = 'qrcode-status scanned';
            break;
        case 'expired':
            statusEl.textContent = t('login.qrcode_expired');
            statusEl.className = 'qrcode-status expired';
            break;
    }
}

function onLoginSuccess(user) {
    showToast('success', t('login.login_success'));
    hideLoginModal();
    checkLoginStatus();
}

function onLoginFailed(error) {
    showToast('error', error || t('login.login_failed'));
}

function refreshQrcode() {
    if (window.pywebview) {
        window.pywebview.api.refresh_qrcode();
    }
}

// Phone Login
let phoneCodeTimer = null;

async function sendPhoneCode() {
    const phoneInput = document.getElementById('phone-input');
    const phone = phoneInput.value.trim();
    
    if (!phone || !/^1\d{10}$/.test(phone)) {
        showToast('warning', '请输入正确的手机号');
        return;
    }
    
    try {
        const result = await window.pywebview.api.send_phone_code(phone);
        if (result) {
            showToast('success', '验证码已发送');
            startCountdown();
        } else {
            showToast('error', '发送失败，请重试');
        }
    } catch (error) {
        showToast('error', '发送失败: ' + error.message);
    }
}

function startCountdown() {
    const btn = document.getElementById('btn-send-code');
    let seconds = 60;
    
    btn.disabled = true;
    btn.textContent = `${seconds}s`;
    
    phoneCodeTimer = setInterval(() => {
        seconds--;
        btn.textContent = `${seconds}s`;
        
        if (seconds <= 0) {
            clearInterval(phoneCodeTimer);
            btn.disabled = false;
            btn.textContent = t('login.send_code');
        }
    }, 1000);
}

async function loginPhone() {
    const phone = document.getElementById('phone-input').value.trim();
    const code = document.getElementById('code-input').value.trim();
    
    if (!phone || !code) {
        showToast('warning', '请输入手机号和验证码');
        return;
    }
    
    try {
        const result = await window.pywebview.api.login_phone(phone, code);
        if (!result) {
            showToast('error', t('login.login_failed'));
        }
    } catch (error) {
        showToast('error', t('login.login_error'));
    }
}

// Cookie Login
async function loginCookie() {
    const cookie = document.getElementById('cookie-input').value.trim();
    
    if (!cookie) {
        showToast('warning', '请输入Cookie');
        return;
    }
    
    try {
        const result = await window.pywebview.api.login_cookie(cookie);
        if (!result) {
            showToast('error', t('login.cookie_invalid'));
        }
    } catch (error) {
        showToast('error', t('login.login_error'));
    }
}

// ===== Playlist =====
async function fetchPlaylist() {
    const input = document.getElementById('playlist-input');
    const playlistInput = input.value.trim();
    
    if (!playlistInput) {
        showToast('warning', '请输入歌单ID或链接');
        return;
    }
    
    setLoading(true);
    updateStatus('fetching_playlist');
    
    try {
        const result = await window.pywebview.api.fetch_playlist(playlistInput);
        
        if (result.success) {
            state.currentPlaylist = result.playlist;
            state.playlist_songs = result.tracks;
            state.selectedSongs.clear();
            
            renderPlaylistInfo(result.playlist);
            renderSongList(result.tracks);
            
            document.getElementById('playlist-card').style.display = 'block';
            document.getElementById('song-list-card').style.display = 'block';
            document.getElementById('btn-start').disabled = false;
            
            showToast('success', `成功获取 ${result.tracks.length} 首歌曲`);
        } else {
            showToast('error', result.error || '获取歌单失败');
        }
    } catch (error) {
        showToast('error', '获取歌单失败: ' + error.message);
    } finally {
        setLoading(false);
        updateStatus('ready');
    }
}

function renderPlaylistInfo(playlist) {
    document.getElementById('playlist-name').textContent = playlist.name;
    document.getElementById('playlist-creator').textContent = `${t('playlist.creator')}: ${playlist.creator}`;
    document.getElementById('playlist-count').textContent = `${t('playlist.track_count')}: ${playlist.track_count}`;
    
    const cover = document.getElementById('playlist-cover');
    if (cover && playlist.cover_url) {
        cover.src = playlist.cover_url;
        cover.onerror = () => { cover.src = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80"><rect fill="%23ddd" width="80" height="80"/><text x="40" y="45" text-anchor="middle" fill="%23999" font-size="12">No Image</text></svg>'; };
    }
}

function renderSongList(tracks) {
    const container = document.getElementById('song-list');
    container.innerHTML = '';
    
    tracks.forEach((track, index) => {
        const item = document.createElement('div');
        item.className = 'song-item';
        item.dataset.index = index;
        
        const isVip = track.fee > 0;
        const vipBadge = isVip ? '<span class="vip-badge">VIP</span>' : '';
        
        item.innerHTML = `
            <input type="checkbox" id="song-check-${index}" onchange="toggleSong(${index}, this.checked)">
            <span class="song-index">${index + 1}</span>
            <div class="song-info">
                <div class="song-name">${track.name} ${vipBadge}</div>
                <div class="song-artist">${track.artists}</div>
            </div>
            <span class="song-status waiting" id="song-status-${index}">${t('download.waiting')}</span>
        `;
        
        container.appendChild(item);
    });
}

function toggleSong(index, checked) {
    if (checked) {
        state.selectedSongs.add(index);
    } else {
        state.selectedSongs.delete(index);
    }
    updateSelectionCount();
}

function selectAll() {
    state.selectedSongs = new Set(state.playlistSongs.map((_, i) => i));
    document.querySelectorAll('#song-list input[type="checkbox"]').forEach(cb => cb.checked = true);
    updateSelectionCount();
}

function deselectAll() {
    state.selectedSongs.clear();
    document.querySelectorAll('#song-list input[type="checkbox"]').forEach(cb => cb.checked = false);
    updateSelectionCount();
}

function updateSelectionCount() {
    // Could add a counter display here
}

// ===== Download =====
async function startDownload() {
    if (state.selectedSongs.size === 0) {
        showToast('warning', t('status.no_songs_selected'));
        return;
    }
    
    const options = {
        quality: document.getElementById('quality-select').value,
        max_concurrent: parseInt(document.getElementById('concurrent-select').value),
        overwrite: document.getElementById('overwrite-check').checked,
        download_dir: state.settings?.download?.download_dir || '',
    };
    
    try {
        const result = await window.pywebview.api.start_download(options);
        
        if (result.success) {
            state.isDownloading = true;
            state.isPaused = false;
            updateDownloadControls();
            updateStatus('downloading', { current: 0, total: state.selectedSongs.size });
        } else if (result.need_login) {
            showToast('warning', t('status.login_required'));
            showLoginModal();
        } else {
            showToast('error', result.error || '启动下载失败');
        }
    } catch (error) {
        showToast('error', '启动下载失败: ' + error.message);
    }
}

function pauseDownload() {
    if (state.isPaused) {
        window.pywebview.api.resume_download();
        state.isPaused = false;
    } else {
        window.pywebview.api.pause_download();
        state.isPaused = true;
    }
    updateDownloadControls();
}

function cancelDownload() {
    if (confirm(t('messages.confirm_cancel_download'))) {
        window.pywebview.api.cancel_download();
        state.isDownloading = false;
        state.isPaused = false;
        updateDownloadControls();
        updateStatus('ready');
    }
}

function updateDownloadControls() {
    const btnStart = document.getElementById('btn-start');
    const btnPause = document.getElementById('btn-pause');
    const btnCancel = document.getElementById('btn-cancel');
    const btnOpenFolder = document.getElementById('btn-open-folder');
    
    btnStart.disabled = state.isDownloading;
    btnPause.disabled = !state.isDownloading;
    btnCancel.disabled = !state.isDownloading;
    btnOpenFolder.disabled = !state.isDownloading;
    
    btnPause.textContent = state.isPaused ? t('download.resume_btn') : t('download.pause_btn');
}

function onDownloadProgress(data) {
    // Update song list status
    const statusEl = document.getElementById(`song-status-${data.index}`);
    if (statusEl) {
        statusEl.textContent = `${data.status === 'downloading' ? t('download.downloading') : t('download.' + data.status)}`;
        statusEl.className = `song-status ${data.status}`;
    }
    
    // Update queue item
    updateQueueItem(data);
    
    // Update progress bar
    updateProgressBar(data);
}

function onDownloadStats(data) {
    const progressContainer = document.getElementById('progress-container');
    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    
    progressContainer.style.display = 'block';
    
    const completed = data.success + data.failed + data.skipped;
    const percent = data.total > 0 ? (completed / data.total * 100) : 0;
    
    progressBar.style.width = `${percent}%`;
    progressText.textContent = `${percent.toFixed(1)}% (${completed}/${data.total})`;
    
    updateStatus('downloading', { current: completed, total: data.total });
}

function onDownloadComplete(data) {
    state.isDownloading = false;
    state.isPaused = false;
    updateDownloadControls();
    
    const stats = data.stats;
    showToast('success', t('status.download_complete', { success: stats.success, fail: stats.failed }));
    updateStatus('ready');
    
    // Show failed list if any
    if (stats.failed > 0) {
        console.log('Failed songs:', stats.failed);
    }
}

function updateQueueItem(data) {
    const queue = document.getElementById('download-queue');
    let item = document.getElementById(`queue-${data.song_id}`);
    
    if (!item) {
        item = document.createElement('div');
        item.className = 'queue-item';
        item.id = `queue-${data.song_id}`;
        item.innerHTML = `
            <div class="queue-item-header">
                <span class="queue-item-name">${data.artists} - ${data.name}</span>
                <span class="queue-item-status ${data.status}">${t('download.' + data.status)}</span>
            </div>
            <div class="queue-item-progress">
                <div class="queue-item-progress-bar" style="width: 0%"></div>
            </div>
            <div class="queue-item-info">
                <span class="queue-speed"></span>
                <span class="queue-size"></span>
            </div>
        `;
        queue.appendChild(item);
        
        // Remove empty state
        const empty = document.getElementById('empty-queue');
        if (empty) empty.style.display = 'none';
    }
    
    // Update progress
    const progressBar = item.querySelector('.queue-item-progress-bar');
    const statusEl = item.querySelector('.queue-item-status');
    const speedEl = item.querySelector('.queue-speed');
    const sizeEl = item.querySelector('.queue-size');
    
    if (progressBar) progressBar.style.width = `${data.progress || 0}%`;
    if (statusEl) {
        statusEl.textContent = t('download.' + data.status);
        statusEl.className = `queue-item-status ${data.status}`;
    }
    if (speedEl && data.speed) {
        speedEl.textContent = `${(data.speed / 1024).toFixed(1)} MB/s`;
    }
    if (sizeEl && data.total_size) {
        const downloaded = (data.downloaded_size / 1024 / 1024).toFixed(1);
        const total = (data.total_size / 1024 / 1024).toFixed(1);
        sizeEl.textContent = `${downloaded} / ${total} MB`;
    }
}

function updateProgressBar(data) {
    const container = document.getElementById('progress-container');
    const bar = document.getElementById('progress-bar');
    const text = document.getElementById('progress-text');
    
    container.style.display = 'block';
    bar.style.width = `${data.progress || 0}%`;
    text.textContent = `${(data.progress || 0).toFixed(1)}%`;
}

function openFolder() {
    if (window.pywebview) {
        window.pywebview.api.open_download_folder();
    }
}

// ===== Settings Modal =====
function showSettingsModal() {
    const modal = document.getElementById('settings-modal');
    if (modal) modal.style.display = 'flex';
}

function hideSettingsModal() {
    const modal = document.getElementById('settings-modal');
    if (modal) modal.style.display = 'none';
}

function switchSettingsTab(tab) {
    document.querySelectorAll('#settings-modal .tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === tab);
    });
    document.querySelectorAll('#settings-modal .tab-content').forEach(c => {
        c.classList.toggle('active', c.id === `settings-${tab}`);
    });
}

async function changeLanguage(lang) {
    if (window.pywebview) {
        await window.pywebview.api.set_language(lang);
        showToast('info', t('messages.language_changed'));
    }
}

async function chooseDirectory() {
    if (window.pywebview) {
        const dir = await window.pywebview.api.choose_directory();
        if (dir) {
            document.getElementById('download-dir-input').value = dir;
        }
    }
}

async function saveSettings() {
    const settings = {
        download: {
            quality: document.getElementById('default-quality-select').value,
            max_concurrent: parseInt(document.getElementById('max-concurrent-select').value),
            overwrite: document.getElementById('overwrite-check').checked,
            download_dir: document.getElementById('download-dir-input').value,
            skip_url_check: document.getElementById('skip-check-check').checked,
        },
        ui: {
            language: document.getElementById('language-select').value,
            theme: document.getElementById('theme-select').value,
        },
        auth: {
            remember_login: document.getElementById('remember-login-check').checked,
        },
        debug: document.getElementById('debug-check').checked,
    };
    
    if (window.pywebview) {
        await window.pywebview.api.update_settings('download', settings.download);
        await window.pywebview.api.update_settings('ui', settings.ui);
        await window.pywebview.api.update_settings('auth', settings.auth);
        await window.pywebview.api.update_settings('debug', { debug: settings.debug });
        
        state.settings = await getSettings();
        applySettings();
        showToast('success', t('messages.settings_saved'));
    }
    
    hideSettingsModal();
}

async function resetSettings() {
    if (confirm('确定要重置所有设置吗?')) {
        if (window.pywebview) {
            await window.pywebview.api.reset_settings();
            state.settings = await getSettings();
            applySettings();
            showToast('info', t('messages.settings_reset'));
        }
    }
}

// ===== Window Controls =====
function minimizeWindow() {
    if (window.pywebview) window.pywebview.api.minimize_window();
}

function maximizeWindow() {
    if (window.pywebview) window.pywebview.api.maximize_window();
}

function closeWindow() {
    if (window.pywebview) window.pywebview.api.close_window();
}

// ===== UI Helpers =====
function setLoading(loading) {
    const btn = document.getElementById('btn-fetch');
    if (btn) {
        btn.disabled = loading;
        btn.textContent = loading ? t('download.fetching') : t('download.fetch_btn');
    }
}

function updateStatus(status, data = {}) {
    const statusText = document.getElementById('status-text');
    const statsText = document.getElementById('stats-text');
    
    const statusKey = `status.${status}`;
    let message = t(statusKey);
    
    if (data.current !== undefined && data.total !== undefined) {
        message = message.replace('{current}', data.current).replace('{total}', data.total);
    }
    
    if (statusText) statusText.textContent = message;
    
    if (status === 'downloading' && statsText) {
        const completed = data.current || 0;
        const total = data.total || 0;
        statsText.textContent = `${completed}/${total}`;
    }
}

function showToast(type, message) {
    // Create container if not exists
    let container = document.querySelector('.toast-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function setupEventListeners() {
    // Enter key on playlist input
    const input = document.getElementById('playlist-input');
    if (input) {
        input.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') fetchPlaylist();
        });
    }
    
    // Theme change listener
    const themeSelect = document.getElementById('theme-select');
    if (themeSelect) {
        themeSelect.addEventListener('change', (e) => applyTheme(e.target.value));
    }
}

// ===== Language Change Handler =====
function onLanguageChanged(translations) {
    state.translations = translations;
    applyTranslations();
}