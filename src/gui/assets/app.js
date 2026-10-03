/**
 * 网易云音乐歌单下载器 / NetEase Music Playlist Downloader - frontend logic.
 *
 * Talks to the Python side through `window.pywebview.api` (see src/gui/bridge.py).
 * The backend pushes updates into the `on*` functions declared at the bottom of
 * this file, so they must stay global.
 *
 * Two views share one shell: 下载 (the original downloader) and 播放 (the player).
 * The player keeps its state in `player` below; audio itself is an <audio>
 * element fed by the loopback media server (src/core/mediaserver.py), which is
 * also what makes Range seeking work for both online and local files.
 *
 * Every user visible string comes from the i18n catalogue: static markup uses
 * `data-i18n` / `data-i18n-placeholder` / `data-i18n-title`, dynamic values go
 * through `t(key, params)`.  Switching language re-renders everything in place.
 */
'use strict';

const state = {
    api: null,
    appInfo: null,
    settings: null,
    translations: {},
    themeMode: (document.documentElement.dataset.themeMode || 'system'),
    playlist: null,
    tracks: [],
    selected: new Set(),
    tasks: new Map(),
    downloading: false,
    paused: false,
    completed: 0,
    total: 0,
    phoneTimer: null,
    confirmResolve: null,
};

/** Player state. The audio element itself is `audio` below. */
const player = {
    queue: [],
    index: -1,
    mode: 'list',
    current: null,
    resolved: null,
    source: null,          // {kind, id, name} of the list the queue came from
    sourceTab: 'playlists',
    lyrics: [],
    lyricIndex: -1,
    favoriteKeys: new Set(),
    counts: {},
    autoScroll: true,
    lastScrollAt: 0,
    pendingPosition: 0,
    pendingRecord: false,
    recordedKey: '',
    lastSaveAt: 0,
    playlists: [],
    playlistOpen: null,
    searchKeyword: '',
    searchOffset: 0,
    searchTotal: 0,
    localTracks: [],
    muted: false,
    volume: 0.8,
    visible: [],
    visibleSource: null,
    localOffset: 0,
    localTotal: 0,
    localKeyword: '',
    // New Phase 1 features
    queueDrawerOpen: false,
    sleepTimer: { minutes: 0, action: 'pause', timerId: null, fadeTimerId: null },
    crossfadeDuration: 0,
    crossfadeAudio: null,  // Second audio element for crossfade
    desktopLyricsWindow: null,
    desktopLyricsVisible: true,
    mediaSessionSupported: false,
    preloadedNextUrl: null,
    preloadedNextTrack: null,
    // New Phase 4 features
    dualLine: false,
    karaoke: false,
    lyricOffset: 0,
    lyricsEditorOpen: false,
};

const MODES = ['single', 'list', 'shuffle'];
const MODE_ICONS = { single: '🔂', list: '🔁', shuffle: '🔀' };
/** Circular accent ring colours sampled from the wallpaper (see sampleAccent). */
let accentCanvas = null;

/* --------------------------------------------------------------- bootstrap */

document.addEventListener('DOMContentLoaded', boot);

async function boot() {
    bindStaticEvents();
    state.api = await waitForApi();

    if (!state.api) {
        showToast('error', 'pywebview bridge unavailable');
        return;
    }

    try {
        await loadTranslations();
        state.appInfo = await state.api.get_app_info();
        await loadSettings();
        await refreshLoginStatus();
        watchSystemTheme();
        initMediaSession();
        initSleepTimer();
        initCrossfade();
        initSearchSuggest();
        updateStatus('ready');
        renderAbout();
        await loadPlayerExtras();
        await restoreSession();
        startSessionTimer();
    } catch (err) {
        console.error(err);
        showToast('error', t('app.init_failed', { message: err.message || err }));
    }
}

/** Resolve once the pywebview API object exists (or null after a timeout). */
function apiReady() {
    return !!(window.pywebview && window.pywebview.api
        && typeof window.pywebview.api.get_settings === 'function');
}

/**
 * Wait until the exposed Python API is actually callable.
 *
 * `window.pywebview.api` exists before pywebview has injected the methods, so
 * polling for the object alone makes the first calls silently resolve to
 * undefined (empty translations, no settings).  We listen for the documented
 * `pywebviewready` event and additionally poll for a known method.
 */
function waitForApi(timeoutMs = 15000) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = () => {
            if (settled || !apiReady()) return;
            settled = true;
            resolve(window.pywebview.api);
        };
        window.addEventListener('pywebviewready', finish);
        const started = Date.now();
        (function check() {
            finish();
            if (settled) return;
            if (Date.now() - started > timeoutMs) {
                settled = true;
                return resolve(null);
            }
            setTimeout(check, 50);
        })();
    });
}

/* -------------------------------------------------------------------- i18n */

async function loadTranslations() {
    let translations = await state.api.get_translations();
    if (!translations || !Object.keys(translations).length) {
        // The bridge is up but answered with nothing: retry once before giving
        // up, then surface the problem instead of showing raw translation keys.
        await new Promise((resolve) => setTimeout(resolve, 250));
        translations = await state.api.get_translations();
    }
    state.translations = translations || {};
    if (!Object.keys(state.translations).length) {
        console.error('i18n catalogue is empty');
        showToast('error', 'i18n catalogue unavailable');
    }
    applyTranslations();
}

function t(key, params) {
    let text = state.translations[key];
    if (text === undefined || text === null) text = key;
    if (params) {
        text = text.replace(/\{(\w+)\}/g, (match, name) =>
            (params[name] !== undefined && params[name] !== null) ? params[name] : match);
    }
    return text;
}

function applyTranslations() {
    document.querySelectorAll('[data-i18n]').forEach((el) => {
        el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
        el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    document.querySelectorAll('[data-i18n-title]').forEach((el) => {
        el.title = t(el.dataset.i18nTitle);
    });
    document.documentElement.lang = (state.settings && state.settings.ui.language === 'en_us')
        ? 'en' : 'zh-CN';
    // One broken renderer must not take the whole window down: this runs first
    // in boot(), so a ReferenceError here used to skip the login badge, the
    // settings and the whole player (the app looked logged out and dead).
    safeRender('refreshDynamicTexts', refreshDynamicTexts);
    safeRender('renderAbout', renderAbout);
    safeRender('updateThemeButton', updateThemeButton);
    safeRender('updateModeButton', updateModeButton);
    safeRender('renderNowPlaying', renderNowPlaying);
    safeRender('renderSourceHeader', renderSourceHeader);
}

/** Run a renderer and report a failure instead of propagating it. */
function safeRender(name, fn) {
    try {
        fn();
    } catch (err) {
        console.error('render ' + name + ' failed:', err);
    }
}

/** Re-render everything that was generated by JS (not static markup). */
function refreshDynamicTexts() {
    document.querySelectorAll('.pill[data-status]').forEach((pill) => {
        pill.textContent = t('download.status_' + pill.dataset.status);
    });
    document.querySelectorAll('.queue-item').forEach((item) => {
        const statusEl = item.querySelector('.queue-status');
        if (statusEl && statusEl.dataset.status) {
            statusEl.textContent = t('download.status_' + statusEl.dataset.status);
        }
        const reasonEl = item.querySelector('.reason');
        if (reasonEl && reasonEl.dataset.reason) {
            reasonEl.textContent = failureReason(reasonEl.dataset.reason);
        }
    });
    updateSelectionCount();
    if (state.playlist) renderPlaylist(state.playlist);
    updateStatusPill();
    renderTrackList();
    renderLyricSource();
}

function failureReason(code) {
    if (!code) return '';
    const short = String(code).split(':').pop();
    const key = 'error.reason.' + short;
    const text = t(key);
    return text === key ? String(code) : text;
}

/* ------------------------------------------------------------------- utils */

function fmtBytes(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function fmtSpeed(bytesPerSecond) {
    return bytesPerSecond > 0 ? `${fmtBytes(bytesPerSecond)}/s` : '';
}

function fmtDuration(ms) {
    if (!ms) return '';
    const total = Math.round(ms / 1000);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function fmtTime(seconds) {
    if (!seconds || !isFinite(seconds) || seconds < 0) return '0:00';
    const total = Math.floor(seconds);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

/** Human name of an audio level; unknown levels render as nothing. */
function levelText(level) {
    if (!level) return '';
    const key = 'download.level_' + level;
    const text = t(key);
    return text === key ? '' : text;
}

function showToast(type, message, timeoutMs = 3200) {
    if (!message) return;
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(6px)';
        setTimeout(() => toast.remove(), 220);
    }, timeoutMs);
}

function showError(payload, fallbackKey) {
    if (!payload) return;
    let message = payload.error_key ? t(payload.error_key) : '';
    if (payload.message && payload.message !== payload.error_key) {
        message = message ? `${message}: ${payload.message}` : payload.message;
    }
    showToast('error', message || t(fallbackKey || 'common.error'));
}

function openModal(id) { document.getElementById(id).hidden = false; }
function closeModal(id) {
    document.getElementById(id).hidden = true;
    if (id === 'login-modal' && state.api) state.api.cancel_login();
}

function confirmDialog(messageKey) {
    return new Promise((resolve) => {
        document.getElementById('confirm-message').textContent = t(messageKey);
        state.confirmResolve = resolve;
        openModal('confirm-modal');
    });
}

function resolveConfirm(result) {
    closeModal('confirm-modal');
    if (state.confirmResolve) {
        state.confirmResolve(result);
        state.confirmResolve = null;
    }
}

/* ---------------------------------------------------------------- settings */

async function loadSettings() {
    state.settings = await state.api.get_settings();
    player.mode = state.settings.playback.play_mode || 'list';
    player.volume = state.settings.playback.volume;
    player.crossfadeDuration = state.settings.playback.crossfade_duration || 0;
    player.sleepTimer.minutes = state.settings.playback.sleep_timer_minutes || 0;
    applyAppearance(state.settings);
    fillSettingsForm();
}

function fillSettingsForm() {
    const s = state.settings;
    if (!s) return;
    setValue('quality-select', s.download.quality);
    setValue('concurrent-select', String(s.download.max_concurrent));
    setChecked('overwrite-check', s.download.overwrite);
    setValue('default-quality-select', s.download.quality);
    setValue('max-concurrent-select', String(s.download.max_concurrent));
    setValue('theme-select', s.ui.theme);
    setChecked('overwrite-files-check', s.download.overwrite);
    setChecked('download-lyrics-check', s.download.download_lyrics);
    setChecked('lyrics-translation-check', s.download.lyrics_translation);
    setChecked('lyrics-check', s.download.download_lyrics);
    setChecked('remember-login-check', s.auth.remember_login);
    setChecked('debug-check', !!s.debug);
    document.getElementById('download-dir-input').value = s.download.download_dir || '';

    // playback
    setValue('priority-select', s.playback.prefer_online ? 'online' : 'local');
    setValue('online-quality-select', s.playback.online_quality);
    setChecked('resume-playback-check', s.playback.resume_playback);
    setChecked('report-play-check', s.playback.report_play_count);
    setChecked('show-translation-check', s.playback.show_translation);
    setValue('crossfade-duration', s.playback.crossfade_duration || 0);
    updateCrossfadeLabel();
    setValue('exclusive-mode-select', s.playback.exclusive_mode || 'off');
    setValue('sleep-timer-select', String(s.playback.sleep_timer_minutes || 0));

    // desktop lyrics
    setValue('desktop-lyrics-font-size', s.ui.desktop_lyrics_font_size || 18);
    setValue('desktop-lyrics-color', s.ui.desktop_lyrics_color || '#ffffff');
    setValue('desktop-lyrics-opacity', s.ui.desktop_lyrics_opacity || 0.9);
    updateDesktopLyricsOpacityLabel();
    setChecked('desktop-lyrics-translation-check', s.ui.desktop_lyrics_show_translation !== false);

    // appearance
    document.getElementById('background-input').value = s.ui.background_image || '';
    setValue('background-blur', s.ui.background_blur);
    setValue('background-dim', s.ui.background_dim);
    setChecked('glass-check', s.ui.glass);
    setChecked('accent-check', s.ui.accent_from_background);
    updateRangeLabels();

    fillLanguageSelect(s.ui.language);
    renderFolderList();
    setTheme(s.ui.theme);
}

function updateRangeLabels() {
    const blur = document.getElementById('background-blur');
    const dim = document.getElementById('background-dim');
    const blurLabel = document.getElementById('blur-value');
    const dimLabel = document.getElementById('dim-value');
    if (blurLabel && blur) blurLabel.textContent = `${blur.value} px`;
    if (dimLabel && dim) dimLabel.textContent = `${dim.value} %`;
}

function fillLanguageSelect(current) {
    const select = document.getElementById('language-select');
    const languages = { zh_cn: '简体中文', en_us: 'English' };
    select.innerHTML = '';
    Object.keys(languages).forEach((code) => {
        const option = document.createElement('option');
        option.value = code;
        option.textContent = languages[code];
        select.appendChild(option);
    });
    select.value = current || 'zh_cn';
}

function renderFolderList() {
    const container = document.getElementById('folder-list');
    if (!container) return;
    const directories = (state.settings && state.settings.playback.local_dirs) || [];
    container.innerHTML = '';
    if (!directories.length) {
        const empty = document.createElement('p');
        empty.className = 'muted';
        empty.textContent = t('settings.no_local_folder');
        container.appendChild(empty);
        return;
    }
    directories.forEach((directory) => {
        const row = document.createElement('div');
        row.className = 'folder-row';
        const label = document.createElement('span');
        label.textContent = directory;
        label.title = directory;
        const remove = document.createElement('button');
        remove.className = 'btn btn-sm btn-ghost';
        remove.type = 'button';
        remove.textContent = '✕';
        remove.title = t('common.remove');
        remove.addEventListener('click', () => removeLocalFolder(directory));
        row.append(label, remove);
        container.appendChild(row);
    });
}

function setValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value;
}

function setChecked(id, value) {
    const el = document.getElementById(id);
    if (el) el.checked = !!value;
}

const THEME_MODES = ['light', 'dark', 'system'];
const THEME_ICONS = { light: '☀', dark: '☾', system: '◐' };

/**
 * Apply a theme mode to the page, the header toggle and (when persisting) the
 * OS window chrome via the bridge, so the title bar never lags behind.
 */
function setTheme(theme, persist) {
    const mode = THEME_MODES.indexOf(theme) >= 0 ? theme : 'system';
    state.themeMode = mode;
    const applied = mode === 'system'
        ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
        : mode;
    document.documentElement.setAttribute('data-theme', applied === 'dark' ? 'dark' : 'light');
    document.documentElement.dataset.themeMode = mode;
    const select = document.getElementById('theme-select');
    if (select && select.value !== mode) select.value = mode;
    updateThemeButton();
    if (persist && state.api) state.api.set_theme(mode);
}

function updateThemeButton() {
    const button = document.getElementById('btn-theme');
    if (!button) return;
    button.textContent = THEME_ICONS[state.themeMode] || THEME_ICONS.system;
    const label = `${t('settings.theme')}: ${t('settings.theme_' + state.themeMode)}`;
    button.title = label;
    button.setAttribute('aria-label', label);
}

function cycleTheme() {
    const next = THEME_MODES[(THEME_MODES.indexOf(state.themeMode) + 1) % THEME_MODES.length];
    setTheme(next, true);
}

function watchSystemTheme() {
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => {
        // Only `system` follows the OS; an explicit light/dark choice wins.
        if (state.themeMode === 'system') setTheme('system', true);
    };
    if (query.addEventListener) query.addEventListener('change', handler);
    else if (query.addListener) query.addListener(handler);
}

/* ------------------------------------------------------------- appearance */

/** Push wallpaper / blur / glass settings into the CSS variables. */
function applyAppearance(settings) {
    const ui = (settings && settings.ui) || {};
    const url = ui.background_url || '';
    const root = document.documentElement;
    if (url && /^https?:\/\//.test(url)) {
        root.style.setProperty('--wallpaper', `url("${url}")`);
        document.body.classList.remove('no-wallpaper');
        if (ui.accent_from_background !== false) sampleAccent(url);
        else clearAccent();
    } else {
        root.style.removeProperty('--wallpaper');
        document.body.classList.add('no-wallpaper');
        clearAccent();
    }
    root.style.setProperty('--wallpaper-blur', `${ui.background_blur != null ? ui.background_blur : 24}px`);
    root.style.setProperty('--wallpaper-dim', `${(ui.background_dim != null ? ui.background_dim : 30) / 100}`);
    root.dataset.glass = ui.glass === false ? 'off' : 'on';
}

/**
 * Pull a usable accent colour out of the wallpaper.
 *
 * Purely cosmetic: the canvas is only read when the image is same-origin
 * friendly (the media server sends `Access-Control-Allow-Origin: *`), and any
 * failure keeps the built-in red.
 */
function sampleAccent(url) {
    try {
        const image = new Image();
        image.crossOrigin = 'anonymous';
        image.onload = () => {
            try {
                accentCanvas = accentCanvas || document.createElement('canvas');
                accentCanvas.width = 24;
                accentCanvas.height = 24;
                const context = accentCanvas.getContext('2d');
                context.drawImage(image, 0, 0, 24, 24);
                const data = context.getImageData(0, 0, 24, 24).data;
                let r = 0; let g = 0; let b = 0; let n = 0;
                for (let i = 0; i < data.length; i += 4) {
                    if (data[i + 3] < 128) continue;
                    r += data[i]; g += data[i + 1]; b += data[i + 2]; n += 1;
                }
                if (!n) return clearAccent();
                const hex = (value) => Math.round(value / n).toString(16).padStart(2, '0');
                const colour = `#${hex(r)}${hex(g)}${hex(b)}`;
                document.documentElement.style.setProperty('--accent', colour);
            } catch (err) {
                clearAccent();
            }
        };
        image.onerror = clearAccent;
        image.src = url;
    } catch (err) {
        clearAccent();
    }
}

function clearAccent() {
    document.documentElement.style.removeProperty('--accent');
}

async function saveSettings() {
    const payload = {
        download: {
            quality: document.getElementById('default-quality-select').value,
            max_concurrent: parseInt(document.getElementById('max-concurrent-select').value, 10),
            overwrite: document.getElementById('overwrite-files-check').checked,
            download_lyrics: document.getElementById('download-lyrics-check').checked,
            lyrics_translation: document.getElementById('lyrics-translation-check').checked,
            download_dir: document.getElementById('download-dir-input').value,
        },
        playback: {
            prefer_online: document.getElementById('priority-select').value === 'online',
            online_quality: document.getElementById('online-quality-select').value,
            resume_playback: document.getElementById('resume-playback-check').checked,
            report_play_count: document.getElementById('report-play-check').checked,
            show_translation: document.getElementById('show-translation-check').checked,
            crossfade_duration: parseFloat(document.getElementById('crossfade-duration').value),
            exclusive_mode: document.getElementById('exclusive-mode-select').value,
            sleep_timer_minutes: parseInt(document.getElementById('sleep-timer-select').value, 10),
        },
        ui: {
            language: document.getElementById('language-select').value,
            theme: document.getElementById('theme-select').value,
            background_blur: parseInt(document.getElementById('background-blur').value, 10),
            background_dim: parseInt(document.getElementById('background-dim').value, 10),
            glass: document.getElementById('glass-check').checked,
            accent_from_background: document.getElementById('accent-check').checked,
            desktop_lyrics_font_size: parseInt(document.getElementById('desktop-lyrics-font-size').value, 10),
            desktop_lyrics_color: document.getElementById('desktop-lyrics-color').value,
            desktop_lyrics_opacity: parseFloat(document.getElementById('desktop-lyrics-opacity').value),
            desktop_lyrics_show_translation: document.getElementById('desktop-lyrics-translation-check').checked,
        },
        auth: {
            remember_login: document.getElementById('remember-login-check').checked,
        },
        debug: document.getElementById('debug-check').checked,
    };

    for (const category of ['download', 'playback', 'ui', 'auth']) {
        const result = await state.api.update_settings(category, payload[category]);
        if (result && result.success) state.settings = result.settings;
    }
    const debugResult = await state.api.update_settings('debug', { debug: payload.debug });
    if (debugResult && debugResult.success) state.settings = debugResult.settings;

    applyAppearance(state.settings);
    fillSettingsForm();
    setValue('quality-select', payload.download.quality);
    setValue('concurrent-select', String(payload.download.max_concurrent));
    setChecked('overwrite-check', payload.download.overwrite);
    applyPlaybackSettings();
    showToast('success', t('settings.saved'));
    closeModal('settings-modal');
}

/** Re-read the playback settings after they changed. */
function applyPlaybackSettings() {
    const playback = state.settings && state.settings.playback;
    if (!playback) return;
    player.mode = playback.play_mode || player.mode;
    player.volume = playback.volume;
    player.crossfadeDuration = playback.crossfade_duration || 0;
    player.sleepTimer.minutes = playback.sleep_timer_minutes || 0;
    audio.volume = player.volume;
    syncVolumeSlider();
    updateModeButton();
    renderTrackList();
    updateCrossfadeLabel();
    // Update media server crossfade setting
    if (state.api && state.api.set_crossfade_duration) {
        state.api.set_crossfade_duration(player.crossfadeDuration);
    }
    // Reset sleep timer with new setting
    if (player.sleepTimer.minutes > 0) {
        startSleepTimer(player.sleepTimer.minutes, 'pause');
    } else {
        clearSleepTimer();
    }
}

async function resetSettings() {
    if (!await confirmDialog('settings.reset_confirm')) return;
    const result = await state.api.reset_settings();
    if (result && result.success) state.settings = result.settings;
    applyAppearance(state.settings);
    fillSettingsForm();
    applyPlaybackSettings();
    showToast('info', t('settings.reset_done'));
}

async function chooseDirectory() {
    const dir = await state.api.choose_directory();
    if (dir) {
        document.getElementById('download-dir-input').value = dir;
        if (state.settings) state.settings.download.download_dir = dir;
    }
}

async function changeLanguage(code) {
    const result = await state.api.set_language(code);
    if (result && result.success) {
        state.translations = result.translations || state.translations;
        if (state.settings) state.settings.ui.language = result.language;
        applyTranslations();
    }
}

/* --------------------------------------------------------------- wallpaper */

async function chooseBackground() {
    const result = await state.api.pick_background_image();
    if (!result.success) {
        if (!result.cancelled) showError(result, 'error.unknown');
        return;
    }
    state.settings = result.settings;
    applyAppearance(state.settings);
    fillSettingsForm();
    showToast('success', t('settings.background_set'));
}

async function clearBackground() {
    const result = await state.api.clear_background_image();
    if (result.success) {
        state.settings = result.settings;
        applyAppearance(state.settings);
        fillSettingsForm();
        showToast('info', t('settings.background_cleared'));
    }
}

/* ------------------------------------------------------------------- login */

async function refreshLoginStatus() {
    const status = await state.api.get_login_status();
    updateLoginBadge(status || { is_logged_in: false, user: null });
}

function updateLoginBadge(status) {
    status = status || {};
    const button = document.getElementById('btn-login');
    if (!button) return;
    if (status.is_logged_in && status.user) {
        button.classList.remove('btn-primary');
        button.classList.add('btn-secondary');
        button.innerHTML = '';
        const name = document.createElement('span');
        name.textContent = status.user.nickname || t('login.logged_in_as', { name: '' });
        button.appendChild(name);
        const badge = document.createElement('span');
        badge.className = 'badge';
        badge.textContent = status.user.vip_type > 0 ? 'VIP' : '';
        if (badge.textContent) button.appendChild(badge);
        button.onclick = async () => {
            if (await confirmDialog('login.logout_confirm')) {
                await state.api.logout();
                player.playlists = [];
                await refreshLoginStatus();
            }
        };
        button.title = t('login.logged_in_as', { name: status.user.nickname || '' });
    } else {
        button.classList.add('btn-primary');
        button.classList.remove('btn-secondary');
        button.textContent = t('login.title');
        button.title = '';
        button.onclick = openLoginModal;
    }
}

function openLoginModal() {
    openModal('login-modal');
    switchLoginTab('qrcode');
}

function switchLoginTab(tab) {
    document.querySelectorAll('#login-tabs .tab').forEach((el) =>
        el.classList.toggle('is-active', el.dataset.tab === tab));
    document.querySelectorAll('#login-modal .tab-panel').forEach((el) =>
        el.classList.toggle('is-active', el.dataset.panel === tab));
    if (tab === 'qrcode') state.api.login_qrcode();
}

async function refreshQrcode() {
    const result = await state.api.refresh_qrcode();
    if (!result || !result.success) showToast('error', t('login.login_error'));
}

async function sendPhoneCode() {
    const phone = document.getElementById('phone-input').value.trim();
    if (!/^1\d{10}$/.test(phone)) {
        showToast('warning', t('login.invalid_phone'));
        return;
    }
    const button = document.getElementById('btn-send-code');
    // Guard against double submits: every extra request extends NetEase's
    // cooldown for the number, which is what produces "操作过于频繁".
    if (button && button.disabled) return;
    if (button) button.disabled = true;
    const result = await state.api.send_phone_code(phone);
    if (result.success) {
        showToast('success', t('login.code_sent'));
        startCountdown();
    } else if (result.throttled || result.error_key === 'login.code_too_frequent') {
        showToast('error', t('login.code_too_frequent', { message: result.message || '' }), 9000);
        startCountdown(180);
    } else {
        showToast('error', t('login.code_send_failed', { message: result.message || '' }));
        startCountdown(60);
    }
}

function startCountdown(seconds = 60) {
    const button = document.getElementById('btn-send-code');
    if (!button) return;
    if (state.phoneTimer) clearInterval(state.phoneTimer);
    button.disabled = true;
    let remaining = seconds;
    const tick = () => {
        if (remaining <= 0) {
            clearInterval(state.phoneTimer);
            state.phoneTimer = null;
            button.disabled = false;
            button.textContent = t('login.send_code');
            return;
        }
        button.textContent = t('login.code_countdown', { seconds: remaining });
        remaining -= 1;
    };
    tick();
    state.phoneTimer = setInterval(tick, 1000);
}

async function doPhoneLogin() {
    const phone = document.getElementById('phone-input').value.trim();
    const code = document.getElementById('code-input').value.trim();
    if (!phone || !code) {
        showToast('warning', t('login.enter_phone_and_code'));
        return;
    }
    const result = await state.api.login_phone(phone, code);
    if (!result.success) {
        showToast('error', result.message ? `${t('login.login_failed')}: ${result.message}` : t('login.login_failed'));
    }
}

async function doCookieLogin() {
    const cookie = document.getElementById('cookie-input').value.trim();
    if (!cookie) {
        showToast('warning', t('login.enter_cookie'));
        return;
    }
    const result = await state.api.login_cookie(cookie);
    if (!result.success) showToast('error', t('login.cookie_invalid'));
}

/* ---------------------------------------------------------------- playlist */

async function fetchPlaylist() {
    const input = document.getElementById('playlist-input');
    const value = input.value.trim();
    if (!value) {
        showToast('warning', t('playlist.invalid_input'));
        return;
    }
    const button = document.getElementById('btn-fetch');
    button.disabled = true;
    button.textContent = t('playlist.fetching');
    updateStatus('fetching_playlist');

    try {
        const result = await state.api.fetch_playlist(value);
        if (!result.success) {
            showError(result, 'playlist.fetch_failed');
            return;
        }
        state.playlist = result.playlist;
        state.tracks = result.tracks || [];
        state.selected.clear();
        state.tasks.clear();
        document.getElementById('download-queue').innerHTML =
            `<p class="empty" id="empty-queue">${t('download.queue_empty')}</p>`;
        renderPlaylist(result.playlist);
        renderTracks(state.tracks);
        document.getElementById('playlist-card').hidden = false;
        document.getElementById('song-card').hidden = false;
        document.getElementById('btn-start').disabled = false;
        updateSelectionCount();
        showToast('success', t('playlist.fetch_success', { count: state.tracks.length }));
    } catch (err) {
        showToast('error', t('playlist.fetch_failed') + ': ' + (err.message || err));
    } finally {
        button.disabled = false;
        button.textContent = t('playlist.fetch_btn');
        updateStatus('ready');
    }
}

function renderPlaylist(playlist) {
    if (!playlist) return;
    document.getElementById('playlist-name').textContent = playlist.name;
    document.getElementById('playlist-creator').textContent =
        `${t('playlist.creator')}: ${playlist.creator}`;
    document.getElementById('playlist-count').textContent =
        `${t('playlist.track_count')}: ${playlist.track_count}`;
    const cover = document.getElementById('playlist-cover');
    cover.classList.toggle('is-empty', !playlist.cover_url);
    if (playlist.cover_url) {
        cover.src = playlist.cover_url;
        cover.onerror = () => {
            cover.removeAttribute('src');
            cover.classList.add('is-empty');
        };
    } else {
        cover.removeAttribute('src');
    }
}

function renderTracks(tracks) {
    const list = document.getElementById('song-list');
    list.innerHTML = '';
    const fragment = document.createDocumentFragment();
    tracks.forEach((track) => {
        const row = document.createElement('div');
        row.className = 'song-row';
        row.dataset.index = track.index;
        row.dataset.songId = track.id;

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.setAttribute('aria-label', track.name);
        checkbox.addEventListener('change', () => toggleSong(track.index, checkbox.checked));

        const index = document.createElement('span');
        index.className = 'idx';
        index.textContent = String(track.index + 1);

        const title = document.createElement('div');
        title.className = 'song-title';
        const nameLine = document.createElement('div');
        nameLine.className = 'song-name-line';
        const name = document.createElement('span');
        name.className = 'name';
        name.textContent = track.name;
        nameLine.appendChild(name);
        if (track.vip) {
            const badge = document.createElement('span');
            badge.className = 'badge';
            badge.textContent = 'VIP';
            nameLine.appendChild(badge);
        }
        const artist = document.createElement('span');
        artist.className = 'song-artist';
        artist.textContent = track.artists;
        title.append(nameLine, artist);

        const pill = document.createElement('span');
        pill.className = 'pill waiting';
        pill.dataset.status = 'waiting';
        pill.textContent = t('download.status_waiting');

        row.append(checkbox, index, title, pill);
        fragment.appendChild(row);
    });
    list.appendChild(fragment);
}

function toggleSong(index, checked) {
    if (checked) state.selected.add(index);
    else state.selected.delete(index);
    updateSelectionCount();
}

function selectAll() {
    state.selected = new Set(state.tracks.map((track) => track.index));
    document.querySelectorAll('#song-list input[type="checkbox"]').forEach((cb) => { cb.checked = true; });
    updateSelectionCount();
}

function deselectAll() {
    state.selected.clear();
    document.querySelectorAll('#song-list input[type="checkbox"]').forEach((cb) => { cb.checked = false; });
    updateSelectionCount();
}

function updateSelectionCount() {
    const counter = document.getElementById('tracks-count');
    if (!counter) return;
    if (!state.tracks.length) {
        counter.textContent = '';
        return;
    }
    if (state.selected.size) {
        counter.textContent = '· ' + t('download.selected_count',
            { count: state.selected.size, total: state.tracks.length });
    } else {
        counter.textContent = '· ' + state.tracks.length;
    }
}

/* ---------------------------------------------------------------- download */

async function startDownload() {
    if (!state.selected.size) {
        showToast('warning', t('status.no_songs_selected'));
        return;
    }
    const options = downloadOptions({
        indices: Array.from(state.selected).sort((a, b) => a - b),
        quality: document.getElementById('quality-select').value,
        max_concurrent: parseInt(document.getElementById('concurrent-select').value, 10),
        overwrite: document.getElementById('overwrite-check').checked,
        download_lyrics: document.getElementById('lyrics-check').checked,
    });
    try {
        const result = await state.api.start_download(options);
        if (result.success) {
            state.downloading = true;
            state.paused = false;
            updateDownloadControls();
            resetProgress(result.total);
        } else {
            showError(result, 'download.start_failed');
        }
    } catch (err) {
        showToast('error', t('download.start_failed', { message: err.message || err }));
    }
}

/** The download options every entry point shares (tab or player). */
function downloadOptions(overrides) {
    const download = (state.settings && state.settings.download) || {};
    return Object.assign({
        quality: download.quality,
        max_concurrent: download.max_concurrent,
        overwrite: download.overwrite,
        download_lyrics: download.download_lyrics,
        lyrics_translation: download.lyrics_translation !== false,
        download_dir: download.download_dir || '',
    }, overrides || {});
}

async function togglePause() {
    if (state.paused) {
        await state.api.resume_download();
        state.paused = false;
        showToast('info', t('status.download_resumed'));
    } else {
        await state.api.pause_download();
        state.paused = true;
        showToast('info', t('status.download_paused'));
    }
    updateDownloadControls();
}

async function cancelDownload() {
    if (!await confirmDialog('error.cancel_download_confirm')) return;
    await state.api.cancel_download();
    state.downloading = false;
    state.paused = false;
    updateDownloadControls();
    showToast('info', t('status.download_cancelled'));
}

async function openFolder() {
    const ok = await state.api.open_download_folder();
    if (!ok) showToast('error', t('error.permission'));
}

function updateDownloadControls() {
    document.getElementById('btn-start').disabled = state.downloading;
    document.getElementById('btn-pause').disabled = !state.downloading;
    document.getElementById('btn-cancel').disabled = !state.downloading;
    document.getElementById('btn-pause').textContent =
        state.paused ? t('download.resume_btn') : t('download.pause_btn');
}

function resetProgress(total) {
    state.completed = 0;
    state.total = total || 0;
    document.getElementById('progress-fill').style.width = '0%';
    document.getElementById('progress-text').textContent = `0% (0/${state.total})`;
    document.getElementById('overall-speed').textContent = '';
    updateStatus('downloading', { current: 0, total: state.total });
}

function updateStatusPill() {
    if (state.downloading) {
        updateStatus('downloading', { current: state.completed || 0, total: state.total || 0 });
    }
}

function updateStatus(key, params) {
    const text = document.getElementById('status-text');
    if (text) text.textContent = t('status.' + key, params || {});
}

function updateSongRow(task) {
    const row = document.querySelector(`.song-row[data-index="${task.index}"]`);
    if (!row) return;
    const pill = row.querySelector('.pill');
    if (!pill) return;
    pill.dataset.status = task.status;
    pill.className = `pill ${task.status}`;
    pill.textContent = t('download.status_' + task.status);
    if (task.status === 'completed' || task.status === 'skipped') {
        const checkbox = row.querySelector('input[type="checkbox"]');
        if (checkbox) checkbox.checked = false;
        state.selected.delete(Number(task.index));
        updateSelectionCount();
    }
}

function upsertQueueItem(task) {
    const queue = document.getElementById('download-queue');
    const empty = document.getElementById('empty-queue');
    if (empty) empty.remove();

    let item = state.tasks.get(task.song_id);
    if (!item || !item.isConnected) {
        item = document.createElement('div');
        item.className = 'queue-item';
        item.innerHTML = `
            <div class="queue-item-head">
                <span class="queue-item-title"></span>
                <span class="pill queue-status"></span>
            </div>
            <div class="progress-track"><div class="progress-fill"></div></div>
            <div class="queue-item-meta"><span class="queue-speed"></span><span class="queue-size"></span></div>
            <div class="reason" hidden></div>`;
        item.querySelector('.queue-item-title').textContent = `${task.artists} - ${task.name}`;
        queue.appendChild(item);
        state.tasks.set(task.song_id, item);
    }

    const fill = item.querySelector('.progress-fill');
    const statusEl = item.querySelector('.queue-status');
    const speedEl = item.querySelector('.queue-speed');
    const sizeEl = item.querySelector('.queue-size');
    const reasonEl = item.querySelector('.reason');

    fill.style.width = `${Math.min(100, task.progress || 0)}%`;
    statusEl.dataset.status = task.status;
    statusEl.className = `pill queue-status ${task.status}`;
    statusEl.textContent = t('download.status_' + task.status);

    const level = task.level ? ` · ${levelText(task.level)}` : '';
    speedEl.textContent = (task.status === 'downloading' ? fmtSpeed(task.speed) : '') + level;
    sizeEl.textContent = task.total_size
        ? `${fmtBytes(task.downloaded_size || 0)} / ${fmtBytes(task.total_size)}`
        : '';

    if (task.status === 'failed' && (task.error_code || task.error)) {
        reasonEl.hidden = false;
        reasonEl.dataset.reason = task.error_code || task.error;
        reasonEl.textContent = failureReason(task.error_code || task.error);
    } else {
        reasonEl.hidden = true;
    }
}

/* ==================================================================== */
/* player                                                               */
/* ==================================================================== */

const audio = document.getElementById('audio');

/** Switch between the download and the player view. */
function showView(name) {
    document.querySelectorAll('.top-tab').forEach((tab) =>
        tab.classList.toggle('is-active', tab.dataset.view === name));
    document.querySelectorAll('.view').forEach((view) =>
        view.classList.toggle('is-active', view.id === 'view-' + name));
    if (name === 'player' && !player.queue.length && !player.current) {
        selectSource(player.sourceTab);
    }
}

/* ------------------------------------------------------------- sources */

async function selectSource(source) {
    player.sourceTab = source;
    player.playlistOpen = null;
    player.currentLocalPlaylist = null;
    document.querySelectorAll('.source-tab').forEach((tab) =>
        tab.classList.toggle('is-active', tab.dataset.source === source));
    document.getElementById('local-actions').hidden = source !== 'local';
    document.getElementById('local-playlist-actions').hidden = source !== 'local_playlists';
    document.getElementById('btn-list-back').hidden = true;
    document.getElementById('btn-list-more').hidden = true;
    document.getElementById('search-type-tabs').hidden = true;

    if (source === 'playlists') await loadPlaylists();
    else if (source === 'liked') await loadLiked();
    else if (source === 'recommend') await loadRecommendPlaylists();
    else if (source === 'personal_fm') await loadPersonalFM();
    else if (source === 'new_songs') await loadNewSongs();
    else if (source === 'recommend_mv') await loadRecommendMVs();
    else if (source === 'toplist') await loadTopLists();
    else if (source === 'local_playlists') await loadLocalPlaylists();
    else if (source === 'local') await loadLocal();
    else if (source === 'recent') await loadRecent();
    else if (source === 'search') await showSearchResults();
}

async function loadPlaylists() {
    setListTitle(t('player.source_playlists'));
    const container = document.getElementById('playlist-list');
    container.innerHTML = `<p class="empty">${t('common.loading')}</p>`;
    document.getElementById('track-list').innerHTML = '';
    const result = await state.api.get_playlists();
    if (!result.success) {
        player.playlists = [];
        container.innerHTML = '';
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t(result.error_key || 'player.playlist_empty');
        container.appendChild(empty);
        return;
    }
    player.playlists = result.playlists || [];
    renderPlaylists(player.playlists);
}

function renderPlaylists(playlists) {
    const container = document.getElementById('playlist-list');
    container.innerHTML = '';
    if (!playlists.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t('player.playlist_empty');
        container.appendChild(empty);
        return;
    }
    const fragment = document.createDocumentFragment();
    playlists.forEach((playlist) => {
        const row = document.createElement('div');
        row.className = 'playlist-row';
        row.dataset.playlistId = playlist.id;
        const isOwn = playlist.creator_id === (player.currentUserId || '') || playlist.subscribed;
        const cover = document.createElement('img');
        cover.alt = '';
        if (playlist.cover_url) cover.src = playlist.cover_url;
        cover.onerror = () => cover.removeAttribute('src');
        const meta = document.createElement('div');
        meta.className = 'pl-meta';
        const name = document.createElement('span');
        name.className = 'pl-name';
        name.textContent = playlist.name;
        const sub = document.createElement('span');
        sub.className = 'pl-sub';
        sub.textContent = t('player.playlist_meta', {
            count: playlist.track_count, creator: playlist.creator || '-',
        });
        meta.append(name, sub);
        
        const actions = document.createElement('div');
        actions.className = 'playlist-actions';
        actions.style.display = 'none';
        
        if (isOwn) {
            // Edit button
            const editBtn = document.createElement('button');
            editBtn.className = 'btn btn-icon btn-sm';
            editBtn.type = 'button';
            editBtn.textContent = '✎';
            editBtn.title = t('player.playlist_edit');
            editBtn.addEventListener('click', (e) => { e.stopPropagation(); editPlaylist(playlist); });
            actions.appendChild(editBtn);
            
            // Subscribe/Unsubscribe
            const subBtn = document.createElement('button');
            subBtn.className = 'btn btn-icon btn-sm';
            subBtn.type = 'button';
            subBtn.textContent = playlist.subscribed ? '★' : '☆';
            subBtn.title = playlist.subscribed ? t('player.playlist_unsubscribe') : t('player.playlist_subscribe');
            subBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleSubscribePlaylist(playlist); });
            actions.appendChild(subBtn);
            
            // Delete button
            const delBtn = document.createElement('button');
            delBtn.className = 'btn btn-icon btn-sm';
            delBtn.type = 'button';
            delBtn.textContent = '🗑';
            delBtn.title = t('player.playlist_delete');
            delBtn.addEventListener('click', (e) => { e.stopPropagation(); deletePlaylist(playlist); });
            actions.appendChild(delBtn);
        } else if (playlist.subscribed !== undefined) {
            // Subscribe for non-owned
            const subBtn = document.createElement('button');
            subBtn.className = 'btn btn-icon btn-sm';
            subBtn.type = 'button';
            subBtn.textContent = playlist.subscribed ? '★' : '☆';
            subBtn.title = playlist.subscribed ? t('player.playlist_unsubscribe') : t('player.playlist_subscribe');
            subBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleSubscribePlaylist(playlist); });
            actions.appendChild(subBtn);
        }
        
        row.append(cover, meta, actions);
        
        // Show actions on hover
        row.addEventListener('mouseenter', () => { actions.style.display = 'flex'; });
        row.addEventListener('mouseleave', () => { actions.style.display = 'none'; });
        row.addEventListener('click', () => openPlaylist(playlist));
        fragment.appendChild(row);
    });
    container.appendChild(fragment);
    setListCount(playlists.length);
}

async function openPlaylist(playlist) {
    player.playlistOpen = playlist;
    document.getElementById('playlist-list').innerHTML =
        `<p class="empty">${t('common.loading')}</p>`;
    const result = await state.api.get_playlist_tracks(playlist.id);
    if (!result.success) {
        renderPlaylists(player.playlists);
        showError(result, 'playlist.fetch_failed');
        return;
    }
    document.getElementById('playlist-list').innerHTML = '';
    document.getElementById('btn-list-back').hidden = false;
    setListTitle(playlist.name);
    playTracksInto(result.tracks, { kind: 'playlist', id: playlist.id, name: playlist.name });
}

function backToPlaylists() {
    player.playlistOpen = null;
    document.getElementById('btn-list-back').hidden = true;
    renderPlaylists(player.playlists);
    setListTitle(t('player.source_playlists'));
}

/* ----------------------------------------------------------- playlist management */

async function createPlaylist() {
    const name = prompt(t('player.playlist_name_prompt'));
    if (!name) return;
    
    const result = await state.api.create_playlist(name, 0, '');
    if (result.success) {
        showToast('success', t('player.playlist_created', { name }));
        await loadPlaylists();
    } else {
        showError(result, 'player.create_failed');
    }
}

async function editPlaylist(playlist) {
    const name = prompt(t('player.playlist_rename'), playlist.name);
    if (!name || name === playlist.name) return;
    
    const result = await state.api.update_playlist(playlist.id, { name });
    if (result.success) {
        showToast('success', t('player.playlist_renamed', { name }));
        playlist.name = name;
        renderPlaylists(player.playlists);
    } else {
        showError(result, 'player.update_failed');
    }
}

async function deletePlaylist(playlist) {
    if (!confirm(t('player.playlist_delete_confirm', { name: playlist.name }))) return;
    
    const result = await state.api.delete_playlist([playlist.id]);
    if (result.success) {
        showToast('success', t('player.playlist_deleted', { name: playlist.name }));
        await loadPlaylists();
    } else {
        showError(result, 'player.delete_failed');
    }
}

async function toggleSubscribePlaylist(playlist) {
    const subscribe = !playlist.subscribed;
    const result = await state.api.subscribe_playlist(playlist.id, subscribe);
    if (result.success) {
        playlist.subscribed = subscribe;
        showToast('success', subscribe ? t('player.playlist_subscribed') : t('player.playlist_unsubscribed'));
        renderPlaylists(player.playlists);
    } else {
        showError(result, subscribe ? 'player.subscribe_failed' : 'player.unsubscribe_failed');
    }
}

async function loadLiked() {
    setListTitle(t('player.source_liked'));
    document.getElementById('playlist-list').innerHTML = '';
    const result = await state.api.get_liked_tracks();
    if (!result.success) {
        renderEmptyTracks(t(result.error_key || 'player.login_required'));
        return;
    }
    playTracksInto(result.tracks, { kind: 'liked', name: t('player.source_liked') });
}

/** One screenful of local tracks; ``more`` appends the next page. */
const LOCAL_PAGE = 200;

async function loadLocal(more) {
    document.getElementById('playlist-list').innerHTML = '';
    const input = document.getElementById('search-input');
    const keyword = (input && input.value ? input.value : '').trim();
    if (!more) {
        player.localOffset = 0;
        player.localTracks = [];
    }
    const result = await state.api.get_local_tracks(keyword, LOCAL_PAGE, player.localOffset);
    const page = result.tracks || [];
    player.localTracks = more ? player.localTracks.concat(page) : page;
    player.localOffset += page.length;
    player.localTotal = result.total || 0;
    player.localKeyword = keyword;

    setListTitle(keyword ? t('player.search_result', { keyword }) : t('player.source_local'));
    if (!player.localTracks.length) {
        renderEmptyTracks(keyword ? t('player.search_empty_local', { keyword })
            : t('player.no_local_music'));
        return;
    }
    playTracksInto(player.localTracks, {
        kind: 'local', name: keyword || t('player.source_local'),
    });
    document.getElementById('btn-list-more').hidden = player.localTracks.length >= player.localTotal;
}

async function loadRecent() {
    setListTitle(t('player.source_recent'));
    document.getElementById('playlist-list').innerHTML = '';
    const result = await state.api.get_recent_tracks(100);
    const tracks = (result.tracks || []).filter((track) => track.key);
    if (!tracks.length) {
        renderEmptyTracks(t('player.no_recent'));
        return;
    }
    playTracksInto(tracks, { kind: 'recent', name: t('player.source_recent') });
}

/* ----------------------------------------------------------- local playlists */

async function loadLocalPlaylists() {
    setListTitle(t('player.source_local_playlists'));
    document.getElementById('playlist-list').innerHTML = `<p class="empty">${t('common.loading')}</p>`;
    document.getElementById('track-list').innerHTML = '';
    document.getElementById('local-actions').hidden = true;
    document.getElementById('local-playlist-actions').hidden = false;
    document.getElementById('btn-list-back').hidden = true;
    document.getElementById('btn-list-more').hidden = true;

    const result = await state.api.get_local_playlists();
    if (!result.success) {
        const container = document.getElementById('playlist-list');
        container.innerHTML = '';
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t(result.error_key || 'error.unknown');
        container.appendChild(empty);
        return;
    }
    player.localPlaylists = result.playlists || [];
    player.localPlaylistSortOrder = result.sort_order || 'manual';
    renderLocalPlaylists(player.localPlaylists);
}

function renderLocalPlaylists(playlists) {
    const container = document.getElementById('playlist-list');
    container.innerHTML = '';
    if (!playlists.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t('player.no_local_playlists');
        container.appendChild(empty);
        const createBtn = document.createElement('button');
        createBtn.className = 'btn btn-primary btn-block';
        createBtn.type = 'button';
        createBtn.textContent = t('player.playlist_create');
        createBtn.addEventListener('click', createLocalPlaylist);
        container.appendChild(createBtn);
        return;
    }
    const fragment = document.createDocumentFragment();
    playlists.forEach((playlist) => {
        const row = document.createElement('div');
        row.className = 'playlist-row';
        row.dataset.playlistId = playlist.id;
        const cover = document.createElement('img');
        cover.alt = '';
        if (playlist.cover_url) cover.src = playlist.cover_url;
        cover.onerror = () => cover.removeAttribute('src');
        const meta = document.createElement('div');
        meta.className = 'pl-meta';
        const name = document.createElement('span');
        name.className = 'pl-name';
        name.textContent = playlist.name;
        const sub = document.createElement('span');
        sub.className = 'pl-sub';
        sub.textContent = t('player.playlist_meta', {
            count: playlist.tracks?.length || 0, creator: 'Local',
        });
        meta.append(name, sub);
        
        const actions = document.createElement('div');
        actions.className = 'playlist-actions';
        actions.style.display = 'none';
        
        // Edit
        const editBtn = document.createElement('button');
        editBtn.className = 'btn btn-icon btn-sm';
        editBtn.type = 'button';
        editBtn.textContent = '✎';
        editBtn.title = t('player.playlist_edit');
        editBtn.addEventListener('click', (e) => { e.stopPropagation(); editLocalPlaylist(playlist); });
        actions.appendChild(editBtn);
        
        // Delete
        const delBtn = document.createElement('button');
        delBtn.className = 'btn btn-icon btn-sm';
        delBtn.type = 'button';
        delBtn.textContent = '🗑';
        delBtn.title = t('player.playlist_delete');
        delBtn.addEventListener('click', (e) => { e.stopPropagation(); deleteLocalPlaylist(playlist); });
        actions.appendChild(delBtn);
        
        row.append(cover, meta, actions);
        
        row.addEventListener('mouseenter', () => { actions.style.display = 'flex'; });
        row.addEventListener('mouseleave', () => { actions.style.display = 'none'; });
        row.addEventListener('click', () => openLocalPlaylist(playlist));
        fragment.appendChild(row);
    });
    container.appendChild(fragment);
    setListCount(playlists.length);
}

async function createLocalPlaylist() {
    const name = prompt(t('player.playlist_name_prompt'));
    if (!name) return;
    
    const result = await state.api.create_local_playlist(name);
    if (result.success) {
        showToast('success', t('player.playlist_created', { name }));
        await loadLocalPlaylists();
    } else {
        showError(result, 'player.create_failed');
    }
}

async function editLocalPlaylist(playlist) {
    const name = prompt(t('player.playlist_rename'), playlist.name);
    if (!name || name === playlist.name) return;
    
    const result = await state.api.update_local_playlist(playlist.id, { name });
    if (result.success) {
        showToast('success', t('player.playlist_renamed', { name }));
        playlist.name = name;
        renderLocalPlaylists(player.localPlaylists);
    } else {
        showError(result, 'player.update_failed');
    }
}

async function deleteLocalPlaylist(playlist) {
    if (!confirm(t('player.playlist_delete_confirm', { name: playlist.name }))) return;
    
    const result = await state.api.delete_local_playlist(playlist.id);
    if (result.success) {
        showToast('success', t('player.playlist_deleted', { name: playlist.name }));
        await loadLocalPlaylists();
    } else {
        showError(result, 'player.delete_failed');
    }
}

async function openLocalPlaylist(playlist) {
    player.currentLocalPlaylist = playlist;
    document.getElementById('playlist-list').innerHTML = `<p class="empty">${t('common.loading')}</p>`;
    document.getElementById('track-list').innerHTML = '';
    document.getElementById('btn-list-back').hidden = false;
    document.getElementById('local-playlist-actions').hidden = true;
    setListTitle(playlist.name);
    
    // Load tracks
    renderLocalPlaylistTracks(playlist.tracks || []);
    document.getElementById('btn-list-more').hidden = true;
}

function renderLocalPlaylistTracks(tracks) {
    const container = document.getElementById('track-list');
    container.innerHTML = '';
    
    if (!tracks.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t('player.no_tracks');
        container.appendChild(empty);
        return;
    }
    
    const fragment = document.createDocumentFragment();
    tracks.forEach((track, index) => {
        const item = document.createElement('div');
        item.className = 'playlist-track-item';
        item.draggable = true;
        item.dataset.index = index;
        item.dataset.trackKey = track.key;
        
        const idx = document.createElement('span');
        idx.className = 'idx';
        idx.textContent = String(index + 1);
        
        const main = document.createElement('div');
        main.className = 'playlist-track-main';
        const name = document.createElement('div');
        name.className = 'playlist-track-name';
        name.textContent = track.name || '-';
        const sub = document.createElement('div');
        sub.className = 'playlist-track-sub';
        sub.textContent = [track.artists, track.album].filter(Boolean).join(' · ');
        main.append(name, sub);
        
        const actions = document.createElement('div');
        actions.className = 'playlist-track-actions';
        
        const removeBtn = document.createElement('button');
        removeBtn.className = 'btn btn-icon btn-sm';
        removeBtn.type = 'button';
        removeBtn.textContent = '🗑';
        removeBtn.title = t('player.playlist_remove_tracks');
        removeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            removeTrackFromLocalPlaylist(player.currentLocalPlaylist, track, item);
        });
        actions.appendChild(removeBtn);
        
        item.append(idx, main, actions);
        
        // Drag and drop
        item.addEventListener('dragstart', handleLocalPlaylistTrackDragStart);
        item.addEventListener('dragover', handleLocalPlaylistTrackDragOver);
        item.addEventListener('drop', handleLocalPlaylistTrackDrop);
        item.addEventListener('dragend', handleLocalPlaylistTrackDragEnd);
        
        fragment.appendChild(item);
    });
    container.appendChild(fragment);
}

let localPlaylistDragSource = null;

function handleLocalPlaylistTrackDragStart(e) {
    localPlaylistDragSource = e.currentTarget;
    e.currentTarget.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
}

function handleLocalPlaylistTrackDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const target = e.currentTarget.closest('.playlist-track-item');
    if (target) target.classList.add('drag-over');
}

function handleLocalPlaylistTrackDrop(e) {
    e.preventDefault();
    const target = e.currentTarget.closest('.playlist-track-item');
    if (!target || target === localPlaylistDragSource) return;
    target.classList.remove('drag-over');
    reorderLocalPlaylistTracks(localPlaylistDragSource, target);
}

function handleLocalPlaylistTrackDragEnd(e) {
    e.currentTarget.classList.remove('dragging');
    document.querySelectorAll('.playlist-track-item').forEach(el => el.classList.remove('drag-over'));
}

async function reorderLocalPlaylistTracks(source, target) {
    const fromIndex = parseInt(source.dataset.index, 10);
    const toIndex = parseInt(target.dataset.index, 10);
    const result = await state.api.reorder_local_playlist_tracks(player.currentLocalPlaylist.id, fromIndex, toIndex);
    if (result.success) {
        // Re-render
        renderLocalPlaylistTracks(player.currentLocalPlaylist.tracks);
    } else {
        showError(result, 'player.reorder_failed');
    }
}

async function removeTrackFromLocalPlaylist(playlist, track, item) {
    const result = await state.api.remove_tracks_from_local_playlist(playlist.id, [track.key]);
    if (result.success) {
        item.remove();
        showToast('success', t('player.track_removed'));
        // Re-index
        const items = document.querySelectorAll('#track-list .playlist-track-item');
        items.forEach((el, idx) => {
            el.dataset.index = idx;
            el.querySelector('.idx').textContent = String(idx + 1);
        });
    } else {
        showError(result, 'player.remove_failed');
    }
}

/* ----------------------------------------------------------- playlist management */

async function createPlaylist() {
    const name = prompt(t('player.playlist_name_prompt'));
    if (!name) return;
    
    const result = await state.api.create_playlist(name, 0, '');
    if (result.success) {
        showToast('success', t('player.playlist_created', { name }));
        await loadPlaylists();
    } else {
        showError(result, 'player.create_failed');
    }
}

async function editPlaylist(playlist) {
    const name = prompt(t('player.playlist_rename'), playlist.name);
    if (!name || name === playlist.name) return;
    
    const result = await state.api.update_playlist(playlist.id, { name });
    if (result.success) {
        showToast('success', t('player.playlist_renamed', { name }));
        playlist.name = name;
        renderPlaylists(player.playlists);
    } else {
        showError(result, 'player.update_failed');
    }
}

async function deletePlaylist(playlist) {
    if (!confirm(t('player.playlist_delete_confirm', { name: playlist.name }))) return;
    
    const result = await state.api.delete_playlist([playlist.id]);
    if (result.success) {
        showToast('success', t('player.playlist_deleted', { name: playlist.name }));
        await loadPlaylists();
    } else {
        showError(result, 'player.delete_failed');
    }
}

async function toggleSubscribePlaylist(playlist) {
    const subscribe = !playlist.subscribed;
    const result = await state.api.subscribe_playlist(playlist.id, subscribe);
    if (result.success) {
        playlist.subscribed = subscribe;
        showToast('success', subscribe ? t('player.playlist_subscribed') : t('player.playlist_unsubscribed'));
        renderPlaylists(player.playlists);
    } else {
        showError(result, subscribe ? 'player.subscribe_failed' : 'player.unsubscribe_failed');
    }
}

function openPlaylistDetailModal(playlist) {
    player.currentPlaylist = playlist;
    const modal = document.getElementById('playlist-detail-modal');
    if (!modal) return;
    
    // Populate info tab
    document.getElementById('playlist-name-input').value = playlist.name || '';
    document.getElementById('playlist-description-input').value = playlist.description || '';
    document.getElementById('playlist-privacy-select').value = playlist.privacy || '0';
    document.getElementById('playlist-cover-input').value = playlist.cover_url || '';
    
    // Clear tracks tab
    document.getElementById('playlist-tracks-list').innerHTML = '';
    document.getElementById('btn-playlist-remove-selected').hidden = true;
    document.getElementById('playlist-sort-select').hidden = true;
    
    // Clear smart tab
    document.getElementById('smart-rules-list').innerHTML = '';
    document.getElementById('smart-rule-preview-count').textContent = t('player.smart_playlist_preview_count', { count: 0 });
    
    // Switch to info tab
    document.querySelectorAll('.playlist-detail-tab').forEach(tab => {
        tab.classList.toggle('is-active', tab.dataset.tab === 'info');
    });
    document.querySelectorAll('.playlist-detail-panel').forEach(panel => {
        panel.classList.toggle('is-active', panel.dataset.panel === 'info');
    });
    
    // Load tracks
    loadPlaylistTracksForModal(playlist);
    
    modal.hidden = false;
}

async function loadPlaylistTracksForModal(playlist) {
    const result = await state.api.get_playlist_tracks(playlist.id);
    if (!result.success) {
        showError(result, 'playlist.fetch_failed');
        return;
    }
    renderPlaylistTracksInModal(result.tracks);
    document.getElementById('btn-playlist-remove-selected').hidden = true;
    document.getElementById('playlist-sort-select').hidden = true;
}

function renderPlaylistTracksInModal(tracks) {
    const container = document.getElementById('playlist-tracks-list');
    container.innerHTML = '';
    
    if (!tracks.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t('player.no_tracks');
        container.appendChild(empty);
        return;
    }
    
    tracks.forEach((track, index) => {
        const item = document.createElement('div');
        item.className = 'playlist-track-item';
        item.draggable = true;
        item.dataset.index = index;
        item.dataset.trackKey = track.key;
        
        const idx = document.createElement('span');
        idx.className = 'idx';
        idx.textContent = String(index + 1);
        
        const main = document.createElement('div');
        main.className = 'playlist-track-main';
        const name = document.createElement('div');
        name.className = 'playlist-track-name';
        name.textContent = track.name || '-';
        const sub = document.createElement('div');
        sub.className = 'playlist-track-sub';
        sub.textContent = [track.artists, track.album].filter(Boolean).join(' · ');
        main.append(name, sub);
        
        const actions = document.createElement('div');
        actions.className = 'playlist-track-actions';
        
        const removeBtn = document.createElement('button');
        removeBtn.className = 'btn btn-icon btn-sm';
        removeBtn.type = 'button';
        removeBtn.textContent = '🗑';
        removeBtn.title = t('player.playlist_remove_tracks');
        removeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            removeTrackFromPlaylist(playlist, track, item);
        });
        actions.appendChild(removeBtn);
        
        item.append(idx, main, actions);
        
        // Drag and drop
        item.addEventListener('dragstart', handlePlaylistTrackDragStart);
        item.addEventListener('dragover', handlePlaylistTrackDragOver);
        item.addEventListener('drop', handlePlaylistTrackDrop);
        item.addEventListener('dragend', handlePlaylistTrackDragEnd);
        
        container.appendChild(item);
    });
}

let playlistDragSource = null;

function handlePlaylistTrackDragStart(e) {
    playlistDragSource = e.currentTarget;
    e.currentTarget.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
}

function handlePlaylistTrackDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const target = e.currentTarget.closest('.playlist-track-item');
    if (target) target.classList.add('drag-over');
}

function handlePlaylistTrackDrop(e) {
    e.preventDefault();
    const target = e.currentTarget.closest('.playlist-track-item');
    if (!target || target === playlistDragSource) return;
    target.classList.remove('drag-over');
    reorderPlaylistTracks(playlistDragSource, target);
}

function handlePlaylistTrackDragEnd(e) {
    e.currentTarget.classList.remove('dragging');
    document.querySelectorAll('.playlist-track-item').forEach(el => el.classList.remove('drag-over'));
}

async function reorderPlaylistTracks(source, target) {
    // This would need backend API support for reordering
    showToast('info', t('player.feature_coming_soon'));
}

async function removeTrackFromPlaylist(playlist, track, item) {
    const result = await state.api.remove_tracks_from_playlist(playlist.id, [track.id]);
    if (result.success) {
        item.remove();
        showToast('success', t('player.track_removed'));
        // Re-index
        const items = document.querySelectorAll('#playlist-tracks-list .playlist-track-item');
        items.forEach((el, idx) => {
            el.dataset.index = idx;
            el.querySelector('.idx').textContent = String(idx + 1);
        });
    } else {
        showError(result, 'player.remove_failed');
    }
}

async function addTracksToPlaylist(playlist) {
    openAddTracksModal(playlist);
}

function openAddTracksModal(playlist) {
    player.targetPlaylist = playlist;
    const modal = document.getElementById('playlist-add-tracks-modal');
    if (!modal) return;
    
    // Reset
    document.getElementById('playlist-add-tracks-search').value = '';
    document.getElementById('playlist-add-tracks-list').innerHTML = '';
    document.getElementById('playlist-add-tracks-selected-count').textContent = t('player.tracks_selected', { count: 0 });
    
    // Switch to library tab
    document.querySelectorAll('.playlist-add-tracks-tab').forEach(tab => {
        tab.classList.toggle('is-active', tab.dataset.source === 'library');
    });
    
    // Load first page of local tracks
    loadAddTracksSource('library');
    
    modal.hidden = false;
}

async function loadAddTracksSource(source) {
    const list = document.getElementById('playlist-add-tracks-list');
    list.innerHTML = `<p class="empty">${t('common.loading')}</p>`;
    
    let tracks = [];
    if (source === 'library') {
        const result = await state.api.get_local_tracks('', 200, 0);
        tracks = result.tracks || [];
    } else if (source === 'liked') {
        const result = await state.api.get_liked_tracks();
        tracks = result.tracks || [];
    } else if (source === 'recent') {
        const result = await state.api.get_recent_tracks(200);
        tracks = (result.tracks || []).filter(t => t.key);
    }
    
    list.innerHTML = '';
    if (!tracks.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t('player.no_tracks_available');
        list.appendChild(empty);
        return;
    }
    
    tracks.forEach(track => {
        const item = document.createElement('div');
        item.className = 'playlist-add-track-item';
        item.dataset.trackKey = track.key;
        item.dataset.trackId = track.id;
        
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.addEventListener('change', updateAddTracksCount);
        
        const main = document.createElement('div');
        main.className = 'playlist-add-track-main';
        const name = document.createElement('div');
        name.className = 'playlist-add-track-name';
        name.textContent = track.name || '-';
        const sub = document.createElement('div');
        sub.className = 'playlist-add-track-sub';
        sub.textContent = [track.artists, track.album].filter(Boolean).join(' · ');
        main.append(name, sub);
        
        item.append(checkbox, main);
        item.addEventListener('click', (e) => {
            if (e.target !== checkbox) checkbox.click();
        });
        
        list.appendChild(item);
    });
}

function updateAddTracksCount() {
    const count = document.querySelectorAll('#playlist-add-tracks-list input[type="checkbox"]:checked').length;
    document.getElementById('playlist-add-tracks-selected-count').textContent = t('player.tracks_selected', { count });
}

async function confirmAddTracksToPlaylist() {
    const checkboxes = document.querySelectorAll('#playlist-add-tracks-list input[type="checkbox"]:checked');
    if (!checkboxes.length) {
        showToast('warning', t('player.no_tracks_selected'));
        return;
    }
    
    const trackIds = Array.from(checkboxes).map(cb => cb.closest('.playlist-add-track-item').dataset.trackId);
    const result = await state.api.add_tracks_to_playlist(player.targetPlaylist.id, trackIds);
    
    if (result.success) {
        showToast('success', t('player.tracks_added', { count: trackIds.length }));
        closeModal('playlist-add-tracks-modal');
        // Refresh the playlist tracks view
        loadPlaylistTracksForModal(player.targetPlaylist);
    } else {
        showError(result, 'player.add_failed');
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.hidden = true;
}

/* ----------------------------------------------------------- smart playlist */

function openSmartRuleModal(rule = null) {
    // Implementation for smart rule editor
    showToast('info', t('player.feature_coming_soon'));
}

/* ----------------------------------------------------------- import/export */

async function importPlaylist() {
    const format = document.getElementById('playlist-import-format').value;
    const file = document.getElementById('playlist-import-file').files[0];
    const merge = document.getElementById('playlist-import-merge').checked;
    
    if (!file) {
        showToast('warning', t('player.no_file_selected'));
        return;
    }
    
    // Parse file based on format
    const text = await file.text();
    let tracks = [];
    
    if (format === 'm3u') {
        tracks = parseM3U(text);
    } else if (format === 'json') {
        tracks = JSON.parse(text);
    } else if (format === 'pls') {
        tracks = parsePLS(text);
    }
    
    if (!tracks.length) {
        showToast('warning', t('player.no_valid_tracks'));
        return;
    }
    
    // Create or merge playlist
    if (merge && player.currentPlaylist) {
        // Add to existing
    } else {
        // Create new
    }
}

function parseM3U(text) {
    const lines = text.split('\n');
    return lines.filter(l => l && !l.startsWith('#')).map(l => l.trim());
}

function parsePLS(text) {
    // Simple PLS parser
    return [];
}

async function exportPlaylist() {
    const format = document.getElementById('playlist-export-format').value;
    const playlistId = document.getElementById('playlist-export-select').value;
    const extended = document.getElementById('playlist-export-extended').checked;
    
    if (!playlistId) {
        showToast('warning', t('player.no_playlist_selected'));
        return;
    }
    
    // Get playlist tracks
    const result = await state.api.get_playlist_tracks(playlistId);
    if (!result.success) {
        showError(result, 'playlist.fetch_failed');
        return;
    }
    
    let content = '';
    if (format === 'm3u') {
        content = generateM3U(result.tracks, extended);
    } else if (format === 'json') {
        content = JSON.stringify(result.tracks, null, 2);
    } else if (format === 'pls') {
        content = generatePLS(result.tracks);
    }
    
    // Download
    const blob = new Blob([content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `playlist.${format}`;
    a.click();
    URL.revokeObjectURL(url);
    showToast('success', t('player.exported', { format }));
}

function generateM3U(tracks, extended) {
    let m3u = '#EXTM3U\n';
    tracks.forEach(track => {
        if (extended && track.duration) {
            m3u += `#EXTINF:${Math.floor(track.duration / 1000)},${track.artists} - ${track.name}\n`;
        } else {
            m3u += `#EXTINF:-1,${track.artists} - ${track.name}\n`;
        }
        m3u += `${track.id ? `https://music.163.com/song?id=${track.id}` : track.path}\n`;
    });
    return m3u;
}

function generatePLS(tracks) {
    let pls = '[playlist]\n';
    tracks.forEach((track, index) => {
        const num = index + 1;
        pls += `File${num}=${track.id ? `https://music.163.com/song?id=${track.id}` : track.path}\n`;
        pls += `Title${num}=${track.artists} - ${track.name}\n`;
        if (track.duration) pls += `Length${num}=${Math.floor(track.duration / 1000)}\n`;
    });
    pls += `NumberOfEntries=${tracks.length}\nVersion=2\n`;
    return pls;
}

async function doSearch(more) {
    const input = document.getElementById('search-input');
    const keyword = (input.value || '').trim();
    if (player.sourceTab === 'local') {
        // While the local list is open the box filters it instead of searching
        // NetEase -- that is what "let me find it in my own files" means.
        await loadLocal(more);
        return;
    }
    if (!keyword) {
        showToast('warning', t('player.search_empty'));
        return;
    }
    if (!more || keyword !== player.searchKeyword) {
        player.searchKeyword = keyword;
        player.searchOffset = 0;
        player.searchResults = [];
    }
    player.sourceTab = 'search';
    document.querySelectorAll('.source-tab').forEach((tab) =>
        tab.classList.toggle('is-active', tab.dataset.source === 'search'));
    document.getElementById('playlist-list').innerHTML = '';
    const button = document.getElementById('btn-search');
    button.disabled = true;
    try {
        const result = await state.api.search_tracks(keyword, 50, player.searchOffset);
        if (!result.success) {
            showError(result, 'player.search_failed');
            return;
        }
        player.searchResults = more
            ? player.searchResults.concat(result.tracks || [])
            : (result.tracks || []);
        player.searchOffset += (result.tracks || []).length;
        setListTitle(t('player.search_result', { keyword }));
        playTracksInto(player.searchResults, { kind: 'search', name: keyword });
        document.getElementById('btn-list-more').hidden = (result.tracks || []).length < 50;
    } finally {
        button.disabled = false;
    }
}

async function showSearchResults() {
    if (!player.searchKeyword) {
        renderEmptyTracks(t('player.search_hint'));
        setListTitle(t('player.source_search'));
        return;
    }
    setListTitle(t('player.search_result', { keyword: player.searchKeyword }));
    playTracksInto(player.searchResults || [], { kind: 'search', name: player.searchKeyword });
}

async function showSearchResults() {
    if (!player.searchKeyword) {
        renderEmptyTracks(t('player.search_hint'));
        setListTitle(t('player.source_search'));
        return;
    }
    setListTitle(t('player.search_result', { keyword: player.searchKeyword }));
    playTracksInto(player.searchResults || [], { kind: 'search', name: player.searchKeyword });
}

function setListTitle(text) {
    const el = document.getElementById('list-title');
    if (el) el.textContent = text;
}

/* ----------------------------------------------------------- discovery sources */

async function loadRecommendPlaylists() {
    setListTitle(t('player.source_recommend'));
    document.getElementById('playlist-list').innerHTML = `<p class="empty">${t('common.loading')}</p>`;
    document.getElementById('track-list').innerHTML = '';
    const result = await state.api.get_recommend_playlists();
    if (!result.success) {
        const container = document.getElementById('playlist-list');
        container.innerHTML = '';
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t(result.error_key || 'player.login_required');
        container.appendChild(empty);
        return;
    }
    player.playlists = result.playlists || [];
    renderPlaylists(player.playlists);
}

async function loadPersonalFM() {
    setListTitle(t('player.source_personal_fm'));
    document.getElementById('playlist-list').innerHTML = '';
    const result = await state.api.get_personal_fm();
    if (!result.success) {
        renderEmptyTracks(t(result.error_key || 'player.login_required'));
        return;
    }
    playTracksInto(result.tracks, { kind: 'personal_fm', name: t('player.source_personal_fm') });
}

async function loadNewSongs(more) {
    if (!more) {
        player.newSongsOffset = 0;
        player.newSongsTracks = [];
    }
    const result = await state.api.get_new_songs(50, player.newSongsOffset);
    const page = result.tracks || [];
    player.newSongsTracks = more ? player.newSongsTracks.concat(page) : page;
    player.newSongsOffset += page.length;

    setListTitle(t('player.source_new_songs'));
    if (!player.newSongsTracks.length) {
        renderEmptyTracks(t('player.no_new_songs'));
        return;
    }
    playTracksInto(player.newSongsTracks, { kind: 'new_songs', name: t('player.source_new_songs') });
    document.getElementById('btn-list-more').hidden = page.length < 50;
}

async function loadRecommendMVs(more) {
    if (!more) {
        player.recommendMVOffset = 0;
        player.recommendMVTracks = [];
    }
    const result = await state.api.get_recommend_mvs(20, player.recommendMVOffset);
    const page = result.mvs || [];
    player.recommendMVTracks = more ? player.recommendMVTracks.concat(page) : page;
    player.recommendMVOffset += page.length;

    setListTitle(t('player.source_recommend_mv'));
    if (!player.recommendMVTracks.length) {
        renderEmptyTracks(t('player.no_recommend_mv'));
        return;
    }
    playTracksInto(player.recommendMVTracks, { kind: 'recommend_mv', name: t('player.source_recommend_mv') });
    document.getElementById('btn-list-more').hidden = page.length < 20;
}

async function loadTopLists() {
    setListTitle(t('player.source_toplist'));
    document.getElementById('playlist-list').innerHTML = `<p class="empty">${t('common.loading')}</p>`;
    document.getElementById('track-list').innerHTML = '';
    const result = await state.api.get_top_lists();
    if (!result.success) {
        const container = document.getElementById('playlist-list');
        container.innerHTML = '';
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t(result.error_key || 'error.network');
        container.appendChild(empty);
        return;
    }
    player.topLists = result.toplists || [];
    renderTopLists(player.topLists);
}

function renderTopLists(topLists) {
    const container = document.getElementById('playlist-list');
    container.innerHTML = '';
    if (!topLists.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t('player.no_toplist');
        container.appendChild(empty);
        return;
    }
    const fragment = document.createDocumentFragment();
    topLists.forEach((toplist) => {
        const row = document.createElement('div');
        row.className = 'playlist-row';
        row.dataset.toplistId = toplist.id;
        const cover = document.createElement('img');
        cover.alt = '';
        if (toplist.coverImgUrl) cover.src = toplist.coverImgUrl;
        cover.onerror = () => cover.removeAttribute('src');
        const meta = document.createElement('div');
        meta.className = 'pl-meta';
        const name = document.createElement('span');
        name.className = 'pl-name';
        name.textContent = toplist.name;
        const sub = document.createElement('span');
        sub.className = 'pl-sub';
        const freq = toplist.updateFrequency === '每日更新' ? 'toplist_update_daily' : 'toplist_update_weekly';
        sub.textContent = t(freq);
        meta.append(name, sub);
        row.append(cover, meta);
        row.addEventListener('click', () => openTopList(toplist));
        fragment.appendChild(row);
    });
    container.appendChild(fragment);
    setListCount(topLists.length);
}

async function openTopList(toplist) {
    player.playlistOpen = { id: toplist.id, name: toplist.name, coverImgUrl: toplist.coverImgUrl };
    document.getElementById('playlist-list').innerHTML = `<p class="empty">${t('common.loading')}</p>`;
    const result = await state.api.get_top_list_tracks(toplist.id);
    if (!result.success) {
        renderTopLists(player.topLists);
        showError(result, 'playlist.fetch_failed');
        return;
    }
    document.getElementById('playlist-list').innerHTML = '';
    document.getElementById('btn-list-back').hidden = false;
    setListTitle(toplist.name);
    playTracksInto(result.tracks, { kind: 'toplist', id: toplist.id, name: toplist.name });
}

/* ----------------------------------------------------------- multi-search */

async function doMultiSearch(more) {
    const input = document.getElementById('search-input');
    const keyword = (input.value || '').trim();
    if (!keyword) {
        showToast('warning', t('player.search_empty'));
        return;
    }
    if (!more || keyword !== player.searchKeyword) {
        player.searchKeyword = keyword;
        player.searchOffset = 0;
        player.searchResults = { songs: [], playlists: [], artists: [], albums: [], mvs: [] };
    }
    player.sourceTab = 'search';
    document.querySelectorAll('.source-tab').forEach((tab) =>
        tab.classList.toggle('is-active', tab.dataset.source === 'search'));
    document.getElementById('playlist-list').innerHTML = '';
    document.getElementById('search-type-tabs').hidden = false;
    
    // Show the active search type tab
    const activeType = player.searchType || 'all';
    document.querySelectorAll('.search-type-tab').forEach((tab) => {
        tab.classList.toggle('is-active', tab.dataset.type === activeType);
    });
    
    const button = document.getElementById('btn-search');
    button.disabled = true;
    try {
        const result = await state.api.search_multi(keyword, 30, player.searchOffset);
        if (!result.success) {
            showError(result, 'player.search_failed');
            return;
        }
        if (more) {
            for (const key of Object.keys(result.results)) {
                player.searchResults[key] = (player.searchResults[key] || []).concat(result.results[key] || []);
            }
        } else {
            player.searchResults = result.results;
        }
        player.searchOffset += (result.results.songs || []).length;
        renderSearchResults();
        document.getElementById('btn-list-more').hidden = (result.results.songs || []).length < 30;
    } finally {
        button.disabled = false;
    }
}

function renderSearchResults() {
    const activeType = player.searchType || 'all';
    const results = player.searchResults || {};
    const tracks = results[activeType === 'all' ? 'songs' : activeType] || [];
    
    setListTitle(t('player.search_result', { keyword: player.searchKeyword }));
    playTracksInto(tracks, { kind: 'search', name: player.searchKeyword });
}

function switchSearchType(type) {
    player.searchType = type;
    document.querySelectorAll('.search-type-tab').forEach((tab) => {
        tab.classList.toggle('is-active', tab.dataset.type === type);
    });
    renderSearchResults();
}

/* ----------------------------------------------------------- album/artist detail */

async function openAlbumDetail(album) {
    const result = await state.api.get_album_detail(album.id);
    if (!result.success) {
        showError(result, 'player.album_not_found');
        return;
    }
    document.getElementById('playlist-list').innerHTML = '';
    document.getElementById('track-list').innerHTML = '';
    document.getElementById('btn-list-back').hidden = false;
    setListTitle(t('player.album_detail'));
    
    // Show album info at top
    const trackList = document.getElementById('track-list');
    trackList.innerHTML = '';
    const albumInfo = document.createElement('div');
    albumInfo.className = 'album-detail-header';
    albumInfo.innerHTML = `
        <img src="${result.album.cover_url || ''}" alt="" class="album-detail-cover" onerror="this.style.display='none'">
        <div class="album-detail-meta">
            <h3>${result.album.name}</h3>
            <p>${t('player.artist')}: <a href="#" data-artist-id="${result.album.artist_id}">${result.album.artist}</a></p>
            <p class="muted">${result.album.description || ''}</p>
            <div class="album-detail-actions">
                <button class="btn btn-primary btn-sm" data-action="play-all">${t('player.play_all')}</button>
                <button class="btn btn-secondary btn-sm" data-action="add-queue">${t('player.add_to_queue')}</button>
                <button class="btn btn-ghost btn-sm" data-action="download-all">${t('player.download_all')}</button>
            </div>
        </div>
    `;
    trackList.appendChild(albumInfo);
    
    // Add click handlers for album actions
    albumInfo.querySelector('[data-action="play-all"]').addEventListener('click', () => {
        playTracksInto(result.tracks, { kind: 'album', id: album.id, name: album.name });
    });
    albumInfo.querySelector('[data-action="add-queue"]').addEventListener('click', () => {
        addTracksToQueue(result.tracks);
    });
    albumInfo.querySelector('[data-action="download-all"]').addEventListener('click', () => {
        downloadTracks(result.tracks);
    });
    albumInfo.querySelector('[data-artist-id]').addEventListener('click', (e) => {
        e.preventDefault();
        openArtistDetail({ id: result.album.artist_id, name: result.album.artist });
    });
    
    playTracksInto(result.tracks, { kind: 'album', id: album.id, name: album.name });
}

async function openArtistDetail(artist) {
    const result = await state.api.get_artist_detail(artist.id);
    if (!result.success) {
        showError(result, 'player.artist_not_found');
        return;
    }
    document.getElementById('playlist-list').innerHTML = '';
    document.getElementById('track-list').innerHTML = '';
    document.getElementById('btn-list-back').hidden = false;
    setListTitle(t('player.artist_detail'));
    
    const trackList = document.getElementById('track-list');
    trackList.innerHTML = '';
    
    // Artist header
    const artistHeader = document.createElement('div');
    artistHeader.className = 'artist-detail-header';
    artistHeader.innerHTML = `
        <img src="${result.artist.cover_url || ''}" alt="" class="artist-detail-cover" onerror="this.style.display='none'">
        <div class="artist-detail-meta">
            <h3>${result.artist.name}</h3>
            <p class="muted">${result.artist.description || ''}</p>
            <div class="artist-detail-actions">
                <button class="btn btn-primary btn-sm" data-action="play-hot">${t('player.play_all')} ${t('player.hot_songs')}</button>
                <button class="btn btn-secondary btn-sm" data-action="view-albums">${t('player.artist_albums')}</button>
            </div>
        </div>
    `;
    trackList.appendChild(artistHeader);
    
    artistHeader.querySelector('[data-action="play-hot"]').addEventListener('click', () => {
        playTracksInto(result.hot_songs, { kind: 'artist', id: artist.id, name: artist.name });
    });
    artistHeader.querySelector('[data-action="view-albums"]').addEventListener('click', () => {
        showArtistAlbums(result.albums);
    });
    
    // Hot songs
    if (result.hot_songs && result.hot_songs.length) {
        const section = document.createElement('div');
        section.className = 'artist-section';
        section.innerHTML = `<h4>${t('player.hot_songs')}</h4>`;
        const list = document.createElement('div');
        list.className = 'track-list';
        result.hot_songs.forEach((song, index) => {
            list.appendChild(buildTrackRow(song, index, null));
        });
        section.appendChild(list);
        trackList.appendChild(section);
    }
    
    playTracksInto(result.hot_songs || [], { kind: 'artist', id: artist.id, name: artist.name });
}

function showArtistAlbums(albums) {
    const trackList = document.getElementById('track-list');
    trackList.innerHTML = '';
    setListTitle(t('player.artist_albums'));
    
    albums.forEach((album) => {
        const row = document.createElement('div');
        row.className = 'album-row';
        row.innerHTML = `
            <img src="${album.cover_url || ''}" alt="" class="album-row-cover" onerror="this.style.display='none'">
            <div class="album-row-meta">
                <span class="album-row-name">${album.name}</span>
                <span class="muted album-row-date">${album.publish_time ? new Date(album.publish_time).getFullYear() : ''}</span>
            </div>
        `;
        row.addEventListener('click', () => openAlbumDetail(album));
        trackList.appendChild(row);
    });
}

/* ----------------------------------------------------------- search suggestions */

let suggestTimeout = null;
function initSearchSuggest() {
    const input = document.getElementById('search-input');
    const suggestBox = document.getElementById('search-suggest');
    if (!input || !suggestBox) return;
    
    input.addEventListener('input', () => {
        clearTimeout(suggestTimeout);
        const keyword = input.value.trim();
        if (!keyword) {
            suggestBox.hidden = true;
            return;
        }
        suggestTimeout = setTimeout(async () => {
            const result = await state.api.search_suggest(keyword);
            if (result.success && result.suggestions.length) {
                suggestBox.innerHTML = result.suggestions.map(s => 
                    `<div class="suggest-item" data-value="${s}">${s}</div>`
                ).join('');
                suggestBox.hidden = false;
            } else {
                suggestBox.hidden = true;
            }
        }, 300);
    });
    
    input.addEventListener('focus', () => {
        if (input.value.trim()) {
            clearTimeout(suggestTimeout);
            suggestTimeout = setTimeout(() => input.dispatchEvent(new Event('input')), 0);
        }
    });
    
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.search-wrapper')) {
            suggestBox.hidden = true;
        }
    });
    
    suggestBox.addEventListener('click', (e) => {
        const item = e.target.closest('.suggest-item');
        if (item) {
            input.value = item.dataset.value;
            suggestBox.hidden = true;
            doMultiSearch(false);
        }
    });
}

function setListCount(count) {
    const el = document.getElementById('list-count');
    if (el) el.textContent = count ? String(count) : '';
}

/**
 * Re-apply the source header (title, count, local actions) in the current
 * language, without refetching the list it belongs to.
 *
 * Called from applyTranslations() so a language switch does not leave the
 * header in the previous language.  Every state the title can come from is
 * mirrored here; the loaders (loadPlaylists, openPlaylist, doSearch, ...) stay
 * the single place that decides what the title *is*.
 */
function renderSourceHeader() {
    if (player.playlistOpen) {
        setListTitle(player.playlistOpen.name || t('player.source_playlists'));
        document.getElementById('btn-list-back').hidden = false;
    } else {
        const tab = player.sourceTab;
        if (tab === 'search') {
            setListTitle(player.searchKeyword
                ? t('player.search_result', { keyword: player.searchKeyword })
                : t('player.source_search'));
        } else {
            setListTitle(t('player.source_' + tab));
        }
        document.getElementById('btn-list-back').hidden = true;
    }
    setListCount(player.visible.length);
    document.getElementById('local-actions').hidden = player.sourceTab !== 'local';
}

function renderEmptyTracks(message) {
    const list = document.getElementById('track-list');
    list.innerHTML = '';
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = message;
    list.appendChild(empty);
    setListCount(0);
}

/** Remember the visible list so "play all" and the rows share one queue. */
function playTracksInto(tracks, source) {
    player.visible = tracks.slice();
    player.visibleSource = source;
    renderTrackList();
    setListCount(tracks.length);
}

function currentKey() {
    return player.current ? trackKeyOf(player.current) : '';
}

function trackKeyOf(track) {
    if (!track) return '';
    if (track.key) return track.key;
    const source = track.source || 'online';
    return `${source}:${source === 'local' ? (track.path || track.id || '') : (track.id || '')}`;
}

function playCountOf(track) {
    return player.counts[trackKeyOf(track)] || 0;
}

function renderTrackList() {
    const list = document.getElementById('track-list');
    if (!list || !player.visible) return;
    list.innerHTML = '';
    const playing = currentKey();
    const fragment = document.createDocumentFragment();
    player.visible.forEach((track, index) => {
        fragment.appendChild(buildTrackRow(track, index, playing));
    });
    list.appendChild(fragment);
}

/** The rendered row for a track key, if that track is on screen. */
function findTrackRow(key) {
    if (!key) return null;
    const rows = document.querySelectorAll('.track-row');
    for (let i = 0; i < rows.length; i += 1) {
        if (rows[i].dataset.trackKey === key) return rows[i];
    }
    return null;
}

/** Update one row in place (play count / favourite) -- no full re-render. */
function refreshTrackRow(key) {
    const row = findTrackRow(key);
    if (!row) return;
    const nameLine = row.querySelector('.track-name-line');
    const count = player.counts[key] || 0;
    let chip = row.querySelector('.count-chip');
    if (count > 0) {
        if (!chip && nameLine) {
            chip = document.createElement('span');
            chip.className = 'count-chip';
            nameLine.appendChild(chip);
        }
        if (chip) {
            chip.textContent = t('player.play_count', { count });
            chip.title = t('player.play_count_title', { count });
        }
    } else if (chip) {
        chip.remove();
    }
    const favorite = player.favoriteKeys.has(key);
    const favButton = row.querySelector('[data-role="fav"]');
    if (favButton) {
        favButton.textContent = favorite ? '♥' : '♡';
        favButton.classList.toggle('fav-on', favorite);
        favButton.classList.toggle('fav-off', !favorite);
    }
}

function buildTrackRow(track, index, playingKey) {
    const row = document.createElement('div');
    row.className = 'track-row';
    const key = trackKeyOf(track);
    row.dataset.trackKey = key;
    if (key && key === playingKey) row.classList.add('is-playing');

    const idx = document.createElement('span');
    idx.className = 'idx';
    idx.textContent = (key && key === playingKey) ? '♪' : String(index + 1);

    const main = document.createElement('div');
    main.className = 'track-main';
    const nameLine = document.createElement('div');
    nameLine.className = 'track-name-line';
    const name = document.createElement('span');
    name.className = 'track-name';
    name.textContent = track.name || '-';
    nameLine.appendChild(name);
    if (track.source === 'local') {
        const badge = document.createElement('span');
        badge.className = 'pill';
        badge.textContent = t('player.badge_local');
        nameLine.appendChild(badge);
    } else if (track.vip) {
        const badge = document.createElement('span');
        badge.className = 'badge';
        badge.textContent = 'VIP';
        nameLine.appendChild(badge);
    }
    const count = playCountOf(track);
    if (count > 0) {
        const chip = document.createElement('span');
        chip.className = 'count-chip';
        chip.textContent = t('player.play_count', { count });
        chip.title = t('player.play_count_title', { count });
        nameLine.appendChild(chip);
    }
    const sub = document.createElement('span');
    sub.className = 'track-sub';
    sub.textContent = [track.artists, track.album, fmtDuration(track.duration)]
        .filter(Boolean).join(' · ');
    main.append(nameLine, sub);

    const actions = document.createElement('div');
    actions.className = 'track-actions';

    const fav = document.createElement('button');
    fav.type = 'button';
    fav.dataset.role = 'fav';
    fav.className = 'btn btn-icon ' + (player.favoriteKeys.has(key) ? 'fav-on' : 'fav-off');
    fav.textContent = player.favoriteKeys.has(key) ? '♥' : '♡';
    fav.title = t('player.favorite');
    fav.addEventListener('click', (event) => {
        event.stopPropagation();
        toggleFavorite(track);
    });
    actions.appendChild(fav);

    if (track.source !== 'local') {
        const download = document.createElement('button');
        download.type = 'button';
        download.dataset.role = 'download';
        download.className = 'btn btn-icon';
        download.textContent = '⤓';
        download.title = t('player.download');
        download.addEventListener('click', (event) => {
            event.stopPropagation();
            downloadTrack(track);
        });
        actions.appendChild(download);
    } else {
        const locate = document.createElement('button');
        locate.type = 'button';
        locate.dataset.role = 'reveal';
        locate.className = 'btn btn-icon';
        locate.textContent = '⌘';
        locate.title = t('player.reveal');
        locate.addEventListener('click', (event) => {
            event.stopPropagation();
            state.api.reveal_path(track.path || '');
        });
        actions.appendChild(locate);
    }

    row.append(idx, main, actions);
    row.addEventListener('click', () => {
        const queue = player.visible || [];
        startQueue(queue, index, player.visibleSource);
    });
    return row;
}

/* ---------------------------------------------------------- playback core */

async function startQueue(tracks, index, source) {
    if (!tracks || !tracks.length) return;
    player.queue = tracks.slice();
    player.source = source || null;
    await playAt(index, { autoplay: true });
}

async function playAt(index, options) {
    if (!player.queue.length) return false;
    const count = player.queue.length;
    player.index = ((index % count) + count) % count;
    return loadCurrent(options);
}

async function loadCurrent(options) {
    const opts = options || {};
    const track = player.queue[player.index];
    if (!track) return false;
    player.current = track;
    player.resolved = null;
    player.pendingPosition = opts.position || 0;
    renderNowPlaying();
    highlightPlayingRow();
    renderLyricsLoading();

    let resolved;
    try {
        resolved = await state.api.resolve_track(track);
    } catch (err) {
        resolved = { success: false, message: String(err && err.message || err) };
    }
    if (!resolved || !resolved.success) {
        showToast('error', `${t('player.unavailable')}: ${track.name || ''}`, 4200);
        // A dead track must not stall the list: move on when it was automatic.
        if (opts.autoplay) setTimeout(() => advance(true), 900);
        updatePlayButton();
        return false;
    }

    player.resolved = resolved;
    if (resolved.kind === 'local') {
        // The local copy knows the real tags; the online metadata is a guess.
        if (resolved.name) track.name = resolved.name;
        if (resolved.artists) track.artists = resolved.artists;
        if (resolved.album != null) track.album = resolved.album;
        if (resolved.duration) track.duration = resolved.duration;
        if (resolved.matched_local) {
            showToast('info', t('player.playing_local_copy'), 2600);
        }
    }
    renderNowPlaying();
    highlightPlayingRow();
    loadLyrics(track);

    audio.src = resolved.url;
    audio.load();
    player.pendingRecord = true;
    if (opts.autoplay !== false) await playAudio();
    return true;
}

async function playAudio() {
    try {
        await audio.play();
    } catch (err) {
        // WKWebView only allows audible autoplay after a gesture.
        console.info('play() was refused:', err && err.message);
        updatePlayButton();
    }
}

function togglePlay() {
    if (!player.queue.length) {
        if (player.visible && player.visible.length) {
            startQueue(player.visible, 0, player.visibleSource);
        }
        return;
    }
    if (audio.paused) playAudio();
    else audio.pause();
    updatePlayButton();
}

/** Next index for the current mode. Shuffle is a plain uniform random pick. */
function nextIndex(automatic) {
    const count = player.queue.length;
    if (!count) return -1;
    if (player.mode === 'single') {
        return automatic ? player.index : (player.index + 1) % count;
    }
    if (player.mode === 'shuffle') {
        if (count === 1) return 0;
        // Uniform over every other entry -- play counts never bias this.
        let pick = player.index;
        while (pick === player.index) pick = Math.floor(Math.random() * count);
        return pick;
    }
    return (player.index + 1) % count;
}

function previousIndex() {
    const count = player.queue.length;
    if (!count) return -1;
    if (player.mode === 'shuffle') {
        if (count === 1) return 0;
        let pick = player.index;
        while (pick === player.index) pick = Math.floor(Math.random() * count);
        return pick;
    }
    return (player.index - 1 + count) % count;
}

function advance(automatic) {
    const next = nextIndex(automatic !== false);
    if (next < 0) return;
    playAt(next, { autoplay: true });
}

function playNext() { advance(false); }

function playPrevious() {
    const previous = previousIndex();
    if (previous < 0) return;
    playAt(previous, { autoplay: true });
}

function setMode(mode, persist) {
    player.mode = MODES.indexOf(mode) >= 0 ? mode : 'list';
    updateModeButton();
    showToast('info', t('player.mode_' + player.mode), 1800);
    if (persist && state.api) {
        state.api.update_settings('playback', { play_mode: player.mode }).then((result) => {
            if (result && result.success) state.settings = result.settings;
        });
    }
}

function cycleMode() {
    setMode(MODES[(MODES.indexOf(player.mode) + 1) % MODES.length], true);
}

function updateModeButton() {
    const button = document.getElementById('pb-mode');
    if (!button) return;
    button.textContent = MODE_ICONS[player.mode] || MODE_ICONS.list;
    const label = t('player.mode_' + player.mode);
    button.title = label;
    button.setAttribute('aria-label', label);
}

function seekBy(seconds) {
    if (!audio.duration || !isFinite(audio.duration)) return;
    audio.currentTime = Math.max(0, Math.min(audio.duration, audio.currentTime + seconds));
}

function seekToFraction(fraction) {
    if (!audio.duration || !isFinite(audio.duration)) return;
    audio.currentTime = Math.max(0, Math.min(1, fraction)) * audio.duration;
}

/* --------------------------------------------------------------- volume */

function setVolume(value, persist) {
    player.volume = Math.max(0, Math.min(1, Number(value)));
    audio.volume = player.volume;
    audio.muted = player.muted;
    syncVolumeSlider();
    if (persist) persistVolumeLater();
}

let volumeTimer = null;
function persistVolumeLater() {
    if (volumeTimer) clearTimeout(volumeTimer);
    volumeTimer = setTimeout(() => {
        if (state.api) {
            state.api.update_settings('playback', { volume: player.volume }).then((result) => {
                if (result && result.success) state.settings = result.settings;
            });
        }
    }, 600);
}

function syncVolumeSlider() {
    const slider = document.getElementById('pb-volume');
    if (slider && Number(slider.value) !== player.volume) slider.value = String(player.volume);
    const icon = document.getElementById('pb-mute');
    if (icon) icon.textContent = (player.muted || player.volume === 0) ? '🔇' : '🔊';
}

function toggleMute() {
    player.muted = !player.muted;
    audio.muted = player.muted;
    syncVolumeSlider();
}

/* ------------------------------------------------------------ favourites */

async function loadPlayerExtras() {
    const stats = await state.api.get_play_stats();
    if (!stats || !stats.success) return;
    player.counts = stats.counts || {};
    player.favoriteKeys = new Set(stats.favorites || []);
    renderTrackList();
    renderNowPlaying();
    updatePlayStatsText();
}

async function toggleFavorite(track) {
    const key = trackKeyOf(track);
    if (!key) return;
    const wanted = !player.favoriteKeys.has(key);
    const result = await state.api.set_favorite(track, wanted);
    if (!result.success) {
        showError(result, 'error.unknown');
        return;
    }
    player.favoriteKeys = new Set(result.favorites || []);
    if (result.cloud === 'login_required') showToast('info', t('player.favorite_local_only'), 3600);
    else if (result.cloud === 'failed') showToast('warning', t('player.favorite_cloud_failed'), 3600);
    else if (wanted) showToast('success', t('player.favorite_added'), 1800);
    refreshTrackRow(key);
    renderNowPlaying();
}

/* ---------------------------------------------------------------- lyrics */

function renderLyricsLoading() {
    const box = document.getElementById('lyrics-scroll');
    if (!box) return;
    box.innerHTML = `<p class="empty">${t('lyrics.loading')}</p>`;
    player.lyrics = [];
    player.lyricIndex = -1;
    const save = document.getElementById('btn-save-lyrics');
    if (save) save.hidden = true;
}

function renderLyricSource() {
    const el = document.getElementById('lyrics-source');
    if (!el) return;
    const source = player.lyricSource || 'none';
    el.textContent = (player.lyrics.length && source !== 'none')
        ? t('lyrics.source_' + source) : '';
}

async function loadLyrics(track, save) {
    if (!track) return;
    const box = document.getElementById('lyrics-scroll');
    if (!box) return;
    box.innerHTML = `<p class="empty">${t('lyrics.loading')}</p>`;
    let result;
    try {
        result = await state.api.get_track_lyrics(track, !!save);
    } catch (err) {
        result = null;
    }
    if (!result || !result.has_lyrics) {
        box.innerHTML = `<p class="empty">${t('lyrics.none')}</p>`;
        player.lyrics = [];
        player.lyricIndex = -1;
        player.lyricSource = 'none';
        const saveButton = document.getElementById('btn-save-lyrics');
        if (saveButton) saveButton.hidden = true;
        renderLyricSource();
        return;
    }
    player.lyrics = result.lines || [];
    player.lyricIndex = -1;
    player.lyricSource = result.source || 'none';
    renderLyricLines();
    renderLyricSource();
    const saveButton = document.getElementById('btn-save-lyrics');
    if (saveButton) {
        // Offer to keep a lyric that had to be looked up online.
        saveButton.hidden = !(track.source === 'local' && result.source === 'netease'
            && !result.lyrics_path);
    }
    if (result.lyrics_path) showToast('success', t('lyrics.saved'), 2600);
}

function renderLyricLines() {
    const box = document.getElementById('lyrics-scroll');
    if (!box) return;
    box.innerHTML = '';
    const showTranslation = (state.settings && state.settings.playback.show_translation) !== false;
    const fragment = document.createDocumentFragment();
    
    if (player.dualLine && showTranslation) {
        // Dual-line mode: original line + translation line
        player.lyrics.forEach((line) => {
            const wrapper = document.createElement('div');
            wrapper.className = 'lyric-line-wrapper';
            wrapper.dataset.time = String(line.time);
            
            const originalLine = document.createElement('div');
            originalLine.className = 'lyric-line lyric-original';
            originalLine.dataset.time = String(line.time);
            originalLine.textContent = line.text || '♪';
            wrapper.appendChild(originalLine);
            
            if (line.translation) {
                const translationLine = document.createElement('div');
                translationLine.className = 'lyric-line lyric-translation-line';
                translationLine.dataset.time = String(line.time);
                translationLine.textContent = line.translation;
                wrapper.appendChild(translationLine);
            }
            
            fragment.appendChild(wrapper);
        });
    } else if (player.karaoke) {
        // Karaoke mode: word-by-word highlighting
        renderKaraokeLines(fragment);
    } else {
        // Standard single-line mode
        player.lyrics.forEach((line) => {
            const element = document.createElement('div');
            element.className = 'lyric-line';
            element.dataset.time = String(line.time);
            element.textContent = line.text || '♪';
            if (showTranslation && line.translation) {
                const translation = document.createElement('span');
                translation.className = 'lyric-translation';
                translation.textContent = line.translation;
                element.appendChild(translation);
            }
            fragment.appendChild(element);
        });
    }
    
    box.appendChild(fragment);
    box.scrollTop = 0;
}

function renderKaraokeLines(fragment) {
    // Karaoke mode requires word-level timing data
    // For now, fall back to standard lines with karaoke class
    const showTranslation = (state.settings && state.settings.playback.show_translation) !== false;
    player.lyrics.forEach((line) => {
        const element = document.createElement('div');
        element.className = 'lyric-line lyric-karaoke';
        element.dataset.time = String(line.time);
        element.textContent = line.text || '♪';
        if (showTranslation && line.translation) {
            const translation = document.createElement('span');
            translation.className = 'lyric-translation';
            translation.textContent = line.translation;
            element.appendChild(translation);
        }
        fragment.appendChild(element);
    });
}

function updateLyricsOffsetLabel() {
    const label = document.getElementById('lyrics-offset-value');
    const slider = document.getElementById('lyrics-offset');
    if (label && slider) {
        const val = parseInt(slider.value, 10);
        if (val >= 0) {
            label.textContent = '+' + val + 'ms';
        } else {
            label.textContent = val + 'ms';
        }
    }
}

function toggleDualLineLyrics() {
    player.dualLine = !player.dualLine;
    if (player.dualLine) {
        player.karaoke = false;
    }
    updateLyricsModeButtons();
    renderLyricLines();
    showToast('info', player.dualLine ? t('lyrics.dual_line_hint') : t('lyrics.single_line_hint'), 2000);
}

function toggleKaraokeLyrics() {
    player.karaoke = !player.karaoke;
    if (player.karaoke) {
        player.dualLine = false;
    }
    updateLyricsModeButtons();
    renderLyricLines();
    showToast('info', player.karaoke ? t('lyrics.karaoke_hint') : t('lyrics.standard_hint'), 2000);
}

function updateLyricsModeButtons() {
    const dualBtn = document.getElementById('btn-lyrics-dual');
    const karaokeBtn = document.getElementById('btn-lyrics-karaoke');
    if (dualBtn) {
        dualBtn.classList.toggle('active', player.dualLine);
    }
    if (karaokeBtn) {
        karaokeBtn.classList.toggle('active', player.karaoke);
    }
}

function openLyricsEditor() {
    if (!player.current || !player.lyrics.length) {
        showToast('warning', t('lyrics.none'));
        return;
    }
    player.lyricsEditorOpen = true;
    const modal = document.getElementById('lyrics-editor-modal');
    if (!modal) return;
    
    // Build timeline
    buildLyricsEditorTimeline();
    
    modal.hidden = false;
    player.lyricsEditorOpen = true;
}

function closeLyricsEditor() {
    const modal = document.getElementById('lyrics-editor-modal');
    if (modal) modal.hidden = true;
    player.lyricsEditorOpen = false;
}

function buildLyricsEditorTimeline() {
    const timeline = document.getElementById('lyrics-editor-timeline');
    if (!timeline) return;
    timeline.innerHTML = '';
    
    if (!audio.duration) return;
    
    const duration = audio.duration;
    const pixelsPerSecond = timeline.clientWidth / duration;
    
    player.lyrics.forEach((line, index) => {
        const marker = document.createElement('div');
        marker.className = 'lyrics-editor-marker';
        marker.style.left = (line.time / 1000) * pixelsPerSecond + 'px';
        marker.dataset.index = index;
        marker.title = fmtTime(line.time / 1000) + ' - ' + (line.text || '');
        timeline.appendChild(marker);
    });
    
    // Seek bar for editor
    const seekBar = document.getElementById('lyrics-editor-seek');
    if (seekBar && audio.duration) {
        seekBar.max = Math.floor(audio.duration);
        seekBar.value = Math.floor(audio.currentTime);
    }
    
    updateLyricsEditorTime();
}

function updateLyricsEditorTime() {
    const timeEl = document.getElementById('lyrics-editor-time');
    const seekBar = document.getElementById('lyrics-editor-seek');
    if (timeEl && audio.duration) {
        timeEl.textContent = fmtTime(audio.currentTime) + ' / ' + fmtTime(audio.duration);
        if (seekBar) {
            seekBar.value = Math.floor(audio.currentTime);
        }
    }
}

function closeLyricsEditor() {
    const modal = document.getElementById('lyrics-editor-modal');
    if (modal) modal.hidden = true;
    player.lyricsEditorOpen = false;
}

function applyLyricsEditorChanges() {
    // Apply changes to current playback (would need to re-render with updated timings)
    showToast('info', t('lyrics.editor_apply'), 2000);
    closeLyricsEditor();
}

function saveLyricsFromEditor() {
    // Save lyrics to local file
    if (!player.current) return;
    loadLyrics(player.current, true);
    showToast('info', t('lyrics.editor_save'), 2000);
    closeLyricsEditor();
}

function applyLyricsEditorChanges() {
    // Apply changes to current playback
    showToast('info', t('lyrics.editor_apply'), 2000);
    closeLyricsEditor();
}

function updateLyricHighlight() {
    if (!player.lyrics.length) return;
    const ms = audio.currentTime * 1000;
    let low = 0;
    let high = player.lyrics.length - 1;
    let found = -1;
    while (low <= high) {
        const mid = (low + high) >> 1;
        if (player.lyrics[mid].time <= ms) { found = mid; low = mid + 1; }
        else high = mid - 1;
    }
    if (found === player.lyricIndex) return;
    const box = document.getElementById('lyrics-scroll');
    if (!box) return;
    const previous = box.querySelector('.lyric-line.is-active');
    if (previous) previous.classList.remove('is-active');
    const lines = box.querySelectorAll('.lyric-line');
    const line = lines[found];
    player.lyricIndex = found;
    if (!line) return;
    line.classList.add('is-active');
    if (player.autoScroll) {
        const top = line.offsetTop - box.clientHeight / 2 + line.clientHeight / 2;
        box.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
    }
}

/* ------------------------------------------------------------- now playing */

function renderNowPlaying() {
    const track = player.current;
    const resolved = player.resolved || {};
    const title = document.getElementById('np-title');
    const artist = document.getElementById('np-artist');
    const album = document.getElementById('np-album');
    const kind = document.getElementById('np-kind');
    const plays = document.getElementById('np-plays');
    const quality = document.getElementById('np-quality');
    const cover = document.getElementById('np-cover');
    const barCover = document.getElementById('pb-cover');
    const barTitle = document.getElementById('pb-title');
    const barArtist = document.getElementById('pb-artist');

    if (!track) {
        if (title) title.textContent = t('player.nothing_playing');
        [artist, album].forEach((el) => { if (el) el.textContent = ''; });
        [kind, plays, quality].forEach((el) => { if (el) el.textContent = ''; });
        if (cover) { cover.removeAttribute('src'); cover.classList.add('is-empty'); }
        if (barCover) { barCover.removeAttribute('src'); barCover.classList.add('is-empty'); }
        if (barTitle) barTitle.textContent = t('player.nothing_playing');
        if (barArtist) barArtist.textContent = '';
        updateFavoriteButton();
        return;
    }

    if (title) title.textContent = track.name || '-';
    if (artist) artist.textContent = track.artists || '';
    if (album) album.textContent = track.album || '';
    if (kind) {
        kind.textContent = t('player.badge_' + (resolved.kind || track.source || 'online'));
    }
    const count = playCountOf(track);
    if (plays) plays.textContent = count ? t('player.play_count', { count }) : '';
    if (quality) {
        const level = resolved.quality || resolved.level;
        quality.textContent = levelText(level);
    }

    const art = resolved.cover_url || track.cover_url || '';
    [cover, barCover].forEach((image) => {
        if (!image) return;
        if (art) {
            image.src = art;
            image.classList.remove('is-empty');
            image.onerror = () => { image.removeAttribute('src'); image.classList.add('is-empty'); };
        } else {
            image.removeAttribute('src');
            image.classList.add('is-empty');
        }
    });
    if (barTitle) barTitle.textContent = track.name || '-';
    if (barArtist) barArtist.textContent = track.artists || '';
    updateFavoriteButton();
}

function updateFavoriteButton() {
    const button = document.getElementById('pb-fav');
    if (!button) return;
    const track = player.current;
    if (!track) {
        button.textContent = '♡';
        button.classList.remove('fav-on');
        return;
    }
    const favorite = player.favoriteKeys.has(trackKeyOf(track));
    button.textContent = favorite ? '♥' : '♡';
    button.classList.toggle('fav-on', favorite);
    button.classList.toggle('fav-off', !favorite);
}

function highlightPlayingRow() {
    const key = currentKey();
    document.querySelectorAll('.track-row').forEach((row) => {
        const isPlaying = row.dataset.trackKey === key && !!key;
        row.classList.toggle('is-playing', isPlaying);
        const idx = row.querySelector('.idx');
        if (idx) {
            if (isPlaying) {
                if (!idx.dataset.number) idx.dataset.number = idx.textContent;
                idx.textContent = '♪';
            } else if (idx.dataset.number) {
                idx.textContent = idx.dataset.number;
            }
        }
    });
}

function updatePlayButton() {
    const button = document.getElementById('pb-play');
    if (!button) return;
    const playing = !audio.paused && !audio.ended;
    button.textContent = playing ? '❚❚' : '▶';
    const label = playing ? t('player.pause') : t('player.play');
    button.title = label;
    button.setAttribute('aria-label', label);
}

function updateProgressUi() {
    const fill = document.getElementById('pb-fill');
    const current = document.getElementById('pb-current');
    const duration = document.getElementById('pb-duration');
    const total = isFinite(audio.duration) ? audio.duration : 0;
    const ratio = total ? (audio.currentTime / total) * 100 : 0;
    if (fill) fill.style.width = `${Math.min(100, Math.max(0, ratio))}%`;
    if (current) current.textContent = fmtTime(audio.currentTime);
    if (duration) duration.textContent = total ? fmtTime(total) : (player.current ? fmtDuration(player.current.duration) : '0:00');
}

/* ------------------------------------------------------ session persistence */

function saveSession(force) {
    if (!state.api || !player.queue.length) return;
    const now = Date.now();
    if (!force && now - player.lastSaveAt < 5000) return;
    player.lastSaveAt = now;
    state.api.save_playback_state({
        queue: player.queue,
        index: player.index,
        position: audio.currentTime || 0,
        mode: player.mode,
        source: player.source,
    });
}

function startSessionTimer() {
    setInterval(() => {
        if (!audio.paused) saveSession(false);
    }, 5000);
    window.addEventListener('beforeunload', () => saveSession(true));
}

async function restoreSession() {
    if (!state.settings || state.settings.playback.resume_playback === false) return;
    const result = await state.api.load_playback_state();
    const session = result && result.state;
    if (!session || !session.queue || !session.queue.length) return;
    player.queue = session.queue;
    player.source = session.source || null;
    player.mode = session.mode || player.mode;
    player.index = Math.max(0, Math.min(session.index || 0, session.queue.length - 1));
    updateModeButton();
    const track = player.queue[player.index];
    player.current = track;
    renderNowPlaying();
    // Load the track and seek back to where the last session stopped. Playback
    // deliberately starts paused: WebKit refuses audible autoplay without a
    // gesture anyway, and resuming silently would surprise the user.
    const ok = await loadCurrent({ autoplay: false, position: session.position || 0 });
    if (ok) {
        showToast('info', t('player.resumed', { name: track.name || '' }), 4200);
        updatePlayButton();
    }
}

/* --------------------------------------------------------- quick download */

async function downloadTrack(track) {
    if (!track) return;
    if (track.source === 'local') {
        showToast('info', t('player.download_local_only'));
        return;
    }
    const result = await state.api.download_tracks([track], downloadOptions({}));
    if (result && result.success) {
        showToast('success', t('player.download_queued', { name: track.name || '' }));
        updateStatus('downloading', { current: 0, total: result.total || 1 });
    } else {
        showError(result, 'download.start_failed');
    }
}

/* ------------------------------------------------------------- local music */

async function addLocalFolder() {
    const result = await state.api.add_local_folder(true);
    if (!result.success) {
        if (!result.cancelled) showError(result, 'error.permission');
        return;
    }
    if (result.settings) state.settings = result.settings;
    renderFolderList();
    updateLocalStatsText();
    showToast('success', t('player.scan_done', { count: result.tracks || 0 }));
    if (player.sourceTab === 'local') await loadLocal();
}

async function removeLocalFolder(directory) {
    const result = await state.api.remove_local_folder(directory);
    if (!result.success) return;
    state.settings = await state.api.get_settings();
    renderFolderList();
    showToast('info', t('settings.saved'));
}

async function rescanLocal() {
    const button = document.getElementById('btn-rescan');
    if (button) button.disabled = true;
    try {
        const result = await state.api.scan_local_music(false);
        showToast('success', t('player.scan_done', { count: result.tracks || 0 }));
        updateLocalStatsText();
        if (player.sourceTab === 'local') await loadLocal();
    } finally {
        if (button) button.disabled = false;
    }
}

function updateLocalStatsText() {
    const el = document.getElementById('local-stats-text');
    if (!el) return;
    state.api.get_local_stats().then((result) => {
        const stats = (result && result.stats) || {};
        const directories = (result && result.directories) || [];
        el.textContent = t('settings.local_stats', {
            tracks: stats.tracks || 0, folders: directories.length,
        });
    });
}

function updatePlayStatsText() {
    const el = document.getElementById('play-stats-text');
    if (!el) return;
    const total = Object.values(player.counts).reduce((sum, value) => sum + (value || 0), 0);
    el.textContent = t('settings.play_stats', {
        tracks: Object.keys(player.counts).length,
        plays: total,
        favorites: player.favoriteKeys.size,
    });
}

async function resetPlayCounts() {
    if (!await confirmDialog('settings.reset_play_counts_confirm')) return;
    const result = await state.api.reset_play_counts();
    if (result && result.success) {
        player.counts = {};
        renderTrackList();
        renderNowPlaying();
        updatePlayStatsText();
        showToast('info', t('settings.reset_play_counts_done'));
    }
}

/* ============================================================================
   Phase 1: Core Playback Experience - New Functions
   ============================================================================ */

/* ----------------------------------------------------------- Queue Drawer */

function toggleQueueDrawer() {
    const drawer = document.getElementById('queue-drawer');
    const backdrop = document.getElementById('queue-drawer-backdrop');
    if (!drawer || !backdrop) return;

    player.queueDrawerOpen = !player.queueDrawerOpen;
    drawer.hidden = !player.queueDrawerOpen;
    backdrop.hidden = !player.queueDrawerOpen;

    // Force reflow for animation
    if (player.queueDrawerOpen) {
        requestAnimationFrame(() => {
            drawer.classList.add('is-open');
            backdrop.classList.add('is-open');
        });
        renderQueueDrawer();
    } else {
        drawer.classList.remove('is-open');
        backdrop.classList.remove('is-open');
    }
}

function closeQueueDrawer() {
    const drawer = document.getElementById('queue-drawer');
    const backdrop = document.getElementById('queue-drawer-backdrop');
    if (!drawer || !backdrop) return;

    player.queueDrawerOpen = false;
    drawer.classList.remove('is-open');
    backdrop.classList.remove('is-open');
    // Wait for animation to finish before hiding
    setTimeout(() => {
        if (!player.queueDrawerOpen) {
            drawer.hidden = true;
            backdrop.hidden = true;
        }
    }, 250);
}

function renderQueueDrawer() {
    const list = document.getElementById('queue-drawer-list');
    if (!list) return;

    if (!player.queue.length) {
        list.innerHTML = `<p class="empty">${t('player.queue_empty')}</p>`;
        return;
    }

    list.innerHTML = '';
    const fragment = document.createDocumentFragment();

    player.queue.forEach((track, index) => {
        const item = document.createElement('div');
        item.className = 'queue-item-drawer';
        item.draggable = true;
        item.dataset.index = index;
        if (index === player.index) item.classList.add('playing');

        const idx = document.createElement('span');
        idx.className = 'idx';
        idx.textContent = index === player.index ? '♪' : String(index + 1);

        const main = document.createElement('div');
        main.className = 'queue-item-main';
        const name = document.createElement('div');
        name.className = 'queue-item-name';
        name.textContent = track.name || '-';
        const sub = document.createElement('div');
        sub.className = 'queue-item-sub';
        sub.textContent = [track.artists, track.album].filter(Boolean).join(' · ');
        main.append(name, sub);

        const actions = document.createElement('div');
        actions.className = 'queue-item-actions';

        const playBtn = document.createElement('button');
        playBtn.className = 'btn btn-icon';
        playBtn.type = 'button';
        playBtn.textContent = '▶';
        playBtn.title = t('player.play');
        playBtn.addEventListener('click', (e) => { e.stopPropagation(); playAt(index, { autoplay: true }); });
        actions.appendChild(playBtn);

        const moveTopBtn = document.createElement('button');
        moveTopBtn.className = 'btn btn-icon';
        moveTopBtn.type = 'button';
        moveTopBtn.textContent = '⬆';
        moveTopBtn.title = t('player.queue_move_top');
        moveTopBtn.addEventListener('click', (e) => { e.stopPropagation(); moveQueueItem(index, 0); });
        actions.appendChild(moveTopBtn);

        const moveBottomBtn = document.createElement('button');
        moveBottomBtn.className = 'btn btn-icon';
        moveBottomBtn.type = 'button';
        moveBottomBtn.textContent = '⬇';
        moveBottomBtn.title = t('player.queue_move_bottom');
        moveBottomBtn.addEventListener('click', (e) => { e.stopPropagation(); moveQueueItem(index, player.queue.length - 1); });
        actions.appendChild(moveBottomBtn);

        const removeBtn = document.createElement('button');
        removeBtn.className = 'btn btn-icon';
        removeBtn.type = 'button';
        removeBtn.textContent = '✕';
        removeBtn.title = t('player.queue_remove');
        removeBtn.addEventListener('click', (e) => { e.stopPropagation(); removeFromQueue(index); });
        actions.appendChild(removeBtn);

        item.append(idx, main, actions);

        // Drag and drop
        item.addEventListener('dragstart', handleDragStart);
        item.addEventListener('dragover', handleDragOver);
        item.addEventListener('drop', handleDrop);
        item.addEventListener('dragend', handleDragEnd);

        fragment.appendChild(item);
    });

    list.appendChild(fragment);
}

let dragSourceIndex = -1;

function handleDragStart(e) {
    dragSourceIndex = parseInt(e.currentTarget.dataset.index, 10);
    e.currentTarget.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', dragSourceIndex);
}

function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const target = e.currentTarget.closest('.queue-item-drawer');
    if (target) target.classList.add('drag-over');
}

function handleDrop(e) {
    e.preventDefault();
    const target = e.currentTarget.closest('.queue-item-drawer');
    if (!target) return;
    const targetIndex = parseInt(target.dataset.index, 10);
    target.classList.remove('drag-over');
    if (dragSourceIndex !== targetIndex) {
        moveQueueItem(dragSourceIndex, targetIndex);
    }
}

function handleDragEnd(e) {
    e.currentTarget.classList.remove('dragging');
    document.querySelectorAll('.queue-item-drawer').forEach(el => el.classList.remove('drag-over'));
}

function moveQueueItem(fromIndex, toIndex) {
    if (fromIndex < 0 || fromIndex >= player.queue.length) return;
    if (toIndex < 0 || toIndex >= player.queue.length) return;

    const [item] = player.queue.splice(fromIndex, 1);
    player.queue.splice(toIndex, 0, item);

    // Adjust player.index if needed
    if (player.index === fromIndex) {
        player.index = toIndex;
    } else if (fromIndex < player.index && toIndex >= player.index) {
        player.index--;
    } else if (fromIndex > player.index && toIndex <= player.index) {
        player.index++;
    }

    renderQueueDrawer();
    renderTrackList();
    highlightPlayingRow();
    saveSession(true);
}

function removeFromQueue(index) {
    if (index < 0 || index >= player.queue.length) return;

    player.queue.splice(index, 1);

    if (player.index === index) {
        // Current track removed, play next or stop
        if (player.queue.length > 0) {
            player.index = Math.min(index, player.queue.length - 1);
            playAt(player.index, { autoplay: true });
        } else {
            player.index = -1;
            player.current = null;
            audio.pause();
            audio.src = '';
            renderNowPlaying();
        }
    } else if (index < player.index) {
        player.index--;
    }

    renderQueueDrawer();
    renderTrackList();
    highlightPlayingRow();
    saveSession(true);
}

function clearQueue() {
    if (!player.queue.length) return;
    if (!confirm(t('player.queue_clear_confirm') || 'Clear the entire queue?')) return;

    player.queue = [];
    player.index = -1;
    player.current = null;
    audio.pause();
    audio.src = '';
    renderQueueDrawer();
    renderTrackList();
    renderNowPlaying();
    saveSession(true);
    showToast('info', t('player.queue_cleared') || 'Queue cleared');
}

function addTracksToQueue(tracks) {
    if (!tracks || !tracks.length) return;
    const existingKeys = new Set(player.queue.map(t => t.key));
    const newTracks = tracks.filter(t => t.key && !existingKeys.has(t.key));
    player.queue.push(...newTracks);
    renderQueueDrawer();
    renderTrackList();
    showToast('info', t('player.added_to_queue', { count: newTracks.length }) || `Added ${newTracks.length} tracks to queue`);
}

function downloadTracks(tracks) {
    if (!tracks || !tracks.length) return;
    const onlineTracks = tracks.filter(t => t.source === 'online' && t.id);
    if (!onlineTracks.length) {
        showToast('info', t('player.download_local_only'));
        return;
    }
    state.api.download_tracks(onlineTracks, downloadOptions({})).then(result => {
        if (result && result.success) {
            showToast('success', t('player.download_queued', { name: `${onlineTracks.length} tracks` }));
            updateStatus('downloading', { current: 0, total: result.total || onlineTracks.length });
        } else {
            showError(result, 'download.start_failed');
        }
    });
}

async function saveQueueAsPlaylist() {
    if (!player.queue.length) {
        showToast('warning', t('player.queue_empty'));
        return;
    }

    const name = prompt(t('player.playlist_name_prompt') || 'Playlist name:');
    if (!name) return;

    try {
        const result = await state.api.save_queue_as_playlist(name, player.queue);
        if (result.success) {
            showToast('success', result.message || t('player.queue_saved_as_playlist', { name }));
        } else {
            showError(result, 'error.unknown');
        }
    } catch (err) {
        showToast('error', t('error.unknown') + ': ' + err.message);
    }
}

/* ----------------------------------------------------------- Desktop Lyrics */

function toggleDesktopLyrics() {
    if (player.desktopLyricsWindow && !player.desktopLyricsWindow.closed) {
        closeDesktopLyrics();
    } else {
        openDesktopLyrics();
    }
}

function openDesktopLyrics() {
    if (player.desktopLyricsWindow && !player.desktopLyricsWindow.closed) {
        player.desktopLyricsWindow.focus();
        return;
    }

    const url = new URL('desktop-lyrics.html', window.location.href);
    // Pass settings via URL params
    url.searchParams.set('fontSize', state.settings?.ui?.desktop_lyrics_font_size || 18);
    url.searchParams.set('color', encodeURIComponent(state.settings?.ui?.desktop_lyrics_color || '#ffffff'));
    url.searchParams.set('opacity', state.settings?.ui?.desktop_lyrics_opacity || 0.9);
    url.searchParams.set('showTranslation', state.settings?.ui?.desktop_lyrics_show_translation !== false);

    // Open as a pywebview window (will be handled by main.py) or fallback to window.open
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_desktop_lyrics) {
        // Use pywebview to open a new window
        window.pywebview.api.open_desktop_lyrics();
    } else {
        // Fallback for development
        player.desktopLyricsWindow = window.open(url.toString(), 'desktop-lyrics', `
            width=600,height=120,
            resizable=yes,
            alwaysRaised=yes,
            toolbar=no,
            menubar=no,
            location=no,
            status=no
        `);
    }

    // Push current lyrics if playing
    if (player.lyrics.length > 0) {
        setTimeout(() => pushLyricsToDesktop(), 500);
    }
}

function closeDesktopLyrics() {
    if (player.desktopLyricsWindow && !player.desktopLyricsWindow.closed) {
        player.desktopLyricsWindow.close();
    }
    player.desktopLyricsWindow = null;
}

function pushLyricsToDesktop() {
    // Push via backend to native pywebview window
    if (state.api && state.api.update_desktop_lyrics) {
        state.api.update_desktop_lyrics(player.lyrics, player.lyricIndex).catch(() => { /* ignore */ });
    }

    // Also try direct window communication as fallback
    if (player.desktopLyricsWindow && !player.desktopLyricsWindow.closed) {
        const payload = {
            action: 'update',
            lines: player.lyrics,
            index: player.lyricIndex
        };
        try {
            player.desktopLyricsWindow.postMessage({ type: 'ncm:desktop-lyrics', payload }, '*');
        } catch (e) {
            try {
                localStorage.setItem('ncm:desktop-lyrics:update', JSON.stringify(payload));
            } catch (e2) { /* ignore */ }
        }
    }
}

function onOpenDesktopLyrics() {
    // Called from backend when user clicks desktop lyrics button
    // The backend already opened the native window, we just need to push current lyrics
    setTimeout(() => pushLyricsToDesktop(), 500);
}

function onCloseDesktopLyrics() {
    player.desktopLyricsWindow = null;
}

function onDesktopLyricsUpdate(payload) {
    // Called from backend when lyrics update
    pushLyricsToDesktop();
}

/* ----------------------------------------------------------- Crossfade */

function initCrossfade() {
    // Create second audio element for crossfade
    player.crossfadeAudio = document.createElement('audio');
    player.crossfadeAudio.preload = 'metadata';
    player.crossfadeAudio.style.display = 'none';
    document.body.appendChild(player.crossfadeAudio);

    // Load crossfade duration from settings
    player.crossfadeDuration = state.settings?.playback?.crossfade_duration || 0;
    updateCrossfadeLabel();
}

function updateCrossfadeLabel() {
    const label = document.getElementById('crossfade-value');
    if (label) {
        label.textContent = player.crossfadeDuration > 0 ? `${player.crossfadeDuration}s` : t('player.crossfade_off');
    }
}

async function loadCurrentWithCrossfade(options) {
    const opts = options || {};
    const track = player.queue[player.index];
    if (!track) return false;

    player.current = track;
    player.resolved = null;
    player.pendingPosition = opts.position || 0;
    renderNowPlaying();
    highlightPlayingRow();
    renderLyricsLoading();

    let resolved;
    try {
        resolved = await state.api.resolve_track(track);
    } catch (err) {
        resolved = { success: false, message: String(err && err.message || err) };
    }
    if (!resolved || !resolved.success) {
        showToast('error', `${t('player.unavailable')}: ${track.name || ''}`, 4200);
        if (opts.autoplay) setTimeout(() => advance(true), 900);
        updatePlayButton();
        return false;
    }

    player.resolved = resolved;
    if (resolved.kind === 'local') {
        if (resolved.name) track.name = resolved.name;
        if (resolved.artists) track.artists = resolved.artists;
        if (resolved.album != null) track.album = resolved.album;
        if (resolved.duration) track.duration = resolved.duration;
        if (resolved.matched_local) {
            showToast('info', t('player.playing_local_copy'), 2600);
        }
    }
    renderNowPlaying();
    highlightPlayingRow();
    loadLyrics(track);

    // Crossfade logic
    if (player.crossfadeDuration > 0 && !opts.position) {
        await crossfadeTo(resolved.url, opts.autoplay !== false);
    } else {
        audio.src = resolved.url;
        audio.load();
        player.pendingRecord = true;
        if (opts.autoplay !== false) await playAudio();
    }

    // Preload next track
    preloadNextTrack();

    return true;
}

// Replace the original loadCurrent with crossfade version
const originalLoadCurrent = loadCurrent;
loadCurrent = loadCurrentWithCrossfade;

async function crossfadeTo(newSrc, autoplay) {
    const currentAudio = audio;
    const nextAudio = player.crossfadeAudio;
    const duration = player.crossfadeDuration * 1000; // ms

    nextAudio.src = newSrc;
    nextAudio.load();
    nextAudio.volume = 0;

    // Wait for next audio to be ready
    await new Promise((resolve) => {
        const onCanPlay = () => {
            nextAudio.removeEventListener('canplay', onCanPlay);
            resolve();
        };
        nextAudio.addEventListener('canplay', onCanPlay);
        // Timeout fallback
        setTimeout(resolve, 5000);
    });

    if (autoplay) {
        await nextAudio.play().catch(() => { /* ignore */ });
    }

    // Crossfade: fade out current, fade in next
    const steps = 20;
    const stepTime = duration / steps;
    const startVol = currentAudio.volume;
    const targetVol = player.volume;

    for (let i = 0; i <= steps; i++) {
        const progress = i / steps;
        currentAudio.volume = startVol * (1 - progress);
        nextAudio.volume = targetVol * progress;
        await new Promise(r => setTimeout(r, stepTime));
    }

    // Swap audio elements
    currentAudio.pause();
    currentAudio.src = '';
    currentAudio.volume = targetVol;

    // Make nextAudio the primary
    audio.src = nextAudio.src;
    audio.currentTime = nextAudio.currentTime;
    nextAudio.src = '';
    nextAudio.volume = 0;

    player.pendingRecord = true;
}

function preloadNextTrack() {
    if (!player.queue.length || player.index < 0) return;

    const nextIndex = nextIndex(true);
    if (nextIndex < 0 || nextIndex === player.index) return;

    const nextTrack = player.queue[nextIndex];
    if (!nextTrack || nextTrack.source !== 'online' || !nextTrack.id) return;

    const quality = state.settings?.playback?.online_quality || 'standard';
    state.api.preload_next_track(nextTrack.id, quality).then(result => {
        if (result.success && result.preloaded) {
            player.preloadedNextUrl = result.url;
            player.preloadedNextTrack = nextTrack;
        }
    }).catch(() => { /* ignore */ });
}

/* ----------------------------------------------------------- Media Session API */

function initMediaSession() {
    if (!('mediaSession' in navigator)) {
        player.mediaSessionSupported = false;
        return;
    }

    player.mediaSessionSupported = true;

    navigator.mediaSession.setActionHandler('play', () => togglePlay());
    navigator.mediaSession.setActionHandler('pause', () => togglePlay());
    navigator.mediaSession.setActionHandler('previoustrack', () => playPrevious());
    navigator.mediaSession.setActionHandler('nexttrack', () => playNext());
    navigator.mediaSession.setActionHandler('seekto', (details) => {
        if (details.seekTime !== undefined) {
            audio.currentTime = details.seekTime;
        }
    });

    // Update metadata when track changes
    updateMediaSessionMetadata();
}

function updateMediaSessionMetadata() {
    if (!player.mediaSessionSupported || !player.current) return;

    const track = player.current;
    const resolved = player.resolved || {};

    navigator.mediaSession.metadata = new MediaMetadata({
        title: track.name || 'Unknown',
        artist: track.artists || 'Unknown',
        album: track.album || 'Unknown',
        artwork: (resolved.cover_url || track.cover_url || '').split(',').map(url => ({
            src: url.trim(),
            sizes: '512x512',
            type: 'image/png'
        })).filter(a => a.src)
    });

    navigator.mediaSession.playbackState = audio.paused ? 'paused' : 'playing';

    // Update position state for seek bar on lock screen
    if (audio.duration) {
        navigator.mediaSession.setPositionState({
            duration: audio.duration,
            playbackRate: audio.playbackRate,
            position: audio.currentTime
        });
    }
}

// Update media session on play/pause/timeupdate
const originalOnAudioPlay = onAudioPlay;
onAudioPlay = function() {
    originalOnAudioPlay();
    updateMediaSessionMetadata();
};

const originalOnAudioPause = onAudioPause;
onAudioPause = function() {
    originalOnAudioPause();
    updateMediaSessionMetadata();
};

const originalUpdateProgressUi = updateProgressUi;
updateProgressUi = function() {
    originalUpdateProgressUi();
    if (player.mediaSessionSupported && audio.duration) {
        navigator.mediaSession.setPositionState({
            duration: audio.duration,
            playbackRate: audio.playbackRate,
            position: audio.currentTime
        });
    }
};

/* ----------------------------------------------------------- Sleep Timer */

function initSleepTimer() {
    // Restore sleep timer from settings
    const minutes = state.settings?.playback?.sleep_timer_minutes || 0;
    if (minutes > 0) {
        startSleepTimer(minutes, 'pause');
    }
}

function startSleepTimer(minutes, action) {
    clearSleepTimer();

    if (minutes <= 0) return;

    player.sleepTimer.minutes = minutes;
    player.sleepTimer.action = action || 'pause';

    const totalMs = minutes * 60 * 1000;
    const fadeMs = 30000; // 30 seconds fade out

    // Main timer
    player.sleepTimer.timerId = setTimeout(() => {
        executeSleepAction();
    }, totalMs - fadeMs);

    // Fade out timer (starts 30 seconds before action)
    if (fadeMs < totalMs) {
        player.sleepTimer.fadeTimerId = setTimeout(() => {
            startFadeOut(fadeMs);
        }, totalMs - fadeMs);
    }

    updateSleepTimerUI();
    showToast('info', t('player.sleep_timer_set', { minutes, action: t(`player.sleep_timer_action_${action}`) }), 3000);
}

function clearSleepTimer() {
    if (player.sleepTimer.timerId) {
        clearTimeout(player.sleepTimer.timerId);
        player.sleepTimer.timerId = null;
    }
    if (player.sleepTimer.fadeTimerId) {
        clearTimeout(player.sleepTimer.fadeTimerId);
        player.sleepTimer.fadeTimerId = null;
    }
    player.sleepTimer.minutes = 0;
    audio.volume = player.volume; // Ensure volume is restored
    updateSleepTimerUI();
}

function startFadeOut(durationMs) {
    const startVol = audio.volume;
    const steps = 30;
    const stepTime = durationMs / steps;
    let step = 0;

    const fade = () => {
        step++;
        audio.volume = startVol * (1 - step / steps);
        if (step < steps) {
            setTimeout(fade, stepTime);
        }
    };
    fade();
}

function executeSleepAction() {
    switch (player.sleepTimer.action) {
        case 'stop':
            audio.pause();
            audio.currentTime = 0;
            audio.src = '';
            player.current = null;
            player.index = -1;
            renderNowPlaying();
            break;
        case 'pause':
            audio.pause();
            break;
        case 'quit':
            if (window.pywebview && window.pywebview.api) {
                window.pywebview.api.logout(); // This will close the app
            } else {
                window.close();
            }
            break;
    }
    clearSleepTimer();
    showToast('info', t('player.sleep_timer_triggered'), 4000);
}

function updateSleepTimerUI() {
    const btn = document.getElementById('pb-sleep-timer');
    const select = document.getElementById('sleep-timer-select');
    if (btn) {
        btn.classList.toggle('active', player.sleepTimer.minutes > 0);
    }
    if (select) {
        select.value = String(player.sleepTimer.minutes);
    }
}

function showSleepTimerPopover() {
    // Simple implementation: cycle through options
    const options = [0, 15, 30, 60, 90, 120];
    const currentIndex = options.indexOf(player.sleepTimer.minutes);
    const nextIndex = (currentIndex + 1) % options.length;
    const minutes = options[nextIndex];

    if (minutes > 0) {
        startSleepTimer(minutes, player.sleepTimer.action || 'pause');
    } else {
        clearSleepTimer();
    }
}

/* ----------------------------------------------------------- Playback Stats Dashboard */

async function renderPlaybackStatsDashboard() {
    const container = document.getElementById('playback-stats-dashboard');
    if (!container) return;

    try {
        const result = await state.api.get_detailed_playback_stats();
        if (!result.success) return;

        container.innerHTML = `
            <div class="stats-dashboard">
                <div class="stat-card">
                    <div class="stat-value">${result.total_time_hours || 0}h</div>
                    <div class="stat-label">${t('player.stats_total_time')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${result.tracks || 0}</div>
                    <div class="stat-label">${t('player.stats_tracks')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${result.total_plays || 0}</div>
                    <div class="stat-label">${t('player.stats_plays')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${result.this_week_plays || 0}</div>
                    <div class="stat-label">${t('player.stats_this_week')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${result.this_month_plays || 0}</div>
                    <div class="stat-label">${t('player.stats_this_month')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${result.favorites || 0}</div>
                    <div class="stat-label">${t('player.favorite')}</div>
                </div>
            </div>
            ${result.top_artists?.length ? `
                <div style="margin-top: 16px;">
                    <h4 style="font-size: 12px; color: var(--text-2); margin-bottom: 8px;">${t('player.stats_top_artists')}</h4>
                    <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                        ${result.top_artists.slice(0, 5).map(a => `<span class="pill">${a.name} (${a.count})</span>`).join('')}
                    </div>
                </div>
            ` : ''}
            ${result.top_albums?.length ? `
                <div style="margin-top: 16px;">
                    <h4 style="font-size: 12px; color: var(--text-2); margin-bottom: 8px;">${t('player.stats_top_albums')}</h4>
                    <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                        ${result.top_albums.slice(0, 5).map(a => `<span class="pill">${a.name} (${a.count})</span>`).join('')}
                    </div>
                </div>
            ` : ''}
        `;
    } catch (err) {
        console.error('Failed to render playback stats:', err);
    }
}

// Call this when settings modal playback tab is shown
function onPlaybackSettingsTabShown() {
    renderPlaybackStatsDashboard();
}

/* ----------------------------------------------------------- Settings Helper Functions */

function updateDesktopLyricsOpacityLabel() {
    const label = document.getElementById('desktop-lyrics-opacity-value');
    const slider = document.getElementById('desktop-lyrics-opacity');
    if (label && slider) {
        label.textContent = `${Math.round(slider.value * 100)}%`;
    }
}

/* ----------------------------------------------------------- Backend Callbacks */

function onOpenDesktopLyrics() {
    openDesktopLyrics();
}

function onCloseDesktopLyrics() {
    closeDesktopLyrics();
}

function onDesktopLyricsUpdate(payload) {
    pushLyricsToDesktop();
}

function onSleepTimerSet(payload) {
    player.sleepTimer.minutes = payload.minutes;
    player.sleepTimer.action = payload.action;
    updateSleepTimerUI();
}

/* ------------------------------------------------------------------ about */

function renderAbout() {
    if (!state.appInfo) return;
    const version = document.getElementById('about-version');
    if (version) version.textContent = t('about.version', { version: state.appInfo.version });
    const author = document.getElementById('about-author');
    if (author) author.textContent = t('about.author', { author: state.appInfo.author });
}

/* ------------------------------------------------------- static listeners */

function bindStaticEvents() {
    // top level tabs
    document.getElementById('top-tabs').addEventListener('click', (event) => {
        const tab = event.target.closest('.top-tab');
        if (tab) showView(tab.dataset.view);
    });

    // download view
    document.getElementById('btn-fetch').addEventListener('click', fetchPlaylist);
    document.getElementById('playlist-input').addEventListener('keydown', (event) => {
        if (event.key === 'Enter') fetchPlaylist();
    });
    document.getElementById('btn-select-all').addEventListener('click', selectAll);
    document.getElementById('btn-select-none').addEventListener('click', deselectAll);
    document.getElementById('btn-start').addEventListener('click', startDownload);
    document.getElementById('btn-pause').addEventListener('click', togglePause);
    document.getElementById('btn-cancel').addEventListener('click', cancelDownload);
    document.getElementById('btn-open-folder').addEventListener('click', openFolder);
    document.getElementById('btn-settings').addEventListener('click', () => {
        openModal('settings-modal');
        updateLocalStatsText();
        updatePlayStatsText();
    });
    document.getElementById('btn-theme').addEventListener('click', cycleTheme);

    // login
    document.getElementById('btn-refresh-qrcode').addEventListener('click', refreshQrcode);
    document.getElementById('btn-send-code').addEventListener('click', sendPhoneCode);
    document.getElementById('btn-phone-login').addEventListener('click', doPhoneLogin);
    document.getElementById('btn-cookie-login').addEventListener('click', doCookieLogin);

    // settings
    document.getElementById('btn-choose-dir').addEventListener('click', chooseDirectory);
    document.getElementById('btn-save-settings').addEventListener('click', saveSettings);
    document.getElementById('btn-reset-settings').addEventListener('click', resetSettings);
    document.getElementById('language-select').addEventListener('change', (event) => {
        changeLanguage(event.target.value);
    });
    document.getElementById('theme-select').addEventListener('change', (event) => {
        setTheme(event.target.value, true);
    });
    document.getElementById('btn-open-homepage').addEventListener('click', async () => {
        if (state.appInfo) await state.api.open_external(state.appInfo.homepage);
    });

    // appearance
    document.getElementById('btn-choose-background').addEventListener('click', chooseBackground);
    document.getElementById('btn-clear-background').addEventListener('click', clearBackground);
    ['background-blur', 'background-dim'].forEach((id) => {
        document.getElementById(id).addEventListener('input', () => {
            updateRangeLabels();
            applyAppearance({
                ui: {
                    background_url: (state.settings && state.settings.ui.background_url) || '',
                    background_blur: parseInt(document.getElementById('background-blur').value, 10),
                    background_dim: parseInt(document.getElementById('background-dim').value, 10),
                    glass: document.getElementById('glass-check').checked,
                    accent_from_background: document.getElementById('accent-check').checked,
                },
            });
        });
    });
    document.getElementById('glass-check').addEventListener('change', () => {
        applyAppearance({
            ui: {
                background_url: (state.settings && state.settings.ui.background_url) || '',
                background_blur: parseInt(document.getElementById('background-blur').value, 10),
                background_dim: parseInt(document.getElementById('background-dim').value, 10),
                glass: document.getElementById('glass-check').checked,
                accent_from_background: document.getElementById('accent-check').checked,
            },
        });
    });
    document.getElementById('btn-add-folder-settings').addEventListener('click', addLocalFolder);
    document.getElementById('btn-rescan-settings').addEventListener('click', async () => {
        await rescanLocal();
        updateLocalStatsText();
    });
    document.getElementById('btn-reset-counts').addEventListener('click', resetPlayCounts);

    // player sources
    document.getElementById('source-tabs').addEventListener('click', (event) => {
        const tab = event.target.closest('.source-tab');
        if (tab) selectSource(tab.dataset.source);
    });
    document.getElementById('search-type-tabs').addEventListener('click', (event) => {
        const tab = event.target.closest('.search-type-tab');
        if (tab) switchSearchType(tab.dataset.type);
    });
    document.getElementById('btn-search').addEventListener('click', () => doSearch(false));
    document.getElementById('search-input').addEventListener('keydown', (event) => {
        if (event.key === 'Enter') doSearch(false);
    });
    document.getElementById('btn-list-more').addEventListener('click', () => {
        if (player.sourceTab === 'local') loadLocal(true);
        else if (player.sourceTab === 'new_songs') loadNewSongs(true);
        else if (player.sourceTab === 'recommend_mv') loadRecommendMVs(true);
        else if (player.sourceTab === 'toplist') { /* top lists don't paginate */ }
        else doSearch(true);
    });
    document.getElementById('btn-list-back').addEventListener('click', backToPlaylists);
    document.getElementById('btn-add-folder').addEventListener('click', addLocalFolder);
    document.getElementById('btn-rescan').addEventListener('click', rescanLocal);
    document.getElementById('btn-save-lyrics').addEventListener('click', () => {
        if (player.current) loadLyrics(player.current, true);
    });
    document.getElementById('btn-create-local-playlist').addEventListener('click', createLocalPlaylist);
    document.getElementById('btn-create-playlist').addEventListener('click', createPlaylist);

    // player bar
    document.getElementById('pb-play').addEventListener('click', togglePlay);
    document.getElementById('pb-next').addEventListener('click', playNext);
    document.getElementById('pb-prev').addEventListener('click', playPrevious);
    document.getElementById('pb-mode').addEventListener('click', cycleMode);
    document.getElementById('pb-fav').addEventListener('click', () => {
        if (player.current) toggleFavorite(player.current);
    });
    document.getElementById('pb-download').addEventListener('click', () => {
        if (player.current) downloadTrack(player.current);
    });
    document.getElementById('pb-queue').addEventListener('click', toggleQueueDrawer);
    document.getElementById('pb-desktop-lyrics').addEventListener('click', toggleDesktopLyrics);
    document.getElementById('pb-sleep-timer').addEventListener('click', showSleepTimerPopover);
    document.getElementById('pb-mute').addEventListener('click', toggleMute);
    document.getElementById('pb-volume').addEventListener('input', (event) => {
        setVolume(event.target.value, true);
    });
    document.getElementById('pb-track').addEventListener('click', (event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        seekToFraction((event.clientX - rect.left) / rect.width);
    });
    setVolume(player.volume, false);

    // queue drawer
    document.getElementById('btn-queue-close').addEventListener('click', closeQueueDrawer);
    document.getElementById('btn-queue-clear').addEventListener('click', clearQueue);
    document.getElementById('btn-queue-save').addEventListener('click', saveQueueAsPlaylist);
    document.getElementById('queue-drawer-backdrop').addEventListener('click', closeQueueDrawer);

    // desktop lyrics button in lyrics panel
    document.getElementById('btn-desktop-lyrics').addEventListener('click', toggleDesktopLyrics);

    // crossfade slider
    document.getElementById('crossfade-duration').addEventListener('input', (event) => {
        updateCrossfadeLabel();
        const seconds = parseFloat(event.target.value);
        player.crossfadeDuration = seconds;
        if (state.api && state.api.set_crossfade_duration) {
            state.api.set_crossfade_duration(seconds);
        }
    });

    // desktop lyrics settings
    document.getElementById('desktop-lyrics-font-size').addEventListener('input', (event) => {
        const val = parseInt(event.target.value, 10);
        if (player.desktopLyricsWindow && !player.desktopLyricsWindow.closed) {
            player.desktopLyricsWindow.postMessage({ type: 'ncm:desktop-lyrics', payload: { action: 'settings', settings: { fontSize: val } } }, '*');
        }
    });
    document.getElementById('desktop-lyrics-color').addEventListener('input', (event) => {
        const val = event.target.value;
        if (player.desktopLyricsWindow && !player.desktopLyricsWindow.closed) {
            player.desktopLyricsWindow.postMessage({ type: 'ncm:desktop-lyrics', payload: { action: 'settings', settings: { color: val } } }, '*');
        }
    });
    document.getElementById('desktop-lyrics-opacity').addEventListener('input', (event) => {
        updateDesktopLyricsOpacityLabel();
        const val = parseFloat(event.target.value);
        if (player.desktopLyricsWindow && !player.desktopLyricsWindow.closed) {
            player.desktopLyricsWindow.postMessage({ type: 'ncm:desktop-lyrics', payload: { action: 'settings', settings: { opacity: val } } }, '*');
        }
    });
    document.getElementById('desktop-lyrics-translation-check').addEventListener('change', (event) => {
        const val = event.target.checked;
        if (player.desktopLyricsWindow && !player.desktopLyricsWindow.closed) {
            player.desktopLyricsWindow.postMessage({ type: 'ncm:desktop-lyrics', payload: { action: 'settings', settings: { showTranslation: val } } }, '*');
        }
    });

    document.getElementById('lyrics-offset').addEventListener('input', (event) => {
        const val = parseInt(event.target.value, 10);
        player.lyricOffset = val;
        updateLyricsOffsetLabel();
        // Apply offset to current lyrics rendering
        if (player.current) {
            loadLyrics(player.current, false);
        }
    });

    // lyrics controls: dual-line, karaoke, edit
    document.getElementById('btn-lyrics-dual').addEventListener('click', toggleDualLineLyrics);
    document.getElementById('btn-lyrics-karaoke').addEventListener('click', toggleKaraokeLyrics);
    document.getElementById('btn-edit-lyrics').addEventListener('click', openLyricsEditor);
    document.getElementById('btn-lyrics-editor-cancel').addEventListener('click', closeLyricsEditor);
    document.getElementById('btn-lyrics-editor-apply').addEventListener('click', applyLyricsEditorChanges);
    document.getElementById('btn-lyrics-editor-save').addEventListener('click', saveLyricsFromEditor);
    document.getElementById('btn-lyrics-editor-play').addEventListener('click', () => playAudio());
    document.getElementById('btn-lyrics-editor-pause').addEventListener('click', () => audio.pause());
    document.getElementById('lyrics-editor-seek').addEventListener('input', (event) => {
        if (audio.duration) {
            audio.currentTime = (event.target.value / 100) * audio.duration;
        }
    });

    // sleep timer select
    document.getElementById('sleep-timer-select').addEventListener('change', (event) => {
        const minutes = parseInt(event.target.value, 10);
        if (minutes > 0) {
            startSleepTimer(minutes, 'pause');
        } else {
            clearSleepTimer();
        }
    });

    // lyrics: pause auto-scroll while the user is reading somewhere else
    const lyricsBox = document.getElementById('lyrics-scroll');
    ['wheel', 'touchstart', 'mousedown'].forEach((name) => {
        lyricsBox.addEventListener(name, () => {
            player.autoScroll = false;
            player.lastScrollAt = Date.now();
        });
    });
    lyricsBox.addEventListener('scroll', () => {
        if (!player.autoScroll && Date.now() - player.lastScrollAt > 4000) {
            player.autoScroll = true;
        }
    });

    // modals
    document.getElementById('login-tabs').addEventListener('click', (event) => {
        const tab = event.target.closest('.tab');
        if (tab) switchLoginTab(tab.dataset.tab);
    });
    document.getElementById('settings-tabs').addEventListener('click', (event) => {
        const tab = event.target.closest('.tab');
        if (!tab) return;
        document.querySelectorAll('#settings-tabs .tab').forEach((el) =>
            el.classList.toggle('is-active', el === tab));
        document.querySelectorAll('#settings-modal .tab-panel').forEach((el) =>
            el.classList.toggle('is-active', el.dataset.panel === tab.dataset.tab));
    });

    document.querySelectorAll('[data-close]').forEach((el) => {
        el.addEventListener('click', () => {
            const target = el.dataset.close;
            if (target === 'confirm-modal') resolveConfirm(false);
            else closeModal(target);
        });
    });
    document.getElementById('btn-confirm-ok').addEventListener('click', () => resolveConfirm(true));
    document.getElementById('btn-confirm-cancel').addEventListener('click', () => resolveConfirm(false));

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            const open = document.querySelector('.modal:not([hidden])');
            if (!open) return;
            if (open.id === 'confirm-modal') resolveConfirm(false);
            else closeModal(open.id);
            return;
        }
        const tag = (event.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
        if (event.key === ' ') {
            event.preventDefault();
            togglePlay();
        } else if (event.key === 'ArrowRight' && event.altKey) {
            seekBy(5);
        } else if (event.key === 'ArrowLeft' && event.altKey) {
            seekBy(-5);
        }
    });

    // audio element events
    audio.addEventListener('play', onAudioPlay);
    audio.addEventListener('pause', onAudioPause);
    audio.addEventListener('ended', onAudioEnded);
    audio.addEventListener('timeupdate', () => {
        updateProgressUi();
        updateLyricHighlight();
    });
    audio.addEventListener('loadedmetadata', onAudioLoadedMetadata);
    audio.addEventListener('durationchange', updateProgressUi);
    audio.addEventListener('error', onAudioError);
}

function onAudioPlay() {
    updatePlayButton();
    if (player.pendingRecord) {
        player.pendingRecord = false;
        const track = player.current;
        if (track) {
            state.api.record_play(track).then((result) => {
                if (result && result.success) {
                    player.counts[result.key] = result.count;
                    refreshTrackRow(result.key);
                    renderNowPlaying();
                }
            });
        }
    }
    saveSession(true);
}

function onAudioPause() {
    updatePlayButton();
    saveSession(true);
}

function onAudioEnded() {
    if (player.mode === 'single') {
        audio.currentTime = 0;
        playAudio();
        return;
    }
    advance(true);
}

function onAudioLoadedMetadata() {
    if (player.pendingPosition > 0) {
        const limit = isFinite(audio.duration) ? Math.max(0, audio.duration - 1) : player.pendingPosition;
        try {
            audio.currentTime = Math.min(player.pendingPosition, limit);
        } catch (err) {
            console.info('cannot seek:', err && err.message);
        }
    }
    player.pendingPosition = 0;
    updateProgressUi();
    saveSession(true);
}

function onAudioError() {
    const track = player.current;
    if (!track || !audio.src) return;
    showToast('error', t('player.playback_failed', { name: track.name || '' }), 4200);
    updatePlayButton();
}

/* ------------------------------------------- backend -> frontend callbacks */

function onStatusUpdate(payload) {
    if (payload && payload.status) updateStatus(payload.status, {});
}

function onDownloadProgress(task) {
    updateSongRow(task);
    upsertQueueItem(task);
}

function onDownloadStats(stats) {
    state.completed = stats.finished || 0;
    state.total = stats.total || 0;
    const percent = state.total ? (state.completed / state.total * 100) : 0;
    document.getElementById('progress-fill').style.width = `${percent}%`;
    document.getElementById('progress-text').textContent =
        `${percent.toFixed(0)}% (${state.completed}/${state.total})`;
    document.getElementById('overall-speed').textContent = fmtSpeed(stats.speed);
    updateStatus('downloading', { current: state.completed, total: state.total });
}

function onDownloadComplete(payload) {
    state.downloading = false;
    state.paused = false;
    updateDownloadControls();
    const stats = (payload && payload.stats) || {};
    showToast(stats.failed ? 'warning' : 'success', t('status.download_complete', {
        success: stats.success || 0,
        fail: stats.failed || 0,
        skipped: stats.skipped || 0,
    }), 6000);
    if (payload && payload.hint_login) {
        showToast('info', t('login.login_needed'), 6000);
    }
    if (stats.lyrics_saved) {
        showToast('info', t('status.lyrics_saved', { count: stats.lyrics_saved }), 6000);
    }
    if (stats.lyrics_missing) {
        showToast('info', t('status.lyrics_none', { count: stats.lyrics_missing }), 6000);
    }
    updateStatus('ready');
    document.querySelectorAll('.pill[data-status="waiting"]').forEach((pill) => {
        pill.dataset.status = 'waiting';
        pill.textContent = t('download.status_waiting');
    });
}

function onDownloadFailed(payload) {
    state.downloading = false;
    updateDownloadControls();
    showToast('error', (payload && payload.message) || t('common.error'));
}

function onLoginStatusChange(payload) {
    const statusEl = document.getElementById('qrcode-status');
    if (!statusEl || !payload) return;
    if (payload.status === 'pending') {
        statusEl.textContent = t('login.qrcode_waiting');
    } else if (payload.status === 'scanned') {
        statusEl.textContent = t('login.qrcode_scanned');
    } else if (payload.status === 'expired') {
        statusEl.textContent = t('login.qrcode_expired');
    } else if (payload.status === 'failed') {
        statusEl.textContent = t('login.login_failed');
    }
    // Show the server's own words when the answer is not one of the codes we
    // know: an unexpected answer used to leave the previous text on screen,
    // which looked exactly like "stuck at scanned".
    const known = [800, 801, 802, 803];
    if (payload.message && !known.includes(payload.code)) {
        statusEl.textContent = `${statusEl.textContent} · ${payload.code}: ${payload.message}`;
    }
}

function onLoginMessage(payload) {
    const statusEl = document.getElementById('qrcode-status');
    if (statusEl && payload && payload.message) {
        statusEl.textContent = `${t('login.qrcode_waiting')} · ${payload.message}`;
    }
}

function onQrcodeUpdate(payload) {
    const image = document.getElementById('qrcode-image');
    const statusEl = document.getElementById('qrcode-status');
    if (image && payload && payload.image) image.src = payload.image;
    if (statusEl) statusEl.textContent = t('login.qrcode_waiting');
}

function onLoginSuccess() {
    showToast('success', t('login.login_success'));
    closeModal('login-modal');
    refreshLoginStatus();
    if (player.sourceTab === 'playlists') loadPlaylists();
}

function onLoginFailed(payload) {
    const message = payload && payload.error_key ? t(payload.error_key) : t('login.login_failed');
    showToast('error', message);
}

function onLanguageChanged(translations) {
    if (translations) state.translations = translations;
    applyTranslations();
}
