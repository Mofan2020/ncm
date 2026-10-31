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
    registerTimer: null,
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
    // Seconds the lyrics panel waits before it returns to the playing
    // line after the user scrolled it away.
    lyricsScrollIdleSeconds: 4,
    pendingPosition: 0,
    pendingRecord: false,
    recordedKey: '',
    lastSaveAt: 0,
    playlists: [],
    playlistOpen: null,
    // The artists the account follows, kept in sync with the server so the
    // follow button and the list cannot disagree.
    followedArtists: [],
    followedOffset: 0,
    followedTotal: 0,
    // The artist whose page is open, so a refresh re-reads *that* page
    // instead of the list it was opened from.
    artistOpen: null,
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
    // Cover URL cache: remote cover URLs -> local media-server URLs.
    // The map is filled lazily as rows render; values are plain strings,
    // so it survives nothing across reloads (which is fine, the cache on
    // disk is what we actually save).
    coverUrlCache: new Map(),
    coverUrlPending: new Set(),
    // The media server's loopback base URL.  It lets the webview build the
    // cover URLs on its own instead of asking the backend for each one.
    mediaBaseUrl: '',
};

const MODES = ['single', 'list', 'shuffle'];
const MODE_ICONS = { single: 'repeat-one', list: 'repeat', shuffle: 'shuffle' };
/** Circular accent ring colours sampled from the wallpaper (see sampleAccent). */
let accentCanvas = null;

/* --------------------------------------------------------------- bootstrap */

document.addEventListener('DOMContentLoaded', boot);

async function boot() {
    bindStaticEvents();
    renderStaticIcons();
    state.api = await waitForApi();

    if (!state.api) {
        showToast('error', 'pywebview bridge unavailable');
        return;
    }

    try {
        await loadTranslations();
        // The disclaimer comes first: it is the first thing a new user sees.
        await showDisclaimerIfNeeded();
        state.appInfo = await state.api.get_app_info();
    if (state.appInfo && state.appInfo.media_base_url) {
        state.mediaBaseUrl = state.appInfo.media_base_url;
    }
        // Fire and forget: a slow or offline GitHub must not delay the boot.
        checkForUpdates(false);
        await loadSettings();
        const loginStatus = await state.api.get_login_status();
        state.loggedIn = !!(loginStatus && loginStatus.is_logged_in);
        await refreshLoginStatus();
        showHome();
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
        initMobileLayout();      // responsive shell (drawer / npm view)
        // Pick a starting source that always shows data on first paint:
        // logged-in users start in their own playlists; anonymous users get
        // the local music tab so the screen is never blank.
        if (!player.sourceTab || player.sourceTab === 'playlists' && !state.loggedIn) {
            player.sourceTab = state.loggedIn ? 'playlists' : 'local';
        }
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
    safeRender('labelHomeCards', labelHomeCards);
}

/**
 * Give every home card a spoken label.
 *
 * The card shows an icon, a title and a sentence; the screen reader needs
 * them as one string, otherwise it only reads the icon.
 */
function labelHomeCards() {
    document.querySelectorAll('.home-card').forEach((card) => {
        const title = card.querySelector('.home-card-title');
        const desc = card.querySelector('.home-card-desc');
        const parts = [];
        if (title) parts.push(title.textContent);
        if (desc) parts.push(desc.textContent);
        if (parts.length) card.setAttribute('aria-label', parts.join('，'));
    });
}

/**
 * Draw the icons declared in the markup (``<button data-icon="play">``).
 *
 * The markup carries the icon *name* instead of a glyph, so the icon set
 * lives in one place (icons.js) and the buttons stay free of emoji.
 */
function renderStaticIcons(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-icon]').forEach((el) => {
        setIcon(el, el.dataset.icon);
    });
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
    // The new sections build their text in JS, so they have to be drawn
    // again when the language changes.
    safeRender('renderCombinedRows', renderCombinedRows);
    safeRender('renderBiliResults', renderBiliResults);
    safeRender('updateFilterChips', updateFilterChips);
    // The idle delay is written in seconds, so the label follows the
    // language like the rest of the settings.
    updateLyricsScrollIdleLabel();
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

//: How long a plain toast stays up.  The old 3.2 s was gone before the eye
//: had settled on it.
const TOAST_MS = 4800;
//: A toast carrying an undo button is a control, not a notification: the user
//: has to notice it, read it and reach it before it leaves.
const TOAST_UNDO_MS = 8000;
//: Confirmation after an undo.  Nothing to act on, so it is short.
const TOAST_UNDONE_MS = 2400;

function showToast(type, message, timeoutMs = TOAST_MS, options = null) {
    if (!message) return;
    const container = document.getElementById('toast-container');
    if (!container) return;
    // Only a toast that can actually be undone gets a button: a label without
    // a callback would be a dead control.
    const undo = options && options.undoLabel && typeof options.onUndo === 'function'
        ? options : null;
    // Cap the visible queue: spam (e.g. dozens of download failures in a row)
    // would otherwise leak DOM and listeners indefinitely.  The oldest fades
    // out as soon as the cap is exceeded.
    const MAX_TOASTS = 5;
    while (container.children.length >= MAX_TOASTS) {
        const oldest = container.firstElementChild;
        if (!oldest) break;
        oldest.classList.add('toast-dismiss');
        oldest.remove();
    }
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    // The message lives in its own element: putting the undo button next to it
    // means the toast body cannot be a bare text node any more.
    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message;
    toast.appendChild(text);
    let timer = null;
    const dismiss = () => {
        if (timer) {
            clearTimeout(timer);
            timer = null;
        }
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(6px)';
        setTimeout(() => toast.remove(), 220);
    };
    if (undo) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'toast-undo';
        button.textContent = undo.undoLabel;
        // One shot: the undo sends a write request, and a second click on a
        // toast that is already fading must not send it twice.
        button.addEventListener('click', () => {
            if (button.disabled) return;
            button.disabled = true;
            dismiss();
            try {
                Promise.resolve(undo.onUndo()).catch(() => {});
            } catch (err) {
                /* the undo path reports its own failure */
            }
        });
        toast.appendChild(button);
    }
    container.appendChild(toast);
    timer = setTimeout(dismiss, undo ? Math.max(timeoutMs, TOAST_UNDO_MS) : timeoutMs);
}

/**
 * Undo one write request and say so.
 *
 * ``apply`` only runs when the request came back successful, so a failed undo
 * never leaves the screen showing the old state.
 */
async function runUndo(request, apply, failKey) {
    const key = failKey || 'error.unknown';
    let result;
    try {
        result = await request();
    } catch (err) {
        showToast('error', t(key));
        return;
    }
    if (!result || result.success === false) {
        if (result) showError(result, key);
        else showToast('error', t(key));
        return;
    }
    if (apply) await apply(result);
    showToast('info', t('common.undone'), TOAST_UNDONE_MS);
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

/* ------------------------------------------------------------------ cache */

/** Human-readable byte size: `1023 B`, `12.3 MB`, `1.20 GB`. */
function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let size = value / 1024;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
        size /= 1024;
        unit += 1;
    }
    return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[unit]}`;
}

/** Read the media cache footprint into the settings panel. */
async function refreshCacheStats() {
    if (!state.api || !state.api.get_cache_stats) return;
    let result;
    try {
        result = await state.api.get_cache_stats();
    } catch (err) {
        return;
    }
    if (!result || !result.success) return;
    const audio = (result.audio && result.audio.bytes) || 0;
    const cover = (result.cover && result.cover.bytes) || 0;
    const meta = result.meta_bytes || 0;
    const set = (id, text) => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    };
    set('cache-audio', formatBytes(audio));
    set('cache-cover', formatBytes(cover));
    set('cache-meta', formatBytes(meta));
    set('cache-total', formatBytes(audio + cover + meta));
}

/**
 * The escape hatch: wipe every cache and restart the app.
 *
 * Caches make the app fast, but a stale one can make it unusable (a song
 * that no longer resolves, a playlist that will not open).  This clears
 * the metadata cache, the audio and cover caches and the in-memory
 * response caches, then relaunches.
 */
async function forceRefresh() {
    if (!await confirmDialog('settings.cache_force_refresh_confirm')) return;
    if (!state.api || !state.api.force_refresh) {
        showToast('error', t('error.unknown'));
        return;
    }
    let result;
    try {
        result = await state.api.force_refresh();
    } catch (err) {
        result = null;
    }
    if (!result || !result.success) {
        showToast('error', t('error.unknown'));
        return;
    }
    // The backend relaunches the app; this window goes away with it.
}

async function clearCache(what) {
    if (!await confirmDialog('cache.confirm')) return;
    let result;
    try {
        result = await state.api.clear_cache(what);
    } catch (err) {
        result = null;
    }
    if (!result || !result.success) {
        showToast('error', t('cache.clear_failed'));
        return;
    }
    showToast('success', t('cache.cleared', { count: result.removed || 0 }));
    refreshCacheStats();
}

/* ---------------------------------------------------------------- account */

/**
 * Show the signed-in account: who it is, and what the server reports.
 *
 * The panel is read-only.  NetEase's ``/api/user/profile/update`` route exists
 * but answers ``code 250 "用户资料初始化或修改失败"`` for third-party clients,
 * so offering an editable nickname here would be a button that always fails.
 */
async function openAccountPanel(refresh) {
    if (!state.api || !state.api.get_user_profile) return;
    const grid = document.getElementById('account-grid');
    if (grid) grid.innerHTML = `<p class="empty">${t('account.loading')}</p>`;
    openModal('account-modal');

    let result;
    try {
        result = await state.api.get_user_profile(Boolean(refresh));
    } catch (err) {
        result = null;
    }
    if (!result || !result.success || !result.profile) {
        if (grid) grid.innerHTML = `<p class="empty">${t('account.refresh_failed')}</p>`;
        return;
    }
    renderAccountProfile(result.profile);
}

function renderAccountProfile(profile) {
    const avatar = document.getElementById('account-avatar');
    if (avatar) {
        avatar.src = profile.avatarUrl || profile.avatarImgUrl || '';
        avatar.hidden = !avatar.src;
    }
    const name = document.getElementById('account-name');
    if (name) name.textContent = profile.nickname || t('account.not_logged_in');
    const signature = document.getElementById('account-signature');
    if (signature) {
        signature.textContent = profile.signature || t('account.signature_empty');
    }

    const grid = document.getElementById('account-grid');
    if (!grid) return;
    grid.innerHTML = '';
    // ``province`` / ``city`` are GB/T 2260 numeric codes (``310000``), not
    // names -- showing them raw is noise, and a partial code table would be
    // worse than omitting the row.
    const rows = [
        ['account.user_id', profile.userId],
        ['account.level', profile.level != null ? String(profile.level) : ''],
        ['account.vip', profile.vipType > 0 ? `VIP ${profile.vipType}` : t('account.vip_none')],
        ['account.follows', profile.followedUserCount],
        ['account.fans', profile.followeds],
        ['account.playlists', profile.playlistCount],
        ['account.listen_songs', profile.listenSongs],
        ['account.joined', formatAccountDate(profile.createTime)],
    ];
    rows.forEach(([key, value]) => {
        if (value === undefined || value === null || value === '') return;
        const term = document.createElement('dt');
        term.textContent = t(key);
        const detail = document.createElement('dd');
        detail.textContent = String(value);
        grid.appendChild(term);
        grid.appendChild(detail);
    });
}

/** ``createTime`` is epoch milliseconds. */
function formatAccountDate(value) {
    const number = Number(value);
    if (!number) return '';
    const date = new Date(number);
    if (Number.isNaN(date.getTime())) return '';
    const pad = (part) => String(part).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/* ----------------------------------------------------------- update check */

/**
 * Look for a newer release and, if there is one, show a corner notice.
 *
 * Advisory only: no dialog, no download, no install.  The check is quiet on
 * every failure (offline, rate limited, API changed) because the user did not
 * ask for it.
 */
async function checkForUpdates(force) {
    if (!state.api || !state.api.check_for_update) return;
    let result;
    try {
        result = await state.api.check_for_update(Boolean(force));
    } catch (err) {
        return;
    }
    if (!result || !result.available) return;

    const notice = document.getElementById('update-notice');
    if (!notice) return;
    const title = document.getElementById('update-notice-title');
    const hint = document.getElementById('update-notice-hint');
    const open = document.getElementById('btn-update-open');
    if (title) {
        title.textContent = t('update.available').replace('{version}', result.latest || '');
    }
    if (hint) {
        hint.textContent = t('update.hint').replace('{current}', result.current || '');
    }
    if (open) {
        open.onclick = () => {
            if (result.url) window.open(result.url, '_blank');
            dismissUpdateNotice();
        };
    }
    notice.hidden = false;
    state.updateInfo = result;
}

function dismissUpdateNotice() {
    const notice = document.getElementById('update-notice');
    if (notice) notice.hidden = true;
}

/* ------------------------------------------------------------- disclaimer */

/** Close the app -- used by the disclaimer's "quit" button. */
function quitApp() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.quit_app) {
        window.pywebview.api.quit_app().catch(() => { /* ignore */ });
        return;
    }
    window.close();
}


/**
 * Show the first-start disclaimer, if it has not been accepted yet.
 *
 * The dialog has no close button and no backdrop dismiss: the user has to
 * tick the box and press "Agree", or quit the app.
 */
async function showDisclaimerIfNeeded() {
    if (!state.api || !state.api.get_disclaimer_status) return;
    let status;
    try {
        status = await state.api.get_disclaimer_status();
    } catch (err) {
        return;
    }
    if (!status || !status.required) return;

    const body = document.getElementById('disclaimer-body');
    if (body) {
        // The translations are flat: the paragraph list arrives as
        // `disclaimer.body.1`, `disclaimer.body.2`, ... (see
        // src/i18n/__init__.py::flatten_translations).
        const paragraphs = Object.keys(state.translations)
            .filter((key) => key.startsWith('disclaimer.body.'))
            .sort((a, b) => Number(a.split('.').pop()) - Number(b.split('.').pop()))
            .map((key) => state.translations[key]);
        body.innerHTML = '';
        paragraphs.forEach((line) => {
            const paragraph = document.createElement('p');
            paragraph.textContent = line;
            body.appendChild(paragraph);
        });
    }
    const agree = document.getElementById('disclaimer-agree');
    const accept = document.getElementById('btn-disclaimer-accept');
    if (agree) agree.checked = false;
    if (accept) accept.disabled = true;
    openModal('disclaimer-modal');
}

function setDisclaimerAgree(checked) {
    const accept = document.getElementById('btn-disclaimer-accept');
    if (accept) accept.disabled = !checked;
}

async function acceptDisclaimer() {
    const agree = document.getElementById('disclaimer-agree');
    if (!agree || !agree.checked) {
        showToast('warning', t('disclaimer.must_agree'));
        return;
    }
    if (state.api && state.api.accept_disclaimer) {
        await state.api.accept_disclaimer().catch(() => { /* ignore */ });
    }
    closeModal('disclaimer-modal');
}

/* ---------------------------------------------------------------- settings */

async function loadSettings() {
    state.settings = await state.api.get_settings();
    player.mode = state.settings.playback.play_mode || 'list';
    player.volume = state.settings.playback.volume;
    player.crossfadeDuration = state.settings.playback.crossfade_duration || 0;
    player.lyricsScrollIdleSeconds = state.settings.playback.lyrics_scroll_idle_seconds || 4;
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
    setValue('lyrics-scroll-idle', String(s.playback.lyrics_scroll_idle_seconds || 4));
    // The slider works right away, so the runtime has to follow the form
    // both ways: an unsaved change is dropped when the form is filled in
    // again.
    player.lyricsScrollIdleSeconds = s.playback.lyrics_scroll_idle_seconds || 4;
    updateLyricsScrollIdleLabel();
    setValue('exclusive-mode-select', s.playback.exclusive_mode || 'off');
    setValue('sleep-timer-select', String(s.playback.sleep_timer_minutes || 0));

    // desktop lyrics
    setValue('desktop-lyrics-font-size', s.ui.desktop_lyrics_font_size || 18);
    setValue('desktop-lyrics-color', s.ui.desktop_lyrics_color || '#ffffff');
    setValue('desktop-lyrics-opacity', s.ui.desktop_lyrics_opacity || 0.9);
    updateDesktopLyricsOpacityLabel();
    setChecked('desktop-lyrics-translation-check', s.ui.desktop_lyrics_show_translation !== false);
    setValue('desktop-lyrics-background', s.ui.desktop_lyrics_background || 'dark');
    setValue('desktop-lyrics-text-style', s.ui.desktop_lyrics_text_style || 'white');

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
        setIcon(remove, 'close');
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
const THEME_ICONS = { light: 'sun', dark: 'moon', system: 'monitor' };

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
    setIcon(button, THEME_ICONS[state.themeMode] || THEME_ICONS.system);
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
            lyrics_scroll_idle_seconds: parseInt(document.getElementById('lyrics-scroll-idle').value, 10),
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
            desktop_lyrics_background: document.getElementById('desktop-lyrics-background').value,
            desktop_lyrics_text_style: document.getElementById('desktop-lyrics-text-style').value,
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
    player.lyricsScrollIdleSeconds = playback.lyrics_scroll_idle_seconds || 4;
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

/* ------------------------------------------------------------ account backup */

/** Pending import plan: the conflicts the user still has to decide about. */
const backup = {
    conflicts: [],
    summary: null,
};

/**
 * How many local-only records a backup carries.
 *
 * Play counts, the local favourites and the recently played list only ever
 * live on this machine, so they are counted together as "local records".
 * The export result and the import result spell the three apart; this
 * sums them for the one-line summary.
 */
function localRecordCount(source) {
    const data = source || {};
    if (data.local_restored) {
        const restored = data.local_restored;
        return (restored.play_counts || 0) + (restored.favorites || 0)
            + (restored.recent || 0);
    }
    return (data.local_play_counts || 0) + (data.local_favorites || 0)
        + (data.local_recent || 0);
}

/** Human name of a backup progress phase. */
function backupPhaseText(phase) {
    const key = 'backup.phase_' + phase;
    const text = t(key);
    return text === key ? phase : text;
}

/** Where a section of the backup came from. */
function backupSourceText(source) {
    const key = 'backup.source_' + (source || 'missing');
    const text = t(key);
    return text === key ? (source || '') : text;
}

function setBackupStatus(text) {
    const el = document.getElementById('backup-status');
    if (el) el.textContent = text || '';
}

function setBackupProgress(current, total) {
    const track = document.getElementById('backup-progress-track');
    const fill = document.getElementById('backup-progress-fill');
    if (!track || !fill) return;
    if (!total) {
        track.hidden = true;
        fill.style.width = '0%';
        return;
    }
    track.hidden = false;
    fill.style.width = `${Math.min(100, Math.round((current / total) * 100))}%`;
}

/**
 * Export the account data into a .ncmbk file.
 *
 * The backend pulls everything from the cloud first and falls back to the
 * local cache for whatever it cannot read, so the file always says where
 * each part came from.
 */
async function exportAccountBackup() {
    if (!state.api || !state.api.export_account_backup) return;
    const button = document.getElementById('btn-backup-export');
    if (button) button.disabled = true;
    setBackupStatus(t('backup.exporting'));
    setBackupProgress(0, 1);
    try {
        const result = await state.api.export_account_backup();
        if (!result) return;
        if (result.cancelled) {
            setBackupStatus('');
            setBackupProgress(0, 0);
            return;
        }
        if (!result.success) {
            showError(result, 'backup.export_failed');
            setBackupStatus(t('backup.export_failed'));
            setBackupProgress(0, 0);
            return;
        }
        const sources = result.sources || {};
        const parts = ['account', 'favorites', 'playlists', 'artists', 'local']
            .map((key) => `${backupSourceText(sources[key])}`)
            .join(' · ');
        setBackupStatus(t('backup.export_done', {
            path: result.path || '',
            playlists: result.playlists || 0,
            songs: result.songs || 0,
            favorites: result.favorites || 0,
            artists: result.artists || 0,
            albums: result.albums || 0,
            djradios: result.djradios || 0,
            mvs: result.mvs || 0,
            local: localRecordCount(result),
            sources: parts,
        }));
        setBackupProgress(0, 0);
        showToast('success', t('backup.export_done_short', {
            playlists: result.playlists || 0,
            songs: result.songs || 0,
        }), 6000);
    } catch (err) {
        console.error(err);
        showToast('error', t('backup.export_failed'));
        setBackupStatus(t('backup.export_failed'));
        setBackupProgress(0, 0);
    } finally {
        if (button) button.disabled = false;
    }
}

/**
 * Pick a backup file and work out what the import would do.
 *
 * Nothing is written yet: the conflicts go to the dialog first, and the
 * backend only touches the account after the user has decided.
 */
async function importAccountBackup() {
    if (!state.api || !state.api.import_account_backup) return;
    const button = document.getElementById('btn-backup-import');
    if (button) button.disabled = true;
    try {
        const chosen = await state.api.choose_backup_file();
        if (!chosen) return;
        if (!chosen.success) {
            if (!chosen.cancelled) showError(chosen, 'backup.import_failed');
            return;
        }
        setBackupStatus(t('backup.importing'));
        const plan = await state.api.import_account_backup(chosen.path);
        if (!plan) return;
        if (!plan.success) {
            showError(plan, 'backup.import_failed');
            setBackupStatus(t('backup.import_failed'));
            return;
        }
        backup.conflicts = plan.conflicts || [];
        backup.summary = plan.summary || {};
        renderBackupConflicts(plan);
        openModal('backup-conflict-modal');
    } catch (err) {
        console.error(err);
        showToast('error', t('backup.import_failed'));
        setBackupStatus(t('backup.import_failed'));
    } finally {
        if (button) button.disabled = false;
    }
}

/** Draw the conflict list: one row per playlist the user has to decide about. */
function renderBackupConflicts(plan) {
    const list = document.getElementById('backup-conflict-list');
    const summary = document.getElementById('backup-conflict-summary');
    if (!list) return;
    list.innerHTML = '';
    const stats = plan.summary || {};
    if (summary) {
        summary.textContent = t('backup.conflict_summary', {
            favorites: stats.favorites_to_add || 0,
            playlists: stats.playlists_to_create || 0,
            conflicts: stats.conflicts || 0,
            skipped: stats.skipped || 0,
            artists: stats.artists_to_follow || 0,
            albums: stats.albums_to_add || 0,
            djradios: stats.djradios_to_add || 0,
            mvs: stats.mvs_to_add || 0,
            local: (stats.local_play_counts || 0) + (stats.local_favorites || 0)
                + (stats.local_recent || 0),
        });
    }
    (plan.skipped || []).forEach((item) => {
        const row = document.createElement('div');
        row.className = 'folder-row';
        const name = document.createElement('span');
        name.textContent = t('backup.skip_' + (item.reason || 'other'), {
            name: item.name || '',
        });
        row.appendChild(name);
        list.appendChild(row);
    });
    (plan.conflicts || []).forEach((item) => {
        const row = document.createElement('div');
        row.className = 'folder-row';
        const name = document.createElement('span');
        name.textContent = t('backup.conflict_row', {
            name: item.name || '',
            count: item.song_count || 0,
        });
        row.appendChild(name);

        const select = document.createElement('select');
        select.dataset.backupConflict = item.id;
        [['skip', 'backup.action_skip'], ['merge', 'backup.action_merge'],
         ['rename', 'backup.action_rename']].forEach(([value, key]) => {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = t(key);
            select.appendChild(option);
        });
        row.appendChild(select);

        const rename = document.createElement('input');
        rename.type = 'text';
        rename.placeholder = t('backup.rename_placeholder');
        rename.dataset.backupRename = item.id;
        rename.hidden = true;
        rename.value = `${item.name || ''} (2)`;
        select.addEventListener('change', () => {
            rename.hidden = select.value !== 'rename';
        });
        row.appendChild(rename);

        list.appendChild(row);
    });
}

/** Collect the user's decisions and hand them to the backend. */
async function applyBackupConflicts() {
    if (!state.api || !state.api.apply_account_backup) return;
    const decisions = { playlists: {} };
    document.querySelectorAll('#backup-conflict-list select[data-backup-conflict]')
        .forEach((select) => {
            const id = select.dataset.backupConflict;
            const action = select.value;
            const rename = document.querySelector(
                `#backup-conflict-list input[data-backup-rename="${CSS.escape(id)}"]`);
            const entry = { action };
            if (action === 'rename') entry.name = (rename && rename.value) || '';
            decisions.playlists[id] = entry;
        });
    closeModal('backup-conflict-modal');
    const button = document.getElementById('btn-backup-conflict-apply');
    if (button) button.disabled = true;
    setBackupStatus(t('backup.importing'));
    setBackupProgress(0, 1);
    try {
        const result = await state.api.apply_account_backup(decisions);
        if (!result) return;
        if (!result.success) {
            showError(result, 'backup.import_failed');
            setBackupStatus(t('backup.import_failed'));
            setBackupProgress(0, 0);
            return;
        }
        setBackupStatus(t('backup.import_done', {
            songs: result.songs_liked || 0,
            playlists: result.playlists_created || 0,
            merged: result.playlists_merged || 0,
            skipped: result.playlists_skipped || 0,
            artists: result.artists_followed || 0,
            albums: result.albums_liked || 0,
            djradios: result.djradios_subscribed || 0,
            mvs: result.mvs_liked || 0,
            local: localRecordCount(result.local_restored || {}),
            failed: result.failed || 0,
        }));
        setBackupProgress(0, 0);
        showToast('success', t('backup.import_done_short', {
            songs: result.songs_liked || 0,
            playlists: result.playlists_created || 0,
        }), 6000);
    } catch (err) {
        console.error(err);
        showToast('error', t('backup.import_failed'));
        setBackupStatus(t('backup.import_failed'));
        setBackupProgress(0, 0);
    } finally {
        if (button) button.disabled = false;
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
        button.onclick = () => openAccountPanel(false);
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
    // Leaving the register tab invalidates the existence check: the number
    // may have been typed for another account in the meantime.
    if (tab !== 'register') resetRegisterState();
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

/* ------------------------------------------------------------- register */

const registerState = {
    /** The number the existence check was run for ('' = not checked yet). */
    checkedPhone: '',
    /** True when that number has no account and may be registered. */
    canRegister: false,
    /** The number a verification code was requested for. */
    codePhone: '',
};

function resetRegisterState() {
    registerState.checkedPhone = '';
    registerState.canRegister = false;
    registerState.codePhone = '';
    const status = document.getElementById('register-status');
    if (status) status.textContent = '';
}

async function checkRegisterPhone() {
    const phone = document.getElementById('register-phone-input').value.trim();
    const status = document.getElementById('register-status');
    if (!/^1\d{10}$/.test(phone)) {
        showToast('warning', t('login.invalid_phone'));
        return;
    }
    if (status) status.textContent = t('login.register_checking');
    const result = await state.api.check_phone_registered(phone);
    registerState.checkedPhone = phone;
    registerState.canRegister = false;
    if (!result || !result.known) {
        if (status) status.textContent = t('login.register_check_failed');
        return;
    }
    if (result.registered) {
        if (status) status.textContent = t('login.register_already_registered');
        showToast('warning', t('login.register_already_registered'));
        return;
    }
    registerState.canRegister = true;
    if (status) status.textContent = t('login.register_not_registered');
}

async function sendRegisterCode() {
    const phone = document.getElementById('register-phone-input').value.trim();
    if (!/^1\d{10}$/.test(phone)) {
        showToast('warning', t('login.invalid_phone'));
        return;
    }
    const button = document.getElementById('btn-register-send-code');
    // Same rule as the login tab: every extra request extends NetEase's
    // cooldown for the number.
    if (button && button.disabled) return;
    if (button) button.disabled = true;
    const result = await state.api.send_phone_code(phone);
    if (result.success) {
        registerState.codePhone = phone;
        showToast('success', t('login.code_sent'));
        startRegisterCountdown();
    } else if (result.throttled || result.error_key === 'login.code_too_frequent') {
        showToast('error', t('login.code_too_frequent', { message: result.message || '' }), 9000);
        startRegisterCountdown(180);
    } else {
        showToast('error', t('login.code_send_failed', { message: result.message || '' }));
        startRegisterCountdown(60);
    }
}

function startRegisterCountdown(seconds = 60) {
    const button = document.getElementById('btn-register-send-code');
    if (!button) return;
    if (state.registerTimer) clearInterval(state.registerTimer);
    button.disabled = true;
    let remaining = seconds;
    const tick = () => {
        if (remaining <= 0) {
            clearInterval(state.registerTimer);
            state.registerTimer = null;
            button.disabled = false;
            button.textContent = t('login.send_code');
            return;
        }
        button.textContent = t('login.code_countdown', { seconds: remaining });
        remaining -= 1;
    };
    tick();
    state.registerTimer = setInterval(tick, 1000);
}

async function doRegister() {
    const phone = document.getElementById('register-phone-input').value.trim();
    const code = document.getElementById('register-code-input').value.trim();
    const password = document.getElementById('register-password-input').value;
    const nickname = document.getElementById('register-nickname-input').value.trim();
    const status = document.getElementById('register-status');

    if (!/^1\d{10}$/.test(phone)) {
        showToast('warning', t('login.invalid_phone'));
        return;
    }
    if (!code) {
        showToast('warning', t('login.enter_phone_and_code'));
        return;
    }
    if (!password) {
        showToast('warning', t('login.register_need_password'));
        return;
    }
    if (password.length < 6) {
        showToast('warning', t('login.register_password_too_short'));
        return;
    }
    if (!nickname) {
        showToast('warning', t('login.register_need_nickname'));
        return;
    }

    // The number may have changed since the check: re-verify before sending.
    if (registerState.checkedPhone !== phone || !registerState.canRegister) {
        await checkRegisterPhone();
        if (!registerState.canRegister) return;
    }

    const button = document.getElementById('btn-register-submit');
    if (button) button.disabled = true;
    const result = await state.api.register_phone(phone, code, password, nickname);
    if (button) button.disabled = false;

    if (result.success) {
        showToast('success', t('login.register_success'));
        resetRegisterState();
        closeModal('login-modal');
        await refreshLoginStatus();
        if (player.sourceTab === 'playlists') loadPlaylists();
        return;
    }
    const key = result.error_key || 'login.register_failed';
    const message = result.message ? t('login.register_failed', { message: result.message }) : t(key);
    showToast('error', message);
    if (status && result.error_key) status.textContent = t(key);
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
        setCoverImg(cover, playlist.cover_url);
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
    // Only the header tabs follow the view; the segmented control inside the
    // search page carries the class too and must keep its own state.
    document.querySelectorAll('#top-tabs .top-tab').forEach((tab) =>
        tab.classList.toggle('is-active', tab.dataset.view === name));
    document.querySelectorAll('.view').forEach((view) =>
        view.classList.toggle('is-active', view.id === 'view-' + name));
    // Always refresh the current source when the player view becomes active.
    // When the queue was non-empty (e.g. a restored session) we used to skip
    // this reload, so #playlist-list / #track-list stayed at whatever stale
    // content the previous render left behind and the user opened the player
    // tab to a blank screen.  selectSource itself is idempotent so calling
    // it again on the same source is safe.
    if (name === 'player') {
        selectSource(player.sourceTab);
    }
}

/* ------------------------------------------------------------- sources */

async function selectSource(source) {
    player.sourceTab = source;
    player.playlistOpen = null;
    player.currentLocalPlaylist = null;
    player.artistOpen = null;
    // Mirror the active state in both the old `.source-tab` row and the new
    // `.sidebar-item` list -- they share `data-source` but live in different
    // containers, so toggling one would leave the other stale.
    document.querySelectorAll('.source-tab, .sidebar-item').forEach((tab) =>
        tab.classList.toggle('is-active', tab.dataset.source === source));
    document.getElementById('local-actions').hidden = source !== 'local';
    document.getElementById('local-playlist-actions').hidden = source !== 'local_playlists';
    document.getElementById('btn-list-back').hidden = true;
    document.getElementById('btn-list-more').hidden = true;
    document.getElementById('search-type-tabs').hidden = true;
    // Switching sources must clear the previous track list.  Without this the
    // last tab's tracks stay on screen and bleed into whatever list opens next
    // (the symptom: a couple of playlists later, ~200 stale tracks reappear).
    const trackList = document.getElementById('track-list');
    if (trackList) trackList.innerHTML = '';
    player.visible = [];
    player.visibleSource = null;

    if (source === 'playlists') await loadPlaylists();
    else if (source === 'liked') await loadLiked();
    else if (source === 'followed_artists') await loadFollowedArtists();
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

/* ============================================================
   Responsive shell (mobile drawer + full-screen now-playing).
   The CSS owns the actual layout; this module only:
     - toggles `<body class="is-mobile">` so JS can branch cheaply
     - wires the sidebar hamburger + backdrop
     - mirrors the desktop renderers into the mobile now-playing panel
   Mirroring avoids a separate "mobile render" path; the npm elements
   just observe the same state as the desktop ones.
   ============================================================ */
const MOBILE_QUERY = window.matchMedia('(max-width: 720px)');
let mobileSidebarBackdrop = null;

function setMobile(isMobile) {
    document.body.classList.toggle('is-mobile', isMobile);
    if (!isMobile) {
        // Closing the drawer when the window resizes back to desktop
        // avoids a stuck drawer that the desktop layout can't see.
        document.body.removeAttribute('data-sidebar-open');
    }
}

function ensureSidebarBackdrop() {
    if (mobileSidebarBackdrop) return mobileSidebarBackdrop;
    const backdrop = document.createElement('div');
    backdrop.className = 'sidebar-backdrop';
    document.body.appendChild(backdrop);
    backdrop.addEventListener('click', () => closeMobileSidebar());
    mobileSidebarBackdrop = backdrop;
    return backdrop;
}

function openMobileSidebar() {
    if (!MOBILE_QUERY.matches) return;
    ensureSidebarBackdrop();
    document.body.setAttribute('data-sidebar-open', '1');
}

function closeMobileSidebar() {
    document.body.removeAttribute('data-sidebar-open');
}

function initMobileLayout() {
    // Backdrop only exists on mobile, but the body class lets JS cheaply
    // branch on layout -- e.g. `renderNowPlaying` only paints the npm
    // mirrors when `is-mobile` is active, saving the work on desktop.
    setMobile(MOBILE_QUERY.matches);
    if (typeof MOBILE_QUERY.addEventListener === 'function') {
        MOBILE_QUERY.addEventListener('change', (event) => setMobile(event.matches));
    } else if (typeof MOBILE_QUERY.addListener === 'function') {
        // Safari < 14 fallback -- safe to drop after pywebview 6 EOL.
        MOBILE_QUERY.addListener((event) => setMobile(event.matches));
    }

    // Sidebar hamburger -- the same button works in two layouts:
    //   mobile  -> drawer (this function)
    //   desktop -> toggle the .is-collapsed class (the existing
    //              CSS rule was already there but had no JS handler).
    const sidebarToggle = document.getElementById('btn-sidebar-toggle');
    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', () => {
            if (MOBILE_QUERY.matches) {
                if (document.body.getAttribute('data-sidebar-open') === '1') {
                    closeMobileSidebar();
                } else {
                    openMobileSidebar();
                }
            } else {
                document.body.classList.toggle('is-sidebar-collapsed');
            }
        });
    }

    // Sidebar items auto-close the drawer on mobile so the chosen source
    // becomes visible immediately.  The desktop layout does not close.
    document.querySelectorAll('#sidebar-nav .sidebar-item').forEach((item) => {
        item.addEventListener('click', () => {
            if (MOBILE_QUERY.matches) closeMobileSidebar();
        });
    });

    // The player-bar title and cover open the full-screen now-playing
    // view on mobile.  Desktop already shows everything inline.
    const pbTitle = document.getElementById('pb-title');
    const pbCover = document.getElementById('pb-cover');
    const openFromBar = () => {
        if (MOBILE_QUERY.matches) openNowPlayingMobile();
    };
    if (pbTitle) pbTitle.addEventListener('click', openFromBar);
    if (pbCover) pbCover.addEventListener('click', openFromBar);

    initNowPlayingMobile();
    initMobileLyricsRenderer();
}

/* ---------- full-screen now-playing (mobile only) ---------- */
function openNowPlayingMobile() {
    const view = document.getElementById('now-playing-mobile');
    if (!view) return;
    view.hidden = false;
    // Force a reflow before adding the class so the slide-up transition
    // runs from the initial state.
    void view.offsetWidth;
    view.classList.add('is-open');
    renderNowPlayingMobile();
    renderMobileLyrics();
}

function closeNowPlayingMobile() {
    const view = document.getElementById('now-playing-mobile');
    if (!view) return;
    view.classList.remove('is-open');
    // Wait for the transition before re-hiding so the user sees it
    // slide down instead of vanishing.
    setTimeout(() => {
        if (!view.classList.contains('is-open')) view.hidden = true;
    }, 320);
}

function initNowPlayingMobile() {
    const view = document.getElementById('now-playing-mobile');
    if (!view) return;

    document.getElementById('btn-npm-close')?.addEventListener('click', closeNowPlayingMobile);
    document.getElementById('npm-play')?.addEventListener('click', togglePlay);
    document.getElementById('npm-prev')?.addEventListener('click', playPrevious);
    document.getElementById('npm-next')?.addEventListener('click', playNext);
    document.getElementById('npm-mode')?.addEventListener('click', cycleMode);
    document.getElementById('npm-fav')?.addEventListener('click', () => {
        if (player.current) toggleFavorite(player.current);
    });
    document.getElementById('npm-desktop-lyrics')?.addEventListener('click', toggleDesktopLyrics);
    document.getElementById('npm-queue')?.addEventListener('click', toggleQueueDrawer);
    document.getElementById('npm-download')?.addEventListener('click', () => {
        if (player.current) downloadTrack(player.current);
    });
    document.getElementById('npm-sleep-timer')?.addEventListener('click', showSleepTimerPopover);

    const track = document.getElementById('npm-track');
    if (track) {
        track.addEventListener('click', (event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            seekToFraction((event.clientX - rect.left) / rect.width);
        });
    }

    // Swipe-down closes the view, swipe-up is a no-op (no need to fight
    // the scroll).  The 60-px threshold is enough that a tiny accidental
    // tap on the cover doesn't dismiss the view.
    let startY = null;
    view.addEventListener('touchstart', (event) => {
        if (event.touches.length !== 1) { startY = null; return; }
        startY = event.touches[0].clientY;
    }, { passive: true });
    view.addEventListener('touchend', (event) => {
        if (startY === null) return;
        const endY = event.changedTouches[0].clientY;
        const delta = endY - startY;
        if (delta > 60) closeNowPlayingMobile();
        startY = null;
    }, { passive: true });
}

/**
 * Mirror the desktop now-playing state into the mobile panel.  The body
 * class flips with the viewport, so calling this on every render is cheap
 * when the panel is hidden.
 */
function renderNowPlayingMobile() {
    if (!document.body.classList.contains('is-mobile')) return;
    const track = player.current;
    const resolved = player.resolved || {};
    const titleEl = document.getElementById('npm-track-title');
    const artistEl = document.getElementById('npm-track-artist');
    const coverEl = document.getElementById('npm-cover');
    const bgEl = document.getElementById('npm-bg');
    const kindEl = document.getElementById('npm-kind');
    const qualityEl = document.getElementById('npm-quality');
    const sourceEl = document.getElementById('npm-source');
    const titleBar = document.getElementById('npm-title');
    const favBtn = document.getElementById('npm-fav');
    if (!titleEl || !coverEl) return;

    if (!track) {
        if (titleEl) titleEl.textContent = t('player.nothing_playing');
        if (artistEl) artistEl.textContent = '';
        if (kindEl) kindEl.textContent = '';
        if (qualityEl) qualityEl.textContent = '';
        if (sourceEl) sourceEl.textContent = '';
        if (titleBar) titleBar.textContent = t('player.nothing_playing');
        coverEl.removeAttribute('src');
        coverEl.classList.add('is-empty');
        if (bgEl) bgEl.style.backgroundImage = '';
        if (favBtn) {
            setIcon(favBtn, 'heart');
            favBtn.classList.remove('fav-on');
        }
        return;
    }

    titleEl.textContent = track.name || '-';
    renderArtistLinks(artistEl, track);
    if (kindEl) kindEl.textContent = t('player.badge_' + (resolved.kind || track.source || 'online'));
    if (qualityEl) qualityEl.textContent = levelText(resolved.quality || resolved.level);
    if (sourceEl) {
        const srcName = (player.source && player.source.name) || '';
        sourceEl.textContent = srcName ? srcName.slice(0, 32) : '';
    }
    if (titleBar) titleBar.textContent = track.name || '-';

    const art = coverSrc({ cover_url: resolved.cover_url || track.cover_url || '' });
    // background-image: url(...) can't safely hold a JS template literal
    // with embedded regex + escaped quotes (the JS scanner in the frontend
    // test would mis-parse the regex body and treat the rest of the file
    // as one giant string).  Escape the value once and inline it.
    if (art) {
        coverEl.src = art;
        coverEl.classList.remove('is-empty');
        coverEl.onerror = () => { coverEl.removeAttribute('src'); coverEl.classList.add('is-empty'); };
        if (bgEl) {
            const safeArt = art.replace(/"/g, '%22');
            bgEl.style.backgroundImage = 'url("' + safeArt + '")';
        }
    } else {
        coverEl.removeAttribute('src');
        coverEl.classList.add('is-empty');
        if (bgEl) bgEl.style.backgroundImage = '';
    }
    if (favBtn) {
        const key = trackKeyOf(track);
        const favorite = player.favoriteKeys.has(key);
        setIcon(favBtn, 'heart', { filled: favorite });
        favBtn.classList.toggle('fav-on', favorite);
        favBtn.classList.toggle('fav-off', !favorite);
    }

    // Reflect play/pause + mode button state.
    updateMobileControlStates();
}

function updateMobileControlStates() {
    const playBtn = document.getElementById('npm-play');
    if (playBtn) {
        const playing = !audio.paused && !audio.ended;
        setIcon(playBtn, playing ? 'pause' : 'play');
    }
    const modeBtn = document.getElementById('npm-mode');
    if (modeBtn) {
        setIcon(modeBtn, MODE_ICONS[player.mode] || MODE_ICONS.list);
        modeBtn.title = t('player.mode_' + player.mode);
    }
    const current = document.getElementById('npm-current');
    const duration = document.getElementById('npm-duration');
    const fill = document.getElementById('npm-fill');
    const total = isFinite(audio.duration) ? audio.duration : 0;
    const ratio = total ? (audio.currentTime / total) * 100 : 0;
    if (fill) fill.style.width = `${Math.min(100, Math.max(0, ratio))}%`;
    if (current) current.textContent = fmtTime(audio.currentTime);
    if (duration) {
        duration.textContent = total
            ? fmtTime(total)
            : (player.current ? fmtDuration(player.current.duration) : '0:00');
    }
}

/* ---------- mobile lyrics renderer ----------
 * Reuses player.lyrics but writes into the npm scroll container.  Same
 * timing logic as the desktop panel (player.lyricOffset + active index).
 */
function initMobileLyricsRenderer() {
    const box = document.getElementById('npm-lyrics');
    if (!box) return;
    // Use the existing desktop highlight cadence but render into the npm
    // container.  updateLyricHighlight already does the binary search and
    // scroll math; this just renders.
    player.lyrics = player.lyrics || [];
    clearLyricsMessage(box);
    box.innerHTML = '';
    resetLyricsList(box);
    if (!player.lyrics.length) {
        // Same centred message as the desktop panel, so "暂无歌词" is in the
        // middle of the area the user is looking at instead of the top.
        setLyricsMessage(box, t('lyrics.none'));
        return;
    }
    const showTranslation = (state.settings && state.settings.playback.show_translation) !== false;
    const fragment = document.createDocumentFragment();
    player.lyrics.forEach((line) => {
        const element = document.createElement('div');
        element.className = 'lyric-line';
        element.dataset.time = String(line.time);
        element.textContent = line.text || '';
        if (showTranslation && line.translation) {
            const t = document.createElement('span');
            t.className = 'lyric-translation';
            t.textContent = line.translation;
            element.appendChild(t);
        }
        fragment.appendChild(element);
    });
    box.appendChild(fragment);
}

function renderMobileLyrics() {
    if (!document.body.classList.contains('is-mobile')) return;
    initMobileLyricsRenderer();
}

function updateMobileLyricHighlight() {
    if (!document.body.classList.contains('is-mobile')) return;
    const box = document.getElementById('npm-lyrics');
    if (!box) return;
    const ms = audio.currentTime * 1000 - (player.lyricOffset || 0);
    let low = 0, high = player.lyrics.length - 1, found = -1;
    while (low <= high) {
        const mid = (low + high) >> 1;
        if (player.lyrics[mid].time <= ms) { found = mid; low = mid + 1; }
        else high = mid - 1;
    }
    const prev = box.querySelector('.lyric-line.is-active');
    if (prev) prev.classList.remove('is-active');
    player.lyricIndex = found;
    const lines = box.querySelectorAll('.lyric-line');
    const line = lines[found];
    if (!line) return;
    line.classList.add('is-active');
    // Keep the active line centred, like Apple Music's now-playing.  Same
    // rect-based maths as the desktop panel: `offsetTop` is relative to
    // whatever happens to be positioned, which is not always this box.
    syncLyricsPadding(box);
    if (lyricsList(box).following) centerLyricLine(box, line, true);
}

/** Re-read the list that is on screen, skipping every cache. */
async function refreshCurrentSource() {
    const source = player.sourceTab;
    if (source === 'liked') {
        await loadLiked(true);
    } else if (source === 'followed_artists') {
        // An open artist page is what the refresh button belongs to when it
        // is showing; the list behind it is already up to date after a
        // follow.  Re-reading the list would look like nothing happened.
        if (player.artistOpen) {
            await openArtistDetail(player.artistOpen);
        } else {
            await loadFollowedArtists();
        }
    } else if (source === 'playlists') {
        if (player.playlistOpen) {
            await openPlaylist(player.playlistOpen, true);
        } else {
            await loadPlaylists(true);
        }
    } else {
        // The other tabs hold data that does not change under the user's
        // feet; a plain reload of the current tab is enough.
        await selectSource(source);
    }
}

async function loadPlaylists(fresh) {
    setListTitle(t('player.source_playlists'));
    const container = document.getElementById('playlist-list');
    container.innerHTML = `<p class="empty">${t('common.loading')}</p>`;
    document.getElementById('track-list').innerHTML = '';
    const result = await state.api.get_playlists(!!fresh);
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
        // The permissions come from the server, which knows who created the
        // playlist.  Falling back to the old guess would put the rename and
        // delete buttons on playlists the account does not own.
        const canUnsubscribe = playlist.can_unsubscribe !== undefined
            ? playlist.can_unsubscribe
            : (playlist.subscribed && !playlist.is_own);
        const canSubscribe = playlist.can_subscribe !== undefined
            ? playlist.can_subscribe
            : (!playlist.subscribed && !playlist.is_own);
        const canRename = playlist.can_rename !== undefined
            ? playlist.can_rename
            : playlist.is_own;
        const canDelete = playlist.can_delete !== undefined
            ? playlist.can_delete
            : playlist.is_own;
        const cover = document.createElement('img');
        cover.alt = '';
        cover.loading = 'lazy';
        cover.decoding = 'async';
        if (playlist.cover_url) setCoverImg(cover, playlist.cover_url);
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
        
        // Someone else's playlist: the only thing to do with it is to
        // un-subscribe.  No rename, no track edits, no delete.
        if (canRename) {
            const editBtn = document.createElement('button');
            editBtn.className = 'btn btn-icon btn-sm';
            editBtn.type = 'button';
            setIcon(editBtn, 'edit');
            editBtn.title = t('player.playlist_edit');
            editBtn.addEventListener('click', (e) => { e.stopPropagation(); editPlaylist(playlist); });
            actions.appendChild(editBtn);
        }

        if (canSubscribe || canUnsubscribe) {
            const subBtn = document.createElement('button');
            subBtn.className = 'btn btn-icon btn-sm';
            subBtn.type = 'button';
            setIcon(subBtn, 'star', { filled: playlist.subscribed });
            subBtn.title = canUnsubscribe ? t('player.playlist_unsubscribe') : t('player.playlist_subscribe');
            subBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleSubscribePlaylist(playlist); });
            actions.appendChild(subBtn);
        }

        // Your own playlist: rename, edit the tracks, delete.  Never
        // un-subscribe -- the star on your own playlist means nothing.
        if (canDelete) {
            const delBtn = document.createElement('button');
            delBtn.className = 'btn btn-icon btn-sm';
            delBtn.type = 'button';
            setIcon(delBtn, 'trash');
            delBtn.title = t('player.playlist_delete');
            delBtn.addEventListener('click', (e) => { e.stopPropagation(); deletePlaylist(playlist); });
            actions.appendChild(delBtn);
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

async function openPlaylist(playlist, fresh) {
    player.playlistOpen = playlist;
    document.getElementById('playlist-list').innerHTML =
        `<p class="empty">${t('common.loading')}</p>`;
    const result = await state.api.get_playlist_tracks(playlist.id, !!fresh);
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
    player.currentLocalPlaylist = null;
    player.artistOpen = null;
    document.getElementById('btn-list-back').hidden = true;
    // The tracks of the playlist we just left are still in player.visible
    // and still drawn inside #track-list; without clearing here the song
    // rows appear underneath the playlist list and look like leaked songs
    // in the "My playlists" tab the moment the user returns.
    const trackList = document.getElementById('track-list');
    if (trackList) trackList.innerHTML = '';
    player.visible = [];
    player.visibleSource = null;
    // A detail opened from a list that is not "My playlists" goes back to
    // *that* list -- dropping the user into the playlist tab would leave
    // the sidebar pointing at a tab the body is not showing.
    if (player.sourceTab !== 'playlists' && player.sourceTab !== 'local_playlists') {
        selectSource(player.sourceTab);
        return;
    }
    renderPlaylists(player.playlists);
    setListTitle(t('player.source_playlists'));
}

/* ----------------------------------------------------------- playlist management */

async function createPlaylist() {
    const name = prompt(t('player.playlist_name_prompt'));
    if (!name) return;
    
    const result = await state.api.create_playlist(name, 0, '');
    if (result.success) {
        const createdId = result.playlist_id;
        showToast('success', t('player.playlist_created', { name }), undefined, {
            undoLabel: t('common.undo'),
            onUndo: () => runUndo(
                () => state.api.delete_playlist([createdId]),
                () => loadPlaylists(),
                'player.delete_failed',
            ),
        });
        await loadPlaylists();
    } else {
        showError(result, 'player.create_failed');
    }
}

async function editPlaylist(playlist) {
    const name = prompt(t('player.playlist_rename'), playlist.name);
    if (!name || name === playlist.name) return;
    
    const before = playlist.name;
    const result = await state.api.update_playlist(playlist.id, { name });
    if (result.success) {
        showToast('success', t('player.playlist_renamed', { name }), undefined, {
            undoLabel: t('common.undo'),
            onUndo: () => runUndo(
                () => state.api.update_playlist(playlist.id, { name: before }),
                () => {
                    playlist.name = before;
                    renderPlaylists(player.playlists);
                },
                'player.update_failed',
            ),
        });
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
        showToast('success', subscribe ? t('player.playlist_subscribed') : t('player.playlist_unsubscribed'), undefined, {
            undoLabel: t('common.undo'),
            onUndo: () => runUndo(
                () => state.api.subscribe_playlist(playlist.id, !subscribe),
                (undo) => {
                    playlist.subscribed = !subscribe;
                    renderPlaylists(player.playlists);
                },
                subscribe ? 'player.unsubscribe_failed' : 'player.subscribe_failed',
            ),
        });
        renderPlaylists(player.playlists);
    } else {
        showError(result, subscribe ? 'player.subscribe_failed' : 'player.unsubscribe_failed');
    }
}

async function loadLiked(fresh) {
    setListTitle(t('player.source_liked'));
    document.getElementById('playlist-list').innerHTML = '';
    const result = await state.api.get_liked_tracks(!!fresh);
    if (!result.success) {
        renderEmptyTracks(t(result.error_key || 'player.login_required'));
        return;
    }
    playTracksInto(result.tracks, { kind: 'liked', name: t('player.source_liked') });
}

/* ------------------------------------------------------- followed artists */

/** How many followed artists one page holds. */
const FOLLOWED_PAGE = 25;

async function loadFollowedArtists(more) {
    setListTitle(t('player.source_followed_artists'));
    const container = document.getElementById('playlist-list');
    container.innerHTML = `<p class="empty">${t('common.loading')}</p>`;
    // An open artist page lives in #track-list; reloading the list behind it
    // must not wipe the page the user is reading.
    if (!player.artistOpen) document.getElementById('track-list').innerHTML = '';
    if (!more) {
        player.followedArtists = [];
        player.followedOffset = 0;
        player.followedTotal = 0;
    }
    const result = await state.api.get_followed_artists(FOLLOWED_PAGE, player.followedOffset);
    if (!result.success) {
        container.innerHTML = '';
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t(result.error_key || 'player.login_required');
        container.appendChild(empty);
        // Nothing was appended, so there is nothing more to load either.
        document.getElementById('btn-list-more').hidden = true;
        return;
    }
    const artists = result.artists || [];
    player.followedArtists = more
        ? player.followedArtists.concat(artists)
        : artists;
    player.followedOffset += artists.length;
    player.followedTotal = result.count || 0;
    renderFollowedArtists(player.followedArtists);
    document.getElementById('btn-list-more').hidden = !result.has_more;
}

function renderFollowedArtists(artists) {
    renderArtistRows(artists, document.getElementById('playlist-list'));
}

/**
 * Render artist rows into a container.
 *
 * Every row opens the artist page and carries a follow toggle, so the
 * list and the page can never disagree about the follow state.
 */
function renderArtistRows(artists, container, emptyKey) {
    container.innerHTML = '';
    if (!artists.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t(emptyKey || 'player.followed_artists_empty');
        container.appendChild(empty);
        setListCount(0);
        return;
    }
    const fragment = document.createDocumentFragment();
    artists.forEach((artist) => {
        const row = document.createElement('div');
        row.className = 'playlist-row';
        row.dataset.artistId = artist.id;
        const cover = document.createElement('img');
        cover.alt = '';
        cover.loading = 'lazy';
        cover.decoding = 'async';
        if (artist.cover_url) setCoverImg(cover, artist.cover_url);
        cover.onerror = () => cover.removeAttribute('src');
        const meta = document.createElement('div');
        meta.className = 'pl-meta';
        const name = document.createElement('span');
        name.className = 'pl-name';
        name.textContent = artist.name;
        const sub = document.createElement('span');
        sub.className = 'pl-sub';
        // The followed list does not carry a song count, so the line falls
        // back to the album count alone rather than showing a wrong zero.
        sub.textContent = artist.music_size
            ? t('player.artist_meta', {
                songs: artist.music_size,
                albums: artist.album_size || 0,
            })
            : t('player.artist_albums_count', { count: artist.album_size || 0 });
        meta.append(name, sub);

        const actions = document.createElement('div');
        actions.className = 'playlist-actions';
        actions.style.display = 'none';
        const followBtn = document.createElement('button');
        followBtn.className = 'btn btn-icon btn-sm';
        followBtn.type = 'button';
        followBtn.dataset.followIcon = '1';
        renderFollowButton(followBtn, !!artist.followed);
        followBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleFollowArtist(artist, followBtn);
        });
        actions.appendChild(followBtn);

        row.append(cover, meta, actions);
        row.addEventListener('mouseenter', () => { actions.style.display = 'flex'; });
        row.addEventListener('mouseleave', () => { actions.style.display = 'none'; });
        row.addEventListener('click', () => openArtistDetail(artist));
        fragment.appendChild(row);
    });
    container.appendChild(fragment);
    setListCount(artists.length);
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
        if (playlist.cover_url) setCoverImg(cover, playlist.cover_url);
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
        setIcon(editBtn, 'edit');
        editBtn.title = t('player.playlist_edit');
        editBtn.addEventListener('click', (e) => { e.stopPropagation(); editLocalPlaylist(playlist); });
        actions.appendChild(editBtn);
        
        // Delete
        const delBtn = document.createElement('button');
        delBtn.className = 'btn btn-icon btn-sm';
        delBtn.type = 'button';
        setIcon(delBtn, 'trash');
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
        const created = (result.playlist || {}).id;
        showToast('success', t('player.playlist_created', { name }), undefined, {
            undoLabel: t('common.undo'),
            onUndo: () => runUndo(
                () => state.api.delete_local_playlist(created),
                () => loadLocalPlaylists(),
                'player.delete_failed',
            ),
        });
        await loadLocalPlaylists();
    } else {
        showError(result, 'player.create_failed');
    }
}

async function editLocalPlaylist(playlist) {
    const name = prompt(t('player.playlist_rename'), playlist.name);
    if (!name || name === playlist.name) return;
    
    const before = playlist.name;
    const result = await state.api.update_local_playlist(playlist.id, { name });
    if (result.success) {
        showToast('success', t('player.playlist_renamed', { name }), undefined, {
            undoLabel: t('common.undo'),
            onUndo: () => runUndo(
                () => state.api.update_local_playlist(playlist.id, { name: before }),
                (undo) => {
                    playlist.name = before;
                    renderLocalPlaylists(player.localPlaylists);
                },
                'player.update_failed',
            ),
        });
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
        showToast('success', t('player.track_removed'), undefined, {
            undoLabel: t('common.undo'),
            onUndo: () => runUndo(
                () => state.api.add_tracks_to_local_playlist(playlist.id, [track.key]),
                () => {
                    // Put the row back where the screen expects to find it:
                    // the local list renders from this array, not from disk.
                    const current = player.currentLocalPlaylist;
                    const list = current && Array.isArray(current.tracks) ? current.tracks : null;
                    if (list && !list.some((row) => trackKeyOf(row) === track.key)) {
                        list.push(track);
                    }
                    if (list) renderLocalPlaylistTracks(list);
                },
                'player.add_failed',
            ),
        });
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

/* The four playlist CRUD helpers (createPlaylist / editPlaylist /
   deletePlaylist / toggleSubscribePlaylist) are defined earlier in this
   file -- a second copy used to live here.  They were removed in the
   2026-10-18 cleanup because keeping both silently masked which one the
   UI actually calls. */

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
        showToast('success', t('player.track_removed'), undefined, {
            undoLabel: t('common.undo'),
            onUndo: () => runUndo(
                () => state.api.add_tracks_to_playlist(playlist.id, [track.id]),
                () => refreshCurrentSource(),
                'player.add_failed',
            ),
        });
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
        showToast('success', t('player.tracks_added', { count: trackIds.length }), undefined, {
            undoLabel: t('common.undo'),
            onUndo: () => runUndo(
                () => state.api.remove_tracks_from_playlist(player.targetPlaylist.id, trackIds),
                () => loadPlaylistTracksForModal(player.targetPlaylist),
                'player.remove_failed',
            ),
        });
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
    if (activeType === 'artists') {
        // An artist is not a track, so the results go into the side list as
        // rows that open the artist page instead of into the track list.
        renderArtistRows(results.artists || [], document.getElementById('playlist-list'),
                         'player.search_no_artists');
        player.visible = [];
        player.visibleSource = null;
        document.getElementById('track-list').innerHTML = '';
        setListCount((results.artists || []).length);
        return;
    }
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
    // The album page replaces the artist page it may have been opened from,
    // so a refresh must not bring the artist back.
    player.artistOpen = null;
    document.getElementById('playlist-list').innerHTML = '';
    document.getElementById('track-list').innerHTML = '';
    document.getElementById('btn-list-back').hidden = false;
    setListTitle(t('player.album_detail'));
    
    // Show album info at top
    const trackList = document.getElementById('track-list');
    trackList.innerHTML = '';
    const albumInfo = document.createElement('div');
    albumInfo.className = 'album-detail-header';
    // renderTrackList() must not throw this away when it re-renders.
    albumInfo.dataset.listHeader = '1';
    const albumCoverUrl = result.album.cover_url || '';
    albumInfo.innerHTML = `
        <img src="" alt="" class="album-detail-cover" data-cover-src="${albumCoverUrl}" onerror="this.style.display='none'">
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
        playAllInto(result.tracks, { kind: 'album', id: album.id, name: album.name });
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
    // Now that the markup is in the DOM we can run the proxy dance and
    // -- once the loopback URL is in the cache -- the <img> lights up.
    setCoverImg(albumInfo.querySelector('.album-detail-cover'), albumCoverUrl);

    playTracksInto(result.tracks, { kind: 'album', id: album.id, name: album.name });
}

async function openArtistDetail(artist) {
    const result = await state.api.get_artist_detail(artist.id);
    if (!result.success) {
        showError(result, 'player.artist_not_found');
        return;
    }
    player.artistOpen = { id: artist.id, name: result.artist.name || artist.name };
    document.getElementById('playlist-list').innerHTML = '';
    document.getElementById('track-list').innerHTML = '';
    document.getElementById('btn-list-back').hidden = false;
    setListTitle(result.artist.name || t('player.artist_detail'));

    const trackList = document.getElementById('track-list');
    trackList.innerHTML = '';

    // The header is built node by node: the artist name and the brief
    // description come from the server, and a name containing "<" must not
    // be able to break the page.
    const artistHeader = document.createElement('div');
    artistHeader.className = 'artist-detail-header';
    // renderTrackList() must not throw this away when it re-renders.
    artistHeader.dataset.listHeader = '1';

    const artistCover = document.createElement('img');
    artistCover.className = 'artist-detail-cover';
    artistCover.alt = '';

    const meta = document.createElement('div');
    meta.className = 'artist-detail-meta';
    const nameEl = document.createElement('h3');
    nameEl.textContent = result.artist.name || '';
    meta.appendChild(nameEl);
    if (result.artist.description) {
        const description = document.createElement('p');
        description.className = 'muted';
        description.textContent = result.artist.description;
        meta.appendChild(description);
    }

    const actions = document.createElement('div');
    actions.className = 'artist-detail-actions';
    const playHotBtn = document.createElement('button');
    playHotBtn.className = 'btn btn-primary btn-sm';
    playHotBtn.type = 'button';
    playHotBtn.textContent = `${t('player.play_all')} ${t('player.hot_songs')}`;
    const albumsBtn = document.createElement('button');
    albumsBtn.className = 'btn btn-secondary btn-sm';
    albumsBtn.type = 'button';
    albumsBtn.textContent = t('player.artist_albums');
    const followBtn = document.createElement('button');
    followBtn.className = 'btn btn-secondary btn-sm';
    followBtn.type = 'button';
    actions.append(playHotBtn, albumsBtn, followBtn);
    meta.appendChild(actions);

    artistHeader.append(artistCover, meta);
    trackList.appendChild(artistHeader);

    const artistCoverUrl = result.artist.cover_url || '';
    playHotBtn.addEventListener('click', () => {
        playAllInto(result.hot_songs, { kind: 'artist', id: artist.id, name: artist.name });
    });
    // The albums belong under the header, not instead of it: replacing the
    // page would take the follow button away with no way back.
    albumsBtn.addEventListener('click', () => {
        showArtistAlbums(result.albums, artistHeader);
    });
    // The button works on an object with a ``followed`` flag, so the state
    // the server reported is carried over into it.
    const followTarget = { id: artist.id, followed: !!result.followed };
    renderFollowButton(followBtn, followTarget.followed);
    followBtn.addEventListener('click', () => toggleFollowArtist(followTarget, followBtn));
    setCoverImg(artistCover, artistCoverUrl);

    // Hot songs
    if (result.hot_songs && result.hot_songs.length) {
        const section = document.createElement('div');
        section.className = 'artist-section';
        const heading = document.createElement('h4');
        heading.textContent = t('player.hot_songs');
        section.appendChild(heading);
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

/**
 * Label the follow button for the current state.
 *
 * The two states have to be told apart at a glance, so the label says
 * what the button will *do*, not what the state is.  The icon variant
 * (a star in the list rows) shows the state as a filled star instead.
 */
function renderFollowButton(button, followed) {
    if (!button) return;
    button.classList.toggle('is-active', followed);
    if (button.dataset.followIcon === '1') {
        setIcon(button, 'star', { filled: followed });
        button.title = followed ? t('player.artist_unfollow') : t('player.artist_follow');
        return;
    }
    button.textContent = followed ? t('player.artist_unfollow') : t('player.artist_follow');
}

/** Follow or unfollow an artist and refresh what depends on it. */
async function toggleFollowArtist(artist, button) {
    const follow = !artist.followed;
    if (button) button.disabled = true;
    const result = await state.api.follow_artist(artist.id, follow);
    if (button) button.disabled = false;
    if (!result.success) {
        showError(result, follow ? 'player.artist_follow_failed' : 'player.artist_unfollow_failed');
        return;
    }
    artist.followed = result.followed;
    if (button) renderFollowButton(button, result.followed);
    const following = result.followed;
    showToast('success', following
        ? t('player.artist_followed')
        : t('player.artist_unfollowed'), undefined, {
        undoLabel: t('common.undo'),
        onUndo: () => runUndo(
            () => state.api.follow_artist(artist.id, !following),
            async (undo) => {
                artist.followed = undo.followed;
                if (button) renderFollowButton(button, undo.followed);
                if (player.sourceTab === 'followed_artists') await loadFollowedArtists();
            },
            following ? 'player.artist_unfollow_failed' : 'player.artist_follow_failed',
        ),
    });
    // The followed list changed, so it has to be re-read -- but only when it
    // is the list on screen.
    if (player.sourceTab === 'followed_artists') {
        await loadFollowedArtists();
    }
}

/**
 * Show the artist's albums under the header.
 *
 * The section is appended, not swapped in: the follow button and the hot
 * songs stay on screen, and pressing the button twice does not stack two
 * copies of the same list.
 */
function showArtistAlbums(albums, header) {
    const anchor = header || document.getElementById('track-list');
    const existing = anchor.parentElement
        && anchor.parentElement.querySelector('.artist-section[data-section="albums"]');
    if (existing) {
        existing.remove();
        return;
    }
    const section = document.createElement('div');
    section.className = 'artist-section';
    section.dataset.section = 'albums';
    const heading = document.createElement('h4');
    heading.textContent = t('player.artist_albums');
    section.appendChild(heading);

    if (!albums || !albums.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t('player.album_not_found');
        section.appendChild(empty);
        anchor.after(section);
        return;
    }

    albums.forEach((album) => {
        const row = document.createElement('div');
        row.className = 'album-row';
        const cover = document.createElement('img');
        cover.className = 'album-row-cover';
        cover.alt = '';
        const meta = document.createElement('div');
        meta.className = 'album-row-meta';
        const name = document.createElement('span');
        name.className = 'album-row-name';
        name.textContent = album.name;
        const date = document.createElement('span');
        date.className = 'muted album-row-date';
        date.textContent = album.publish_time
            ? String(new Date(album.publish_time).getFullYear())
            : '';
        meta.append(name, date);
        row.append(cover, meta);
        row.addEventListener('click', () => openAlbumDetail(album));
        setCoverImg(cover, album.cover_url || '');
        section.appendChild(row);
    });
    anchor.after(section);
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
 * Resolve a remote NetEase cover URL to a loopback ``/cover`` URL so the
 * webview can load it without tripping CORS, and the bytes get persisted
 * to the on-disk cache by the media server.  The result is memoised in
 * ``player.coverUrlCache``; ``null`` is returned for inputs that have no
 * cover at all so the caller can short-circuit and stop the spinner.
 */
function proxyCoverUrl(remote_url) {
    const raw = String(remote_url || '').trim();
    if (!raw) return '';
    if (!raw.startsWith('http://') && !raw.startsWith('https://')) return raw;

    // 1. The in-memory map (this session).
    if (player.coverUrlCache.has(raw)) return player.coverUrlCache.get(raw) || '';

    // 2. The loopback URL is fully determined by the remote URL, so it can
    //    be built here without asking the backend.  That removes one IPC
    //    round-trip per cover image -- with a few hundred covers in a
    //    playlist that was hundreds of round-trips on every launch, and
    //    the artwork only appeared after the last one came back.
    const built = buildCoverUrl(raw);
    if (built) {
        player.coverUrlCache.set(raw, built);
        return built;
    }

    // 3. No base URL yet (the app info has not arrived): fall back to the
    //    old asynchronous path so nothing is lost.
    if (!state.api || typeof state.api.resolve_cover_url !== 'function') return raw;
    if (player.coverUrlPending.has(raw)) return '';
    player.coverUrlPending.add(raw);
    state.api.resolve_cover_url({ cover_url: raw }).then((result) => {
        player.coverUrlPending.delete(raw);
        if (result && result.success && result.url) {
            player.coverUrlCache.set(raw, result.url);
        } else {
            // ``null`` means "no cover" so we do not retry on every render.
            player.coverUrlCache.set(raw, '');
        }
        // Re-render any visible rows that were waiting on this URL.  The
        // lock-screen artwork is updated too so a track that was already
        // playing picks up the loopback URL instead of the raw CDN one.
        if (typeof renderTrackList === 'function') renderTrackList();
        if (typeof renderNowPlaying === 'function') renderNowPlaying();
        if (typeof updateMediaSessionMetadata === 'function'
                && player.current
                && String(player.current.cover_url || '') === raw) {
            updateMediaSessionMetadata();
        }
    }).catch(() => {
        player.coverUrlPending.delete(raw);
        player.coverUrlCache.set(raw, '');
    });
    return '';
}

/**
 * Build the loopback cover URL from the media server's base URL.
 *
 * The server's route is ``/cover?u=<urlencoded remote>&ext=<ext>`` and it
 * caches the bytes on disk, so the same URL always yields the same image.
 * Returns '' when the base URL is not known yet.
 */
function buildCoverUrl(raw) {
    const base = state.mediaBaseUrl;
    if (!base) return '';
    let ext = 'jpg';
    try {
        const path = String(raw).split('?')[0];
        const match = path.match(/\.([A-Za-z0-9]{2,4})$/);
        if (match) {
            const candidate = match[1].toLowerCase();
            if (['jpg', 'jpeg', 'png', 'webp', 'gif'].includes(candidate)) {
                ext = candidate === 'jpeg' ? 'jpg' : candidate;
            }
        }
    } catch (e) { /* keep the default */ }
    return base + '/cover?u=' + encodeURIComponent(raw) + '&ext=' + ext;
}

/**
 * Convenience wrapper for ``<img>`` sources.  Returns a synchronous
 * ``src`` value when the cover is already cached, otherwise an empty
 * string so the browser shows nothing rather than a broken icon while the
 * backend resolves the URL.
 */
function coverSrc(track) {
    const raw = String(track && track.cover_url || '').trim();
    if (!raw) return '';
    if (!raw.startsWith('http://') && !raw.startsWith('https://')) return raw;
    return proxyCoverUrl(raw) || '';
}

/**
 * Drop-in ``src`` setter for a cover ``<img>`` that proxies the request
 * through the local media server.  When the backend has not answered yet we
 * leave the ``src`` attribute empty (no broken icon), then refill it as
 * soon as the proxy URL lands in the cache and ``renderTrackList`` fires
 * again -- ``proxyCoverUrl`` triggers a re-render on success.
 */
function setCoverImg(img, raw_url) {
    if (!img) return;
    const proxied = coverSrc({ cover_url: raw_url });
    if (proxied) {
        img.src = proxied;
        img.onerror = () => { img.removeAttribute('src'); };
    } else {
        img.removeAttribute('src');
        img.onerror = () => { img.removeAttribute('src'); };
    }
}

/**
 * Pick the artwork URL(s) we hand to ``MediaMetadata``.  Order of preference:
 *   1. The loopback ``/art`` URL when the resolved track is a local file
 *      (the media server is the only way the webview can read it anyway).
 *   2. The cached loopback ``/cover`` URL when one is in ``coverUrlCache``.
 *   3. The raw ``track.cover_url`` so the first play after launch still
 *      shows artwork -- the proxy will land in the cache for next time.
 * Returns an array of plain strings so the caller can map directly into
 * the MediaMetadata constructor.
 */
function collectArtworkUrls(track, resolved) {
    const out = [];
    if (resolved && resolved.cover_url) out.push(resolved.cover_url);
    const remote = String((track && track.cover_url) || '').trim();
    if (remote) {
        if (player.coverUrlCache.has(remote)) {
            const proxied = player.coverUrlCache.get(remote);
            if (proxied && !out.includes(proxied)) out.push(proxied);
        }
        if (!out.includes(remote)) out.push(remote);
    }
    return out.filter(Boolean);
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
    // A new list starts at the top; only re-windowing the *same* list keeps
    // the scroll position (see renderTrackList).
    const list = document.getElementById('track-list');
    const scroller = list && (list.closest('.list-area') || list);
    if (scroller && !sameSource(scroller._virtualSource, source)) {
        scroller.scrollTop = 0;
    }
    if (scroller) scroller._virtualSource = source;
    renderTrackList();
    setListCount(tracks.length);
}

/**
 * Put a list on screen and start playing it from the top.
 *
 * ``playTracksInto`` only replaces the *visible* list -- it does not touch
 * the playback queue, so a button labelled "play all" that only called it
 * silently did nothing.  The queue is set by ``startQueue``, which also
 * starts playback.
 */
async function playAllInto(tracks, source) {
    playTracksInto(tracks, source);
    if (player.visible && player.visible.length) {
        await startQueue(player.visible, 0, source);
    }
}

/** Whether two source descriptors point at the same list. */
function sameSource(a, b) {
    if (!a || !b) return false;
    return a.kind === b.kind && String(a.id || '') === String(b.id || '')
        && String(a.name || '') === String(b.name || '');
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
    // Clearing the list collapses it, and a collapsed scroller clamps its
    // scrollTop to 0 -- so reading the position after the clear would always
    // report "we are at the top" and every re-window would jump the view back
    // to row 1.  Restore it once the new rows are in.
    const scroller = list.closest('.list-area') || list;
    const savedScroll = scroller.scrollTop;
    // The album and artist pages put their header (cover, name, bio, action
    // buttons) in this same container.  A plain ``innerHTML = ''`` threw it
    // away on the very next render, which is how both detail pages shipped
    // with no header at all.  A node marked as the header survives.
    const keptHeader = list.querySelector(':scope > [data-list-header]');
    list.innerHTML = '';
    if (keptHeader) list.appendChild(keptHeader);
    const playing = currentKey();
    // For large lists, mount only a window of rows around the viewport so a
    // 1000-track playlist costs ~30 DOM nodes instead of 1000.  Below the
    // threshold we keep the original full render to avoid a scroll listener
    // (the win is small and the regression risk is not).
    const total = player.visible.length;
    if (total > 200) {
        mountVirtualTrackList(list, player.visible, playing);
        // A fresh source starts at the top; a re-window keeps the position.
        if (savedScroll > 0 && scroller.scrollTop < savedScroll) {
            scroller.scrollTop = savedScroll;
        }
        return;
    }
    const fragment = document.createDocumentFragment();
    player.visible.forEach((track, index) => {
        fragment.appendChild(buildTrackRow(track, index, playing));
    });
    list.appendChild(fragment);
    if (savedScroll > 0 && scroller.scrollTop < savedScroll) {
        scroller.scrollTop = savedScroll;
    }
}

/**
 * Windowed track-list renderer.  Pads above + below the visible scroll
 * position so the scroll bar's perceived size and ratio stay correct.
 * The pad is intentionally small (6 rows) so the row lookup is O(1) and
 * we still get most of the memory win.
 *
 * Two failure modes the old version had:
 *   1. ``container.clientHeight`` was 0 on the very first call (the parent
 *      column hadn't been laid out yet), which collapsed the visible window
 *      to a single row and left the rest of the list as blank space until
 *      the user scrolled.
 *   2. The window never adapted when the column got taller/shorter after
 *      the user resized the window or the sidebar collapsed.
 * Both are fixed by measuring the height in a layout-stable callback and
 * installing a ResizeObserver on the container.
 */
function mountVirtualTrackList(container, tracks, playingKey) {
    const ROW_H = 50;        // matches `.track-row` (6px*2 padding + ~38px content)
    const PAD = 6;
    const total = tracks.length;

    // The scrolling element owns the geometry.  `#track-list` is
    // ``height: auto``; the element that actually scrolls -- and therefore
    // owns ``scrollTop`` and ``clientHeight`` -- is its parent
    // ``.list-area``.  Measuring the container gave a height of 0 and a
    // scrollTop of 0, so every list of more than 200 tracks rendered as
    // zero rows: exactly what a user sees when opening a big playlist
    // from "我的歌单".
    const scroller = container.closest('.list-area') || container;

    if (scroller.clientHeight <= 0) {
        // Layout has not happened yet -- wait for one frame and retry.  We
        // keep the existing ones in place so the user does not see a flash
        // of empty content; if the observer below is already wired up it
        // will pick up the size change by itself.
        if (!container.dataset.virtualPending) {
            container.dataset.virtualPending = '1';
            requestAnimationFrame(() => {
                container.dataset.virtualPending = '';
                if (container.isConnected) renderTrackList();
            });
        }
        return;
    }

    // `.list-area` can hold siblings above the list (a header, the playlist
    // grid), so the scroll offset has to be measured against the list's own
    // position inside the scroller rather than against 0.
    const listTop = Math.max(0, container.offsetTop - scroller.offsetTop);
    const viewport = Math.max(ROW_H, scroller.clientHeight - listTop);
    const offset = Math.max(0, scroller.scrollTop - listTop);

    let topIndex = Math.max(0, Math.floor(offset / ROW_H) - PAD);
    let bottomIndex = Math.min(total, topIndex + 1 +
        Math.ceil(viewport / ROW_H) + PAD * 2);

    const totalHeight = total * ROW_H;
    const topPad = topIndex * ROW_H;
    const bottomPad = totalHeight - bottomIndex * ROW_H;

    const placeholderTop = document.createElement('div');
    placeholderTop.style.height = topPad + 'px';
    placeholderTop.setAttribute('aria-hidden', 'true');
    const placeholderBottom = document.createElement('div');
    placeholderBottom.style.height = bottomPad + 'px';
    placeholderBottom.setAttribute('aria-hidden', 'true');

    const fragment = document.createDocumentFragment();
    fragment.appendChild(placeholderTop);
    for (let index = topIndex; index < bottomIndex; index += 1) {
        fragment.appendChild(buildTrackRow(tracks[index], index, playingKey));
    }
    fragment.appendChild(placeholderBottom);
    // Keep the rendered range reachable from the (single) scroll listener,
    // which is installed once and lives across re-renders.
    container._virtualRange = { top: topIndex, bottom: bottomIndex, listTop: listTop };
    container.appendChild(fragment);

    // Single passive scroll listener that re-renders the window.  Guarded
    // against a stack overflow when the user drags the thumb fast (rAF
    // coalesces multiple events into one repaint).
    if (!container.dataset.virtualBound) {
        container.dataset.virtualBound = '1';
        let pending = false;
        scroller.addEventListener('scroll', () => {
            if (pending) return;
            pending = true;
            requestAnimationFrame(() => {
                pending = false;
                // Re-render only if the window actually changed.  Compare
                // against the range we rendered: `topIndex` is already
                // padded, so comparing it with the raw row index would
                // re-render on every single scroll event.
                const range = container._virtualRange;
                if (!range) {
                    renderTrackList();
                    return;
                }
                const row = Math.floor(
                    Math.max(0, scroller.scrollTop - range.listTop) / ROW_H);
                if (row < range.top || row >= range.bottom) {
                    renderTrackList();
                }
            });
        }, { passive: true });

        // Adapt the virtualised window when the column gets taller or
        // shorter (window resize, sidebar collapse, language switch, ...).
        if (typeof ResizeObserver !== 'undefined') {
            const observer = new ResizeObserver(() => {
                if (container._virtualResizePending) return;
                container._virtualResizePending = true;
                requestAnimationFrame(() => {
                    container._virtualResizePending = false;
                    renderTrackList();
                });
            });
            observer.observe(scroller);
            container._virtualObserver = observer;
        }
    }
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
        setIcon(favButton, 'heart', { filled: favorite });
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
    // The playing row shows a note instead of its number.  The number is
    // kept in a data attribute so it can be restored when playback moves on.
    idx.dataset.number = String(index + 1);
    if (key && key === playingKey) setIcon(idx, 'music');
    else idx.textContent = idx.dataset.number;

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
    setIcon(fav, 'heart', { filled: player.favoriteKeys.has(key) });
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
        setIcon(download, 'download');
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
        setIcon(locate, 'reveal');
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
    // Drop any previously buffered media.  Without this, the previous track's
    // bytes stay reachable from the audio element and inflate RSS -- the
    // user-facing symptom is steady memory growth across many tracks.
    try {
        audio.pause();
        audio.removeAttribute('src');
        audio.load();
    } catch (err) { /* not all platforms allow this before the first src */ }
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
    setIcon(button, MODE_ICONS[player.mode] || MODE_ICONS.list);
    const label = t('player.mode_' + player.mode);
    button.title = label;
    button.setAttribute('aria-label', label);
    updateMobileControlStates();
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
    if (icon) setIcon(icon, (player.muted || player.volume === 0) ? 'volume-muted' : 'volume');
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
    // The cloud outcome tells more than "it worked" or "it did not": a track
    // NetEase refuses and a request that came too fast need different words,
    // and neither of them is the user's mistake.
    // Every branch here reports a write that already changed the local state,
    // including the ones that explain why the cloud refused it -- so every one
    // of them can be undone, not just the plain success.  ``!wanted`` restores
    // whichever state the track was in before the click.
    const undoFavorite = {
        undoLabel: t('common.undo'),
        onUndo: () => runUndo(
            () => state.api.set_favorite(track, !wanted),
            (undo) => {
                player.favoriteKeys = new Set(undo.favorites || []);
                refreshTrackRow(key);
                renderNowPlaying();
                if (undo.liked_dirty) refreshCurrentSource();
            },
            'error.unknown',
        ),
    };
    if (result.cloud === 'login_required') showToast('info', t('player.favorite_local_only'), 3600, undoFavorite);
    else if (result.cloud === 'unsupported') showToast('warning', t('player.favorite_cloud_unsupported'), 4200, undoFavorite);
    else if (result.cloud === 'rate_limited') showToast('warning', t('player.favorite_cloud_busy'), 3600, undoFavorite);
    else if (result.cloud === 'failed') showToast('warning', t('player.favorite_cloud_failed'), 3600, undoFavorite);
    else if (wanted) showToast('success', t('player.favorite_added'), undefined, undoFavorite);
    refreshTrackRow(key);
    renderNowPlaying();
    // The account's liked list changed, so the list on screen is out of
    // date.  Re-read it now instead of waiting for the cache to expire.
    if (result.liked_dirty) refreshCurrentSource();
}

/* ---------------------------------------------------------------- lyrics */

function renderLyricsLoading() {
    const box = document.getElementById('lyrics-scroll');
    if (!box) return;
    setLyricsMessage(box, t('lyrics.loading'));
    player.lyrics = [];
    player.lyricIndex = -1;
    const save = document.getElementById('btn-save-lyrics');
    if (save) save.hidden = true;
    // Mobile mirror: clear the npm lyrics too.
    const npmBox = document.getElementById('npm-lyrics');
    if (npmBox) npmBox.innerHTML = `<p class="empty">${t('lyrics.loading')}</p>`;
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
    setLyricsMessage(box, t('lyrics.loading'));
    let result;
    try {
        result = await state.api.get_track_lyrics(track, !!save);
    } catch (err) {
        result = null;
    }
    if (!result || !result.has_lyrics) {
        setLyricsMessage(box, t('lyrics.none'));
        player.lyrics = [];
        player.lyricIndex = -1;
        player.lyricSource = 'none';
        const saveButton = document.getElementById('btn-save-lyrics');
        if (saveButton) saveButton.hidden = true;
        renderLyricSource();
        renderMobileLyrics();
        pushLyricsToDesktop(true);
        return;
    }
    player.lyrics = result.lines || [];
    player.lyricIndex = -1;
    player.lyricSource = result.source || 'none';
    renderLyricLines();
    renderLyricSource();
    // The desktop overlay is a separate window; it only learns about the
    // lyrics when we push them.  Opening the overlay before a track was
    // playing used to leave it empty for the rest of the session.
    pushLyricsToDesktop(true);
    const saveButton = document.getElementById('btn-save-lyrics');
    if (saveButton) {
        // Offer to keep a lyric that had to be looked up online.
        saveButton.hidden = !(track.source === 'local' && result.source === 'netease'
            && !result.lyrics_path);
    }
    if (result.lyrics_path) showToast('success', t('lyrics.saved'), 2600);
    renderMobileLyrics();
}

/**
 * The vertical padding that lets the first and last line reach the middle.
 *
 * The value is `clientHeight / 2` in px, written to `--lyrics-pad`.  It has
 * to be px: a percentage padding resolves against the box's *width*, so the
 * old `padding: 40%` pushed the lyrics down by 40% of the window's width --
 * which only looks centred when the window happens to be about as wide as
 * it is tall.  On a maximised wide window the lyrics sat far too low.
 */
function syncLyricsPadding(box) {
    if (!box) return;
    watchLyricsBox(box);
    const pad = Math.max(0, Math.round((box.clientHeight || 0) / 2));
    if (Number(box.dataset.lyricsPad) === pad) return;
    box.dataset.lyricsPad = String(pad);
    box.style.setProperty('--lyrics-pad', pad + 'px');
}

let lyricsResizeObserver = null;

/** Recompute the padding whenever the lyrics area changes size. */
function watchLyricsBox(box) {
    if (!box || lyricsResizeObserver || typeof ResizeObserver === 'undefined') return;
    lyricsResizeObserver = new ResizeObserver(() => {
        syncLyricsPadding(box);
    });
    lyricsResizeObserver.observe(box);
}

/* ------------------------------------------------------ lyrics following */

/**
 * Who drives a lyrics list: the song, or the user.
 *
 * `lyrics-scroll` is the panel next to the player, `npm-lyrics` the
 * full-screen now-playing view on narrow windows.  Both keep the line
 * that is playing in the middle.  The user can scroll either one
 * somewhere else; the list then stays where they left it and returns
 * to the playing line once they have been idle for a while.
 *
 * The return is driven by a timer, not by the `scroll` event: a scroll
 * event only fires *while* the list moves, so a check placed there is
 * never evaluated again once the user stops scrolling.  The old code
 * did exactly that, and the panel never came back on its own.
 */
const lyricsLists = new Map();

/** The follow state of one lyrics list. */
function lyricsList(box) {
    let entry = lyricsLists.get(box);
    if (!entry) {
        entry = {
            following: true,     // does the list follow the playing line?
            idleTimer: null,     // pending return to the playing line
            autoScrollUntil: 0,  // scroll events before this are ours
            refocus: null,       // put the playing line back in the middle
        };
        lyricsLists.set(box, entry);
    }
    return entry;
}

/**
 * How long a lyrics list may stay where the user left it before it
 * returns to the line that is playing, in milliseconds.
 */
function lyricsIdleMs() {
    const seconds = Number(player.lyricsScrollIdleSeconds);
    if (!isFinite(seconds) || seconds < 1) return 4000;
    return seconds * 1000;
}

/**
 * Remember that the panel moved by itself, so the scroll event it
 * fires is not read as the user taking over.
 */
function markLyricsAutoScroll(box) {
    lyricsList(box).autoScrollUntil = Date.now() + 120;
}

/**
 * The list was rebuilt, so the browser fires a scroll event for the
 * position it clamped the list to.  That is not the user scrolling:
 * ignore it for a moment and follow the song again.
 */
function resetLyricsList(box) {
    const entry = lyricsList(box);
    entry.following = true;
    entry.autoScrollUntil = Date.now() + 250;
    if (entry.idleTimer) {
        clearTimeout(entry.idleTimer);
        entry.idleTimer = null;
    }
}

/**
 * The user moved the list: stop following the playing line and let
 * them read.  The list returns to the playing line on a timer, so it
 * comes back even though no further scroll events arrive.
 */
function takeOverLyrics(box) {
    const entry = lyricsList(box);
    entry.following = false;
    if (entry.idleTimer) clearTimeout(entry.idleTimer);
    entry.idleTimer = setTimeout(() => {
        entry.idleTimer = null;
        entry.following = true;
        if (entry.refocus) entry.refocus();
    }, lyricsIdleMs());
}

/**
 * Re-arm the pending return of every list, with the current interval.
 *
 * Called when the setting changes: a list that is already waiting for
 * the user to stop should wait for the new interval, not the old one.
 */
function rearmLyricsReturn() {
    lyricsLists.forEach((entry) => {
        if (!entry.idleTimer) return;
        clearTimeout(entry.idleTimer);
        entry.idleTimer = setTimeout(() => {
            entry.idleTimer = null;
            entry.following = true;
            if (entry.refocus) entry.refocus();
        }, lyricsIdleMs());
    });
}

/**
 * Wire a lyrics list to the user.
 *
 * The list moves when the song advances and when the user scrolls it,
 * and the two have to be told apart -- otherwise every line change
 * would look like the user taking over and the panel would stop
 * following the song.
 */
function bindLyricsList(box, refocus) {
    if (!box) return;
    lyricsList(box).refocus = refocus;
    // A wheel or a finger is the user driving, whether or not the list
    // can still move.
    ['wheel', 'touchmove'].forEach((name) => {
        box.addEventListener(name, () => takeOverLyrics(box));
    });
    box.addEventListener('scroll', () => {
        // Scrolls the panel made itself (see centerLyricLine) are not
        // the user taking over.
        if (Date.now() < lyricsList(box).autoScrollUntil) return;
        takeOverLyrics(box);
    });
}

/**
 * Scroll `box` so that `line` sits in the middle of it.
 *
 * Measured with rectangles rather than `offsetTop`: the latter is relative
 * to the nearest positioned ancestor, which used to be `.lyrics-panel`
 * (the header sits inside it), so every scroll overshot by the header
 * height and the active line landed above centre.
 */
function centerLyricLine(box, line, smooth) {
    if (!box || !line) return;
    const boxRect = box.getBoundingClientRect();
    const lineRect = line.getBoundingClientRect();
    const height = line.offsetHeight || lineRect.height;
    const wanted = box.scrollTop + (lineRect.top - boxRect.top)
                 - (box.clientHeight - height) / 2;
    const limit = Math.max(0, box.scrollHeight - box.clientHeight);
    const top = Math.max(0, Math.min(wanted, limit));
    if (!smooth) {
        markLyricsAutoScroll(box);
        box.scrollTo({ top, behavior: 'auto' });
        return;
    }
    smoothScrollTo(box, top);
}

let lyricScrollTimer = null;

/**
 * Glide the lyrics list to `top` ourselves.
 *
 * The browser's own `behavior: 'smooth'` is not dependable: it is skipped
 * under `prefers-reduced-motion`, it does nothing in a headless webview,
 * and it silently gives up on some compositor hiccups -- in which case the
 * active line stays wherever it was.  Driving the animation from a timer
 * with `scrollTo({behavior: 'auto'})` lands on the exact target on every
 * platform, and a new target cancels the previous glide.
 */
function smoothScrollTo(box, top) {
    if (lyricScrollTimer) {
        clearInterval(lyricScrollTimer);
        lyricScrollTimer = null;
    }
    const from = box.scrollTop;
    if (Math.abs(from - top) < 1) return;
    const duration = 240;
    const started = Date.now();
    const step = () => {
        // The user scrolled while we were gliding: their scroll wins.
        if (!lyricsList(box).following) {
            clearInterval(lyricScrollTimer);
            lyricScrollTimer = null;
            return;
        }
        const progress = Math.min(1, (Date.now() - started) / duration);
        // easeOutCubic: quick start, gentle landing.
        const eased = 1 - Math.pow(1 - progress, 3);
        markLyricsAutoScroll(box);
        box.scrollTo({ top: from + (top - from) * eased, behavior: 'auto' });
        if (progress >= 1) {
            clearInterval(lyricScrollTimer);
            lyricScrollTimer = null;
        }
    };
    lyricScrollTimer = setInterval(step, 16);
}

/**
 * Show a message (loading / no lyrics) centred in the lyrics area.
 *
 * The `is-empty` class swaps the scroll list for a centred flex box: with
 * the old padding the message was positioned at 40% of the width, which on
 * a short panel is below the visible area -- the usual "暂无歌词 is
 * invisible" complaint.
 */
function setLyricsMessage(box, text) {
    if (!box) return;
    box.classList.add('is-empty');
    box.innerHTML = '';
    const message = document.createElement('p');
    message.className = 'empty';
    message.textContent = text;
    box.appendChild(message);
}

/** Drop the centred-message state once real lyrics are rendered. */
function clearLyricsMessage(box) {
    if (box) box.classList.remove('is-empty');
}

/** The index of the line that is playing at `ms`. */
function lyricIndexAt(ms) {
    let low = 0;
    let high = player.lyrics.length - 1;
    let found = -1;
    while (low <= high) {
        const mid = (low + high) >> 1;
        if (player.lyrics[mid].time <= ms) { found = mid; low = mid + 1; }
        else high = mid - 1;
    }
    return found;
}

function renderLyricLines() {
    const box = document.getElementById('lyrics-scroll');
    if (!box) return;
    clearLyricsMessage(box);
    box.innerHTML = '';
    // The old scroll position died with the old DOM, so the list starts
    // following the playing line again.
    resetLyricsList(box);
    // The DOM is about to be rebuilt, so the remembered index no longer
    // describes anything.  Leaving it in place made `updateLyricHighlight`
    // early-return (`found === player.lyricIndex`) and highlight *nothing*
    // until the next lyric line advanced -- most visible when toggling
    // dual-line mode mid-song.
    player.lyricIndex = -1;
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
            originalLine.textContent = line.text || '';
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
            element.textContent = line.text || '';
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
    syncLyricsPadding(box);
    // Mobile mirror: the npm view renders the same lyrics in a separate
    // container; rebind whenever the desktop panel re-renders.
    renderMobileLyrics();
    // Put the highlight back straight away.  Rebuilding the DOM left
    // `lyricIndex` at -1 and the scroll at the top, and nothing restored
    // them until the next `timeupdate` -- so switching to dual-line (or
    // karaoke) while paused looked broken: no active line, list at the top.
    player.lyricIndex = -1;
    updateLyricHighlight(true);
}

/** A CJK / kana / hangul character: these get one span per character. */
const KARAOKE_CJK = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]/;

/**
 * Split a lyric line into the pieces karaoke lights up one at a time.
 *
 * Chinese and Japanese lyrics want one span per character; English lyrics
 * want whole words (one span per letter would look like noise and would
 * break wrapping).  Whitespace becomes its own token so the spacing
 * survives, but it never counts as a sung step on its own.
 */
function karaokeTokens(text) {
    const tokens = [];
    let word = '';
    for (const character of String(text || '')) {
        if (KARAOKE_CJK.test(character)) {
            if (word) { tokens.push(word); word = ''; }
            tokens.push(character);
        } else if (/\s/.test(character)) {
            if (word) { tokens.push(word); word = ''; }
            tokens.push(character);
        } else {
            word += character;
        }
    }
    if (word) tokens.push(word);
    return tokens;
}

/**
 * Build the karaoke rows.
 *
 * The lyrics API only gives a start time per line, so each line is treated
 * as running until the next one starts (4 s for the last) and the
 * characters are revealed evenly across that span.  It is an
 * approximation, but it is what makes 逐字 highlighting work at all --
 * the previous implementation only added a class and let CSS fill the
 * whole line, which is a block highlight, not a progress one.
 */
function renderKaraokeLines(fragment) {
    const showTranslation = (state.settings && state.settings.playback.show_translation) !== false;
    player.lyrics.forEach((line, index) => {
        const element = document.createElement('div');
        element.className = 'lyric-line lyric-karaoke';
        element.dataset.time = String(line.time);
        const start = Number(line.time) || 0;
        const next = player.lyrics[index + 1];
        const end = next ? Number(next.time) || start : start + 4000;
        element.dataset.start = String(start);
        element.dataset.end = String(end);

        const body = document.createElement('span');
        body.className = 'karaoke-text';
        karaokeTokens(line.text).forEach((token) => {
            const span = document.createElement('span');
            span.className = 'karaoke-char';
            span.textContent = token;
            body.appendChild(span);
        });
        element.appendChild(body);

        if (showTranslation && line.translation) {
            const translation = document.createElement('span');
            translation.className = 'lyric-translation';
            translation.textContent = line.translation;
            element.appendChild(translation);
        }
        fragment.appendChild(element);
    });
}

/**
 * Light up the characters of the active karaoke line up to `ms`.
 *
 * Called on every playback tick, not just when the line changes.
 */
function updateKaraokeProgress(box, index, ms) {
    const lines = box.querySelectorAll('.lyric-line.lyric-karaoke');
    const line = lines[index];
    if (!line) return;
    const chars = line.querySelectorAll('.karaoke-char');
    if (!chars.length) return;
    const start = Number(line.dataset.start) || 0;
    const end = Number(line.dataset.end) || start;
    const span = Math.max(1, end - start);
    const progress = Math.max(0, Math.min(1, (ms - start) / span));
    // Spaces are spans too, but lighting them up should not eat the line's
    // time: count only the visible characters, then mark everything up to
    // and including the last lit one (so the spaces in between follow).
    const visible = [];
    for (let i = 0; i < chars.length; i += 1) {
        if (chars[i].textContent.trim()) visible.push(i);
    }
    if (!visible.length) return;
    const lit = Math.round(progress * visible.length);
    const cutoff = lit > 0 ? visible[Math.min(lit, visible.length) - 1] : -1;
    for (let i = 0; i < chars.length; i += 1) {
        chars[i].classList.toggle('is-sung', i <= cutoff);
    }
}

function updateLyricsScrollIdleLabel() {
    const label = document.getElementById('lyrics-scroll-idle-value');
    const slider = document.getElementById('lyrics-scroll-idle');
    if (label && slider) {
        label.textContent = t('player.seconds', { seconds: parseInt(slider.value, 10) });
    }
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
    const linesContainer = document.getElementById('lyrics-editor-lines');
    if (!timeline || !linesContainer) return;
    timeline.innerHTML = '';
    linesContainer.innerHTML = '';
    
    if (!audio.duration) return;
    
    const duration = audio.duration;
    const pixelsPerSecond = timeline.clientWidth / duration;
    
    player.lyrics.forEach((line, index) => {
        // Timeline marker
        const marker = document.createElement('div');
        marker.className = 'lyrics-editor-marker';
        marker.style.left = (line.time / 1000) * pixelsPerSecond + 'px';
        marker.dataset.index = index;
        marker.title = fmtTime(line.time / 1000) + ' - ' + (line.text || '');
        timeline.appendChild(marker);
        
        // Editable lyrics line
        const lineEl = document.createElement('div');
        lineEl.className = 'lyrics-editor-line';
        lineEl.dataset.index = index;
        
        const timeInput = document.createElement('input');
        timeInput.type = 'number';
        timeInput.step = '1';
        timeInput.min = '0';
        timeInput.max = Math.floor(audio.duration * 1000);
        timeInput.value = line.time;
        timeInput.title = '时间 (毫秒)';
        
        const textInput = document.createElement('input');
        textInput.type = 'text';
        textInput.value = line.text || '';
        textInput.placeholder = '歌词文本';
        textInput.style.flex = '1';
        
        const translationInput = document.createElement('input');
        translationInput.type = 'text';
        translationInput.value = line.translation || '';
        translationInput.placeholder = '翻译 (可选)';
        translationInput.style.width = '200px';
        
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'btn btn-icon btn-sm';
        deleteBtn.type = 'button';
        setIcon(deleteBtn, 'trash');
        deleteBtn.title = '删除';
        deleteBtn.addEventListener('click', () => {
            player.lyrics.splice(index, 1);
            buildLyricsEditorTimeline();
        });
        
        lineEl.append(timeInput, textInput, translationInput, deleteBtn);
        linesContainer.appendChild(lineEl);
    });
    
    // Add new line button
    const addBtn = document.createElement('button');
    addBtn.className = 'btn btn-sm btn-secondary';
    addBtn.type = 'button';
    addBtn.textContent = '+ 添加歌词行';
    addBtn.addEventListener('click', () => {
        const newLine = { time: Math.floor(audio.currentTime * 1000), text: '', translation: '' };
        player.lyrics.push(newLine);
        buildLyricsEditorTimeline();
    });
    linesContainer.appendChild(addBtn);
    
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

function updateLyricHighlight(force) {
    if (!player.lyrics.length) return;
    // Apply the per-track offset (-5000 .. +5000 ms) so the slider works in
    // real time instead of forcing a fresh network lookup on every nudge.
    const ms = audio.currentTime * 1000 - (player.lyricOffset || 0);
    const found = lyricIndexAt(ms);
    const box = document.getElementById('lyrics-scroll');
    if (!box) return;
    // Karaoke paints inside the active line, so it has to advance on every
    // tick -- the index alone is not enough (it changes once per line).
    if (player.karaoke) updateKaraokeProgress(box, found, ms);
    if (!force && found === player.lyricIndex) return;
    // Dual-line mode puts two .lyric-line nodes (original + translation) into
    // every row, so the flat node list is longer than player.lyrics and the
    // index no longer points at the right row.  Highlight the row wrapper
    // there and the plain line everywhere else.
    // Dual mode must be detected exactly as `renderLyricLines` does it --
    // gating on `player.dualLine` alone disagrees with the renderer whenever
    // `show_translation` is off, and then the wrapper list does not exist.
    const showTranslation = (state.settings && state.settings.playback.show_translation) !== false;
    const dual = player.dualLine && showTranslation;
    const previous = box.querySelector(dual
        ? '.lyric-line-wrapper.is-active'
        : '.lyric-line.is-active');
    if (previous) previous.classList.remove('is-active');
    player.lyricIndex = found;
    const list = box.querySelectorAll(dual ? '.lyric-line-wrapper' : '.lyric-line');
    const target = list[found];
    if (!target) return;
    target.classList.add('is-active');
    syncLyricsPadding(box);
    if (lyricsList(box).following) centerLyricLine(box, target, true);
    // Mobile mirror: the full-screen now-playing view has its own lyrics
    // container; share the same found index.
    updateMobileLyricHighlight();
    // Keep the desktop overlay on the same line as the main panel.
    pushLyricsToDesktop();
}

/**
 * Fill an element with the track's artist names, each one opening that
 * artist's page.
 *
 * The ids arrive from the bridge as ``artist_list``.  A track without them --
 * a local file, a queue entry written by an older version -- falls back to
 * plain text, so the panel never shows an empty artist line.
 */
function renderArtistLinks(el, track) {
    if (!el) return;
    el.textContent = '';
    const list = (track && track.artist_list) || [];
    const usable = list.filter(item => item && item.id && item.name);
    if (!usable.length) {
        el.textContent = (track && track.artists) || '';
        return;
    }
    usable.forEach((item, index) => {
        if (index) el.appendChild(document.createTextNode(', '));
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'artist-link';
        button.textContent = item.name;
        button.title = t('player.artist_detail');
        button.addEventListener('click', (event) => {
            // The now-playing panel is inside clickable rows; without this
            // the click would also start the track again.
            event.stopPropagation();
            openArtistDetail({ id: item.id, name: item.name });
        });
        el.appendChild(button);
    });
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
        renderNowPlayingMobile();
        return;
    }

    if (title) title.textContent = track.name || '-';
    renderArtistLinks(artist, track);
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
    if (barArtist) renderArtistLinks(barArtist, track);
    updateFavoriteButton();
    renderNowPlayingMobile();
}

function updateFavoriteButton() {
    const button = document.getElementById('pb-fav');
    if (!button) return;
    const track = player.current;
    if (!track) {
        setIcon(button, 'heart');
        button.classList.remove('fav-on');
        return;
    }
    const favorite = player.favoriteKeys.has(trackKeyOf(track));
    setIcon(button, 'heart', { filled: favorite });
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
                setIcon(idx, 'music');
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
    setIcon(button, playing ? 'pause' : 'play');
    const label = playing ? t('player.pause') : t('player.play');
    button.title = label;
    button.setAttribute('aria-label', label);
    updateMobileControlStates();
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
    updateMobileControlStates();
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

/**
 * Schedule the next session snapshot.  Each call replaces the pending
 * timeout, so a steady stream of `timeupdate` events collapses into a
 * single 5s-delayed save (matching the previous setInterval cadence) with
 * no wakeup while playback is paused.  This keeps memory flat when the
 * app sits idle with the window in the background.
 */
function scheduleSessionSave() {
    if (player._sessionSaveTimer) clearTimeout(player._sessionSaveTimer);
    player._sessionSaveTimer = setTimeout(() => {
        player._sessionSaveTimer = null;
        saveSession(false);
    }, 5000);
}

function startSessionTimer() {
    // Persist on tab close -- still the cheapest moment to capture the
    // last position; the in-window persistence now happens on timeupdate.
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
        idx.dataset.number = String(index + 1);
        if (index === player.index) setIcon(idx, 'music');
        else idx.textContent = idx.dataset.number;

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
        setIcon(playBtn, 'play');
        playBtn.title = t('player.play');
        playBtn.addEventListener('click', (e) => { e.stopPropagation(); playAt(index, { autoplay: true }); });
        actions.appendChild(playBtn);

        const moveTopBtn = document.createElement('button');
        moveTopBtn.className = 'btn btn-icon';
        moveTopBtn.type = 'button';
        setIcon(moveTopBtn, 'arrow-up');
        moveTopBtn.title = t('player.queue_move_top');
        moveTopBtn.addEventListener('click', (e) => { e.stopPropagation(); moveQueueItem(index, 0); });
        actions.appendChild(moveTopBtn);

        const moveBottomBtn = document.createElement('button');
        moveBottomBtn.className = 'btn btn-icon';
        moveBottomBtn.type = 'button';
        setIcon(moveBottomBtn, 'arrow-down');
        moveBottomBtn.title = t('player.queue_move_bottom');
        moveBottomBtn.addEventListener('click', (e) => { e.stopPropagation(); moveQueueItem(index, player.queue.length - 1); });
        actions.appendChild(moveBottomBtn);

        const removeBtn = document.createElement('button');
        removeBtn.className = 'btn btn-icon';
        removeBtn.type = 'button';
        setIcon(removeBtn, 'close');
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
    // In the packaged app the overlay window lives in the Python bridge, not
    // in this window's JS context: `player.desktopLyricsWindow` is only set
    // in the browser fallback.  Asking the bridge is the only way to know
    // whether the overlay is open -- and the only way to close it.
    if (state.api && state.api.desktop_lyrics_open) {
        state.api.desktop_lyrics_open()
            .then((status) => {
                if (status && status.open) closeDesktopLyrics();
                else openDesktopLyrics();
            })
            .catch(() => openDesktopLyrics());
        return;
    }
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
    url.searchParams.set('background', state.settings?.ui?.desktop_lyrics_background || 'dark');
    url.searchParams.set('textStyle', state.settings?.ui?.desktop_lyrics_text_style || 'white');

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
        setTimeout(() => pushLyricsToDesktop(true), 500);
    }
}

function closeDesktopLyrics() {
    // Close the native window through the bridge; the browser fallback
    // keeps its own handle.
    if (state.api && state.api.close_desktop_lyrics) {
        state.api.close_desktop_lyrics().catch(() => { /* ignore */ });
    }
    if (player.desktopLyricsWindow && !player.desktopLyricsWindow.closed) {
        player.desktopLyricsWindow.close();
    }
    player.desktopLyricsWindow = null;
}

let pushLyricsToDesktopDebounce = 0;

/**
 * Push the lyrics and the active line to the desktop overlay.
 *
 * @param {boolean} [force] send the whole lyrics array even when the
 *        debounce would swallow the call (a new set of lines, or a window
 *        that was just opened).
 */
function pushLyricsToDesktop(force) {
    // Debounce to ~30fps.  Anything tighter than ~33 ms is invisible on the
    // 60 Hz (or lower) refresh of the desktop window.  A forced push skips
    // the debounce: it carries a *new* set of lines, which the debounce
    // would otherwise drop and leave the overlay showing the old song.
    const now = Date.now();
    if (!force && now - pushLyricsToDesktopDebounce < 33) {
        return;
    }
    pushLyricsToDesktopDebounce = now;

    if (!state.api || !state.api.update_desktop_lyrics) return;
    // The backend now evaluates the payload directly on the desktop window
    // (see src/gui/bridge.py::update_desktop_lyrics); we just hand it the
    // current lyrics + active index.  The earlier `postMessage` and
    // `localStorage` fallbacks were dead code in pywebview (each window has
    // its own JS context; `window.opener` is null and localStorage is not
    // shared between windows).
    state.api.update_desktop_lyrics(player.lyrics || [], player.lyricIndex)
        .catch(() => { /* ignore */ });
}

// NOTE: there used to be duplicate definitions of `onOpenDesktopLyrics`,
// `onCloseDesktopLyrics`, and `onDesktopLyricsUpdate` in this section --
// they conflicted with the canonical versions defined further down and
// were the root cause of the desktop lyrics window never receiving data.
// The backend now calls `handleLyricsUpdate` directly on the desktop
// window, so the main window does not need a push handler at all.

/* ----------------------------------------------------------- Crossfade */

/**
 * Lazily create the second audio element used for crossfade.  Most users
 * leave crossfade off, so paying for a second <audio> plus its decoder
 * state would be a steady ~30 MB waste for nothing.  The element is built
 * on demand and removed when crossfade is disabled again.
 */
function ensureCrossfadeAudio() {
    if (player.crossfadeAudio) return;
    const second = document.createElement('audio');
    second.preload = 'metadata';
    second.style.display = 'none';
    document.body.appendChild(second);
    player.crossfadeAudio = second;
}

function dropCrossfadeAudio() {
    if (!player.crossfadeAudio) return;
    try {
        player.crossfadeAudio.pause();
        player.crossfadeAudio.removeAttribute('src');
        player.crossfadeAudio.load();
        player.crossfadeAudio.remove();
    } catch (err) { /* ignore */ }
    player.crossfadeAudio = null;
}

function initCrossfade() {
    // Load crossfade duration from settings; only spin up the second audio
    // element when the feature is actually enabled.
    player.crossfadeDuration = state.settings?.playback?.crossfade_duration || 0;
    if (player.crossfadeDuration > 0) ensureCrossfadeAudio();
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
    // The user just enabled crossfade -- build the second <audio> on demand.
    ensureCrossfadeAudio();
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

    // The local must not be called ``nextIndex``: that is the function being
    // called here, and a ``const`` of the same name shadows it for the whole
    // block, so the call hits the temporal dead zone and throws.
    const upcoming = nextIndex(true);
    if (upcoming < 0 || upcoming === player.index) return;

    const nextTrack = player.queue[upcoming];
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
        // The lock screen / control center must be given a synchronous URL,
        // so prefer the loopback ``/art`` (local files) or the loopback
        // ``/cover`` proxy already cached for this URL.  Falling back to the
        // raw CDN URL keeps the artwork working on first play (when the
        // proxy has not answered yet) -- the cache fills in next time.
        artwork: collectArtworkUrls(track, resolved).map(url => ({
            src: url,
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

function onDesktopLyricsWindowReady() {
    // Called from the bridge immediately after `open_desktop_lyrics` creates
    // a new pywebview window.  Pushing once here seeds the desktop with the
    // current lyrics (if any); from then on `updateLyricHighlight` updates
    // keep it in sync via `pushLyricsToDesktop`.
    pushLyricsToDesktop(true);
}

function openDesktopLyricsFallback() {
    // Dev-only: when pywebview is not available the bridge asks the main
    // window to use `window.open` instead.  We just call the same entry
    // point the toolbar button uses; the popup then receives lyrics via
    // postMessage because every window shares the same JS context here.
    openDesktopLyrics();
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
    bindSectionEvents();
    // top level tabs
    document.getElementById('top-tabs').addEventListener('click', (event) => {
        const tab = event.target.closest('.top-tab');
        if (!tab) return;
        showView(tab.dataset.view);
        // The section picker refreshes its counters every time it is opened.
        if (tab.dataset.view === 'home') showHome();
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
        refreshCacheStats();
    });
    document.getElementById('btn-theme').addEventListener('click', cycleTheme);

    // cache
    document.getElementById('btn-clear-audio')
        .addEventListener('click', () => clearCache('audio'));
    document.getElementById('btn-clear-cover')
        .addEventListener('click', () => clearCache('cover'));
    document.getElementById('btn-clear-all')
        .addEventListener('click', () => clearCache('all'));
    document.getElementById('btn-force-refresh')
        .addEventListener('click', () => forceRefresh());

    // account panel
    document.getElementById('btn-account-close')
        .addEventListener('click', () => closeModal('account-modal'));
    document.getElementById('btn-account-refresh')
        .addEventListener('click', () => openAccountPanel(true));
    document.getElementById('btn-account-logout')
        .addEventListener('click', async () => {
            if (!await confirmDialog('login.logout_confirm')) return;
            await state.api.logout();
            player.playlists = [];
            closeModal('account-modal');
            await refreshLoginStatus();
        });

    // update notice
    document.getElementById('btn-update-dismiss')
        .addEventListener('click', dismissUpdateNotice);

    // disclaimer
    document.getElementById('disclaimer-agree')
        .addEventListener('change', (event) => setDisclaimerAgree(event.target.checked));
    document.getElementById('btn-disclaimer-accept')
        .addEventListener('click', acceptDisclaimer);
    document.getElementById('btn-disclaimer-quit')
        .addEventListener('click', quitApp);

    // login
    document.getElementById('btn-refresh-qrcode').addEventListener('click', refreshQrcode);
    document.getElementById('btn-send-code').addEventListener('click', sendPhoneCode);
    document.getElementById('btn-phone-login').addEventListener('click', doPhoneLogin);
    document.getElementById('btn-register-check').addEventListener('click', checkRegisterPhone);
    document.getElementById('btn-register-send-code').addEventListener('click', sendRegisterCode);
    document.getElementById('btn-register-submit').addEventListener('click', doRegister);
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

    // account backup
    async function showCookieResult(result, message) {
        const output = document.getElementById('cookie-output');
        if (output && result && result.cookie) {
            output.value = result.cookie;
            output.hidden = false;
        }
        setBackupStatus(message);
    }

    async function exportLoginCookie() {
        const button = document.getElementById('btn-cookie-export');
        if (button) button.disabled = true;
        try {
            const result = await state.api.export_login_cookie();
            if (!result || result.cancelled) return;
            if (!result.success) {
                setBackupStatus(t(result.error_key || 'backup.cookie_failed'));
                return;
            }
            await showCookieResult(result, t('backup.cookie_exported') + ' ' + (result.path || ''));
        } catch (err) {
            setBackupStatus(t('backup.cookie_failed'));
        } finally {
            if (button) button.disabled = false;
        }
    }

    async function copyLoginCookie() {
        const button = document.getElementById('btn-cookie-copy');
        if (button) button.disabled = true;
        try {
            const result = await state.api.copy_login_cookie();
            if (!result) return;
            if (!result.success) {
                setBackupStatus(t(result.error_key || 'backup.cookie_failed'));
                return;
            }
            await showCookieResult(result, t(result.copied ? 'backup.cookie_copied' : 'backup.cookie_copy_manual'));
        } catch (err) {
            setBackupStatus(t('backup.cookie_failed'));
        } finally {
            if (button) button.disabled = false;
        }
    }

    document.getElementById('btn-backup-export').addEventListener('click', exportAccountBackup);
    document.getElementById('btn-cookie-export').addEventListener('click', exportLoginCookie);
    document.getElementById('btn-cookie-copy').addEventListener('click', copyLoginCookie);
    document.getElementById('btn-backup-import').addEventListener('click', importAccountBackup);
    document.getElementById('btn-backup-conflict-apply')
        .addEventListener('click', applyBackupConflicts);
    document.getElementById('btn-backup-conflict-cancel')
        .addEventListener('click', () => closeModal('backup-conflict-modal'));
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
        else if (player.sourceTab === 'followed_artists') loadFollowedArtists(true);
        else if (player.sourceTab === 'toplist') { /* top lists don't paginate */ }
        else doSearch(true);
    });
    document.getElementById('btn-list-back').addEventListener('click', backToPlaylists);
    // Re-read the list on screen, skipping every cache.  This is the way
    // out when a playlist was changed on another device: the caches are
    // there to spare the server, not to hide a change the user can see
    // on their phone.
    document.getElementById('btn-refresh-list')
        .addEventListener('click', refreshCurrentSource);
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

    // desktop lyrics settings -- the four sliders/inputs push their value to
    // the open desktop window via the bridge.  In pywebview the main window
    // does not hold a reference to the desktop window (it lives in the
    // Python bridge), so the previous `player.desktopLyricsWindow.postMessage`
    // call was always a no-op for the packaged build.
    const pushDesktopSettings = (settings) => {
        if (state.api && state.api.update_desktop_lyrics_settings) {
            state.api.update_desktop_lyrics_settings(settings).catch(() => { /* ignore */ });
        }
    };
    document.getElementById('desktop-lyrics-font-size').addEventListener('input', (event) => {
        pushDesktopSettings({ fontSize: parseInt(event.target.value, 10) });
    });
    document.getElementById('desktop-lyrics-color').addEventListener('input', (event) => {
        pushDesktopSettings({ color: event.target.value });
    });
    document.getElementById('desktop-lyrics-opacity').addEventListener('input', (event) => {
        updateDesktopLyricsOpacityLabel();
        pushDesktopSettings({ opacity: parseFloat(event.target.value) });
    });
    document.getElementById('desktop-lyrics-background').addEventListener('change', (event) => {
        pushDesktopSettings({ background: event.target.value });
    });
    document.getElementById('desktop-lyrics-text-style').addEventListener('change', (event) => {
        pushDesktopSettings({ textStyle: event.target.value });
    });
    document.getElementById('desktop-lyrics-translation-check').addEventListener('change', (event) => {
        pushDesktopSettings({ showTranslation: event.target.checked });
    });

    document.getElementById('lyrics-offset').addEventListener('input', (event) => {
        const val = parseInt(event.target.value, 10);
        // Real-time local offset: just nudge `lyricOffset`, the next
        // timeupdate picks it up via `updateLyricHighlight`.  The old code
        // re-fetched lyrics from the network on every slider tick, which was
        // both slow and wrong (lyrics_offset is a *playback* offset, not a
        // request parameter).
        player.lyricOffset = val;
        updateLyricsOffsetLabel();
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

    // lyrics scroll idle time: how long the panel waits before it
    // returns to the line that is playing.
    document.getElementById('lyrics-scroll-idle').addEventListener('input', (event) => {
        player.lyricsScrollIdleSeconds = parseInt(event.target.value, 10);
        rearmLyricsReturn();
        updateLyricsScrollIdleLabel();
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

    // lyrics: the panel follows the playing line until the user scrolls
    // it somewhere else, and comes back on its own once they stop.
    bindLyricsList(document.getElementById('lyrics-scroll'), () => updateLyricHighlight(true));
    bindLyricsList(document.getElementById('npm-lyrics'), () => updateMobileLyricHighlight());

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
            if (!open) {
                // No dialog is open: Esc walks back out of a sub page.
                const tag = (event.target.tagName || '').toLowerCase();
                if (tag !== 'input' && tag !== 'textarea' && tag !== 'select'
                    && sectionIsActive('search')) {
                    backToHome();
                } else if (tag !== 'input' && tag !== 'textarea' && tag !== 'select'
                    && sectionIsActive('bili')) {
                    backToHome();
                }
                return;
            }
            if (open.id === 'confirm-modal') resolveConfirm(false);
            else closeModal(open.id);
            return;
        }
        const tag = (event.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
        // Alt+Left (Windows) / Cmd+[ (macOS) leave a sub page as well.
        if (sectionIsActive('search') || sectionIsActive('bili')) {
            if ((event.altKey && event.key === 'ArrowLeft')
                || (event.metaKey && event.key === '[')) {
                event.preventDefault();
                backToHome();
                return;
            }
        }
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
        // Session persistence is now driven by playback: each `timeupdate`
        // schedules (debounced) a save, so paused tracks do not write.
        if (!audio.paused) scheduleSessionSave();
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
    // The heart state now includes the account's NetEase favourites, which
    // only exist once somebody is signed in.
    loadPlayerExtras();
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

/** Progress of the account backup export / import. */
function onAccountBackupProgress(payload) {
    if (!payload) return;
    const total = payload.total || 0;
    const current = payload.current || 0;
    if (!total) {
        setBackupProgress(0, 0);
        return;
    }
    setBackupProgress(current, total);
    setBackupStatus(`${backupPhaseText(payload.phase)} ${current}/${total}`);
}

/* ==================================================================== */
/* home · combined search · bilibili sections                            */
/* ==================================================================== */

/** Partition key -> the i18n key of its chip label. */
const BILI_CHIP_KEYS = {
    all: 'bili.chip_all',
    animation: 'bili.chip_animation',
    bangumi: 'bili.chip_bangumi',
    music: 'bili.chip_music',
    music_video: 'bili.chip_music_video',
    douga: 'bili.chip_douga',
    knowledge: 'bili.chip_knowledge',
    game: 'bili.chip_game',
    life: 'bili.chip_life',
    film: 'bili.chip_film',
    documentary: 'bili.chip_documentary'
};

/** The combined search: what is loaded, and what the filters say. */
const combined = {
    keyword: '',
    source: 'all',
    category: 'all',
    sort: 'relevance',
    duration: 'all',
    rows: [],
    neteaseOffset: 0,
    biliPage: 1,
    neteaseDone: false,
    biliDone: false,
    neteaseError: '',
    biliError: '',
    loading: false,
    requestId: 0
};

/** The Bilibili section: the partition that is open and what it holds. */
const biliSection = {
    rid: 0,
    page: 1,
    items: [],
    partitions: [],
    error: '',
    loading: false,
    exhausted: false
};

/** The home card the user came from, so the way back lands on it. */
let homeReturnEntry = '';

/* ---------------------------------------------------------- small helpers */

/** Whether one of the three new views is the visible one. */
function sectionIsActive(name) {
    const view = document.getElementById('view-' + name);
    return !!(view && view.classList.contains('is-active'));
}

/** The offline bars are per view, so they are updated together. */
function updateOfflineBars() {
    const offline = navigator.onLine === false;
    ['home-offline', 'search-offline', 'bili-offline'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.hidden = !offline;
    });
}

/** "1.0" -> "1", "1.5" -> "1.5" (the trailing zero is noise in a count). */
function trimDecimal(text) {
    const parts = String(text).split('.');
    if (parts.length > 1 && parts[1] === '0') return parts[0];
    return String(text);
}

/** 12345 -> "1.2万" (or "12.3K" in English). */
function formatPlayCount(value) {
    const number = Number(value) || 0;
    const english = !!(state.settings && state.settings.ui
        && state.settings.ui.language === 'en_us');
    if (english) {
        if (number >= 1000000) return trimDecimal((number / 1000000).toFixed(1)) + 'M';
        if (number >= 1000) return trimDecimal((number / 1000).toFixed(1)) + 'K';
        return String(number);
    }
    if (number >= 100000000) return trimDecimal((number / 100000000).toFixed(1)) + '亿';
    if (number >= 10000) return trimDecimal((number / 10000).toFixed(1)) + '万';
    return String(number);
}

/** A block with an icon, a line of text and (optionally) one button. */
function buildStateBlock(iconName, title, hint, actionLabel, action) {
    const block = document.createElement('div');
    block.className = 'state-block';
    const icon = document.createElement('div');
    icon.className = 'state-icon';
    icon.setAttribute('data-icon', iconName);
    setIcon(icon, iconName);
    block.appendChild(icon);
    const heading = document.createElement('div');
    heading.className = 'state-title';
    heading.textContent = title;
    block.appendChild(heading);
    if (hint) {
        const note = document.createElement('div');
        note.className = 'state-hint';
        note.textContent = hint;
        block.appendChild(note);
    }
    if (actionLabel && action) {
        const button = document.createElement('button');
        button.className = 'btn btn-sm btn-secondary';
        button.type = 'button';
        button.textContent = actionLabel;
        button.addEventListener('click', action);
        block.appendChild(button);
    }
    return block;
}

/* ------------------------------------------------------------------- home */

/** Show the section picker and refresh its counters. */
function showHome() {
    updateOfflineBars();
    updateHomeCounts();
}

/**
 * The counters under the home cards.
 *
 * A card without a number is still a card: a failing count hides its line
 * instead of putting an error on the page.
 */
async function updateHomeCounts() {
    const neteaseLine = document.getElementById('home-count-netease');
    const biliLine = document.getElementById('home-count-bili');
    const foot = document.getElementById('home-foot');
    if (neteaseLine) {
        neteaseLine.textContent = '--';
        neteaseLine.classList.remove('is-empty');
    }
    if (biliLine) {
        biliLine.textContent = '--';
        biliLine.classList.remove('is-empty');
    }
    if (foot) foot.hidden = true;
    let failed = false;

    if (!state.loggedIn) {
        if (neteaseLine) neteaseLine.textContent = t('home.count_signed_out');
    } else {
        try {
            const result = await state.api.get_playlists(false);
            if (result && result.success) {
                const count = (result.playlists || []).length;
                if (neteaseLine) neteaseLine.textContent = t('home.count_playlists', { count });
            } else {
                failed = true;
                if (neteaseLine) neteaseLine.classList.add('is-empty');
            }
        } catch (err) {
            failed = true;
            if (neteaseLine) neteaseLine.classList.add('is-empty');
        }
    }

    try {
        if (!bridgeHas('get_bilibili_popular')) {
            failed = true;
            if (biliLine) biliLine.classList.add('is-empty');
            if (foot) foot.hidden = false;
            return;
        }
        const result = await state.api.get_bilibili_popular(0, 1, 50);
        if (result && result.success) {
            const count = (result.tracks || result.items || []).length;
            if (biliLine) biliLine.textContent = t('home.count_trending', { count });
        } else {
            failed = true;
            if (biliLine) biliLine.classList.add('is-empty');
        }
    } catch (err) {
        failed = true;
        if (biliLine) biliLine.classList.add('is-empty');
    }

    if (failed && foot) foot.hidden = false;
}

/** Mark the card the user came from, so the way back is obvious. */
function markHomeEntry(entry) {
    document.querySelectorAll('.home-card').forEach((card) => {
        const current = card.dataset.entry === entry;
        card.classList.toggle('is-current', current);
        if (current) card.setAttribute('aria-current', 'true');
        else card.removeAttribute('aria-current');
    });
}

/** Enter one of the four home cards. */
function openHomeEntry(entry, byKeyboard) {
    homeReturnEntry = entry;
    markHomeEntry(entry);
    if (entry === 'netease') {
        player.sourceTab = 'playlists';
        showView('player');
        return;
    }
    if (entry === 'bili') {
        showSection('bili');
        renderBili(0);
        focusPageTitle('bili-title');
        return;
    }
    if (entry === 'search') {
        showSection('search');
        if (byKeyboard) {
            const input = document.getElementById('aggr-input');
            if (input) input.focus();
        } else {
            focusPageTitle('search-title');
        }
        return;
    }
    if (entry === 'settings') {
        openModal('settings-modal');
        updateLocalStatsText();
        updatePlayStatsText();
        refreshCacheStats();
    }
}

/**
 * Put the focus on the title of the page that was just opened.
 *
 * The keyboard user must not lose the place: without this the focus stays
 * on the home card that is now hidden.
 */
function focusPageTitle(id) {
    const title = document.getElementById(id);
    if (title) title.focus();
}

/** Show a sub view; the "home" tab stays lit because it is its parent. */
function showSection(name) {
    document.querySelectorAll('.view').forEach((view) => {
        view.classList.toggle('is-active', view.id === 'view-' + name);
    });
    document.querySelectorAll('#top-tabs .top-tab').forEach((tab) => {
        tab.classList.toggle('is-active', tab.dataset.view === 'home');
    });
    updateOfflineBars();
}

/** Back to the section picker, with the focus on the card we left. */
function backToHome() {
    showView('home');
    const card = homeReturnEntry
        ? document.querySelector('.home-card[data-entry="' + homeReturnEntry + '"]')
        : null;
    if (card) card.focus();
}

/* -------------------------------------------------------- combined search */

/** The four filters, read from the form. */
function combinedFilters() {
    const category = document.getElementById('aggr-category');
    const sort = document.getElementById('aggr-sort');
    const duration = document.getElementById('aggr-duration');
    return {
        source: combinedSourceValue(),
        category: category ? category.value : 'all',
        sort: sort ? sort.value : 'relevance',
        duration: duration ? duration.value : 'all'
    };
}

/** The source is a segmented control, so its value is the checked button. */
function combinedSourceValue() {
    const active = document.querySelector('#aggr-source .top-tab.is-active');
    return active ? active.dataset.value : 'all';
}

/** Put the source segmented control on one of its three values. */
function setCombinedSource(value) {
    document.querySelectorAll('#aggr-source .top-tab').forEach((tab) => {
        const on = tab.dataset.value === value;
        tab.classList.toggle('is-active', on);
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
}

function setFilterValue(id, value) {
    if (id === 'aggr-source') {
        setCombinedSource(value);
        return;
    }
    const el = document.getElementById(id);
    if (el) el.value = value;
}

function combinedSourceLabel() {
    if (combined.source === 'netease') return t('search.source_netease');
    if (combined.source === 'bilibili') return t('search.source_bili');
    return t('search.source_all');
}

function combinedCategoryLabel() {
    if (combined.category === 'song') return t('search.category_song');
    if (combined.category === 'playlist') return t('search.category_playlist');
    if (combined.category === 'video') return t('search.category_video');
    return t('search.category_all');
}

function combinedSortLabel() {
    if (combined.sort === 'plays') return t('search.sort_plays');
    if (combined.sort === 'newest') return t('search.sort_newest');
    if (combined.sort === 'duration') return t('search.sort_duration');
    return t('search.sort_relevance');
}

function combinedDurationLabel() {
    if (combined.duration === 'short') return t('search.duration_short');
    if (combined.duration === 'mid') return t('search.duration_mid');
    if (combined.duration === 'long') return t('search.duration_long');
    if (combined.duration === 'xlong') return t('search.duration_xlong');
    return t('search.duration_all');
}

/** Mark the filters that are not on their default value. */
function updateFilterSelects() {
    // The source is a segmented control: its active button already shows
    // that a filter is on, so only the three selects need the accent.
    const marks = [
        ['aggr-category', combined.category !== 'all'],
        ['aggr-sort', combined.sort !== 'relevance'],
        ['aggr-duration', combined.duration !== 'all']
    ];
    marks.forEach((mark) => {
        const el = document.getElementById(mark[0]);
        if (el) el.classList.toggle('is-set', mark[1]);
    });
}

/**
 * The reason a bridge call failed, in words.
 *
 * The bridge answers with an ``error_key`` (an i18n key) and sometimes a
 * free text ``message``.  The key wins, because it is translated; the text
 * is the fallback for a reason the backend does not know a key for.
 */
function bridgeFailure(result, fallbackKey) {
    if (!result) return t(fallbackKey || 'error.unknown');
    const key = result.error_key;
    if (key) {
        const text = t(key);
        return text === key ? (result.message || t('error.unknown')) : text;
    }
    return result.message || t('error.unknown');
}

/**
 * Is the bridge able to do this at all?
 *
 * A half installed bridge (an older backend without the Bilibili methods)
 * must not throw a JavaScript error into the page: the caller gets a
 * readable reason instead.
 */
function bridgeHas(method) {
    return !!(state.api && typeof state.api[method] === 'function');
}

/** One chip per active filter, so it is visible what narrowed the list. */
function updateFilterChips() {
    const row = document.getElementById('aggr-chips');
    if (!row) return;
    // The label stays, the chips are rebuilt.
    const label = row.querySelector('.filter-chips-label');
    row.innerHTML = '';
    if (label) row.appendChild(label);
    const entries = [];
    if (combined.source !== 'all') {
        entries.push({
            label: combinedSourceLabel(),
            reset: () => { setFilterValue('aggr-source', 'all'); applyCombinedFilters(true); }
        });
    }
    if (combined.category !== 'all') {
        entries.push({
            label: combinedCategoryLabel(),
            reset: () => { setFilterValue('aggr-category', 'all'); applyCombinedFilters(false); }
        });
    }
    if (combined.sort !== 'relevance') {
        entries.push({
            label: combinedSortLabel(),
            reset: () => { setFilterValue('aggr-sort', 'relevance'); applyCombinedFilters(false); }
        });
    }
    if (combined.duration !== 'all') {
        entries.push({
            label: combinedDurationLabel(),
            reset: () => { setFilterValue('aggr-duration', 'all'); applyCombinedFilters(false); }
        });
    }
    entries.forEach((entry) => {
        const chip = document.createElement('button');
        chip.className = 'chip is-active';
        chip.type = 'button';
        const text = document.createElement('span');
        text.textContent = entry.label;
        chip.appendChild(text);
        const close = document.createElement('span');
        close.setAttribute('data-icon', 'close');
        setIcon(close, 'close');
        chip.appendChild(close);
        chip.addEventListener('click', entry.reset);
        row.appendChild(chip);
    });
    if (entries.length) {
        const clear = document.createElement('button');
        clear.className = 'btn btn-link';
        clear.type = 'button';
        clear.textContent = t('search.clear_filters');
        clear.addEventListener('click', clearCombinedFilters);
        row.appendChild(clear);
    }
    row.classList.toggle('is-open', entries.length > 0);
    row.setAttribute('aria-hidden', entries.length ? 'false' : 'true');
}

/** Put every filter back to its default. */
function clearCombinedFilters() {
    setFilterValue('aggr-source', 'all');
    setFilterValue('aggr-category', 'all');
    setFilterValue('aggr-sort', 'relevance');
    setFilterValue('aggr-duration', 'all');
    applyCombinedFilters(true);
}

/** The Bilibili sort order that matches the chosen sort. */
function biliOrderFor(sort) {
    if (sort === 'plays') return 'play';
    if (sort === 'newest') return 'date';
    return 'default';
}

/**
 * Read the filters and render what they allow.
 *
 * The source and the sort decide which rows are asked for: the sort is a
 * request parameter of the Bilibili search.  The category and the length
 * work on the rows that are already loaded, so they cost no request.
 */
function applyCombinedFilters(needsRefetch) {
    const filters = combinedFilters();
    combined.source = filters.source;
    combined.category = filters.category;
    combined.sort = filters.sort;
    combined.duration = filters.duration;
    updateFilterSelects();
    updateFilterChips();
    if (needsRefetch) {
        renderCombinedSearch(combined.keyword, combinedFilters());
        return;
    }
    renderCombinedRows();
}

/** One result row, in the shape of the result list. */
function combinedRowElement(row) {
    const element = document.createElement('div');
    element.className = 'result-row';
    element.dataset.src = row.source;
    element.setAttribute('role', 'button');
    element.tabIndex = 0;
    element.setAttribute('aria-label', row.name);

    if (row.cover) {
        const thumb = document.createElement('img');
        thumb.className = 'result-thumb';
        thumb.src = row.cover;
        thumb.alt = '';
        thumb.loading = 'lazy';
        thumb.addEventListener('error', () => { thumb.style.visibility = 'hidden'; });
        element.appendChild(thumb);
    } else {
        const thumb = document.createElement('div');
        thumb.className = 'result-thumb is-empty';
        thumb.setAttribute('data-icon', 'music');
        setIcon(thumb, 'music');
        element.appendChild(thumb);
    }

    const main = document.createElement('div');
    main.className = 'result-main';
    const nameLine = document.createElement('div');
    nameLine.className = 'result-name-line';
    const name = document.createElement('span');
    name.className = 'result-name';
    name.textContent = row.name;
    nameLine.appendChild(name);
    const badge = document.createElement('span');
    badge.className = row.source === 'bilibili' ? 'source-badge is-bili' : 'source-badge is-netease';
    badge.textContent = row.source === 'bilibili'
        ? t('search.source_bili') : t('search.source_netease');
    nameLine.appendChild(badge);
    main.appendChild(nameLine);
    const sub = document.createElement('div');
    sub.className = 'result-sub';
    sub.textContent = row.source === 'bilibili'
        ? t('search.sub_bili', { uploader: row.artists, plays: formatPlayCount(row.plays) })
        : t('search.sub_netease', { artist: row.artists, album: row.album });
    main.appendChild(sub);
    element.appendChild(main);

    const duration = document.createElement('span');
    duration.className = 'result-dur';
    duration.textContent = row.durationMs ? fmtTime(row.durationMs / 1000) : '';
    element.appendChild(duration);

    element.addEventListener('click', () => openCombinedRow(row));
    element.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            event.stopPropagation();
            openCombinedRow(row);
        }
    });
    return element;
}

/** What happens when a result row is picked. */
function openCombinedRow(row) {
    if (row.source === 'bilibili') {
        const url = (row.track && row.track.url) || '';
        if (url) state.api.open_external(url);
        return;
    }
    if (row.kind === 'playlist') {
        const playlist = {
            id: row.id,
            name: row.name,
            cover_url: row.cover,
            track_count: row.plays
        };
        showPlayerView();
        openPlaylist(playlist, true);
        return;
    }
    showPlayerView();
    playAllInto([row.track], { kind: 'search', name: combined.keyword });
}

/** The player view without touching the source list behind it. */
function showPlayerView() {
    document.querySelectorAll('.view').forEach((view) => {
        view.classList.toggle('is-active', view.id === 'view-player');
    });
    document.querySelectorAll('#top-tabs .top-tab').forEach((tab) => {
        tab.classList.toggle('is-active', tab.dataset.view === 'player');
    });
}

/** A song row of the NetEase source. */
function neteaseSongRow(song) {
    return {
        source: 'netease',
        kind: 'song',
        id: song.id || '',
        name: song.name || '',
        artists: song.artists || '',
        album: song.album || '',
        cover: song.cover_url || song.picUrl || '',
        // The bridge hands the NetEase duration over in milliseconds.
        durationMs: Number(song.duration) || 0,
        plays: Number(song.play_count) || 0,
        track: song
    };
}

/** A playlist row of the NetEase source. */
function neteasePlaylistRow(playlist) {
    const creator = typeof playlist.creator === 'string'
        ? playlist.creator
        : ((playlist.creator && (playlist.creator.nickname || playlist.creator.name)) || '');
    return {
        source: 'netease',
        kind: 'playlist',
        id: playlist.id || '',
        name: playlist.name || playlist.title || '',
        artists: creator,
        album: '',
        cover: playlist.cover_url || playlist.picUrl || playlist.coverImgUrl || '',
        durationMs: 0,
        // A playlist carries the number of its tracks, not a play count.
        plays: Number(playlist.track_count || playlist.play_count) || 0,
        track: playlist
    };
}

/** A video row of the Bilibili source. */
function biliTrackRow(track) {
    return {
        source: 'bilibili',
        kind: 'video',
        id: track.id || track.bvid || '',
        name: track.name || track.title || '',
        artists: track.artists || track.author || '',
        album: track.album || track.category || '',
        cover: track.cover_url || track.cover || '',
        // The Bilibili payload counts in seconds.
        durationMs: (Number(track.duration) || 0) * 1000,
        plays: Number(track.play_count) || 0,
        track: track
    };
}

/** Does a row pass the length filter? */
function combinedMatchesDuration(row, duration) {
    if (duration === 'all') return true;
    if (!row.durationMs) return true;
    const seconds = row.durationMs / 1000;
    if (duration === 'short') return seconds < 60;
    if (duration === 'mid') return seconds < 300;
    if (duration === 'long') return seconds < 900;
    return seconds >= 900;
}

/** The most rows the merged list ever holds (the cap of the paging). */
const COMBINED_MAX_ROWS = 200;

/** The merged list.
 *
 * Each source keeps its own order and the two are interleaved: a "plays"
 * sort only means something inside one source, so the ranking is done per
 * source and then alternated.
 */
function combinedVisibleRows() {
    const category = combined.category;
    const duration = combined.duration;
    const rows = combined.rows.filter((row) => {
        if (category === 'song' && row.kind !== 'song') return false;
        if (category === 'playlist' && row.kind !== 'playlist') return false;
        if (category === 'video' && row.kind !== 'video') return false;
        return combinedMatchesDuration(row, duration);
    });
    const netease = rows.filter((row) => row.source === 'netease');
    const bili = rows.filter((row) => row.source === 'bilibili');
    sortCombinedRows(netease);
    sortCombinedRows(bili);
    const merged = [];
    const count = Math.max(netease.length, bili.length);
    for (let i = 0; i < count; i += 1) {
        if (netease[i]) merged.push(netease[i]);
        if (bili[i]) merged.push(bili[i]);
    }
    return merged.slice(0, COMBINED_MAX_ROWS);
}

/** Sort the rows of one source. */
function sortCombinedRows(rows) {
    if (combined.sort === 'plays') {
        rows.sort((a, b) => b.plays - a.plays);
    } else if (combined.sort === 'duration') {
        rows.sort((a, b) => b.durationMs - a.durationMs);
    } else if (combined.sort === 'newest') {
        // Only the Bilibili payload carries a publish time; the NetEase rows
        // keep the order the source returned them in.
        rows.sort((a, b) => (Number(b.track && b.track.publish_time) || 0)
            - (Number(a.track && a.track.publish_time) || 0));
    }
}

/** The "找到 N 条" line. */
function updateCombinedMeta(rows) {
    const meta = document.getElementById('aggr-meta');
    if (!meta) return;
    let neteaseCount = 0;
    let biliCount = 0;
    rows.forEach((row) => {
        if (row.source === 'bilibili') biliCount += 1;
        else neteaseCount += 1;
    });
    let text = t('search.result_count', { count: rows.length }) + ' · '
        + t('search.result_split', { netease: neteaseCount, bili: biliCount });
    if (combined.neteaseError || combined.biliError) {
        // One source failed: the other one still counts, the broken one is named.
        const okSource = combined.neteaseError ? t('search.source_bili') : t('search.source_netease');
        const okCount = combined.neteaseError ? biliCount : neteaseCount;
        const badSource = combined.neteaseError ? t('search.source_netease') : t('search.source_bili');
        text = t('search.result_partial', { ok: okSource + ' ' + okCount, failed: badSource });
    }
    meta.textContent = text;
}

/** The empty, failed and offline blocks of the result page. */
function updateCombinedState(rows) {
    const box = document.getElementById('aggr-state');
    const errorBox = document.getElementById('aggr-error');
    if (!box) return;
    box.innerHTML = '';
    if (errorBox) {
        errorBox.innerHTML = '';
        errorBox.hidden = true;
    }
    const failed = combined.neteaseError || combined.biliError;
    if (failed && errorBox) {
        const bothFailed = combined.neteaseError && combined.biliError;
        const source = bothFailed
            ? t('search.source_all')
            : (combined.neteaseError ? t('search.source_netease') : t('search.source_bili'));
        const message = combined.neteaseError || combined.biliError;
        const text = document.createElement('span');
        text.textContent = t('search.error_source', { source: source, message: message });
        errorBox.appendChild(text);
        const retry = document.createElement('button');
        retry.className = 'btn btn-link';
        retry.type = 'button';
        retry.textContent = t('common.retry');
        retry.addEventListener('click', () => {
            renderCombinedSearch(combined.keyword, combinedFilters());
        });
        errorBox.appendChild(retry);
        errorBox.hidden = false;
    }
    if (combined.loading) return;
    if (rows.length) return;
    if (navigator.onLine === false) {
        box.appendChild(buildStateBlock('close', t('search.offline_title'),
            t('search.offline_hint')));
        return;
    }
    if (combined.neteaseError && combined.biliError) {
        box.appendChild(buildStateBlock('close', t('search.error_title'),
            t('search.error_source', {
                source: t('search.source_all'),
                message: combined.neteaseError || combined.biliError
            }), t('common.retry'), () => {
                renderCombinedSearch(combined.keyword, combinedFilters());
            }));
        return;
    }
    box.appendChild(buildStateBlock('list', t('search.empty_title'),
        t('search.empty_hint'),
        combined.source !== 'all' || combined.category !== 'all'
            || combined.duration !== 'all' ? t('search.clear_filters') : '',
        () => { clearCombinedFilters(); }));
}

/** Draw the merged list. */
function renderCombinedRows() {
    const list = document.getElementById('search-results');
    if (!list) return;
    const rows = combinedVisibleRows();
    list.innerHTML = '';
    if (combined.loading && !rows.length) {
        for (let i = 0; i < 5; i += 1) {
            const skeleton = document.createElement('div');
            skeleton.className = 'skeleton-row';
            list.appendChild(skeleton);
        }
    } else {
        rows.forEach((row) => list.appendChild(combinedRowElement(row)));
        list.classList.toggle('is-stale', combined.loading);
    }
    updateCombinedMeta(rows);
    updateCombinedState(rows);
    const more = document.getElementById('btn-aggr-more');
    const end = document.getElementById('aggr-end');
    if (more) {
        const exhausted = combined.neteaseDone && combined.biliDone;
        more.hidden = exhausted || combined.loading;
        more.textContent = combined.loading ? t('common.loading') : t('search.load_more');
        more.disabled = combined.loading;
    }
    if (end) {
        // The button gives way to a line of text: "no more" or "too many".
        // It only appears when there is a list to look at.
        const exhausted = combined.neteaseDone && combined.biliDone;
        const capped = rows.length >= COMBINED_MAX_ROWS;
        end.hidden = combined.loading || !rows.length || (!exhausted && !capped);
        end.textContent = capped ? t('search.cap_hint') : t('search.no_more');
    }
}

/** Ask one source for the next page. */
async function loadCombinedNetease(requestId) {
    if (combined.neteaseDone) return;
    try {
        if (!bridgeHas('search_multi')) {
            combined.neteaseError = t('error.unknown');
            combined.neteaseDone = true;
            return;
        }
        const result = await state.api.search_multi(combined.keyword, 20, combined.neteaseOffset);
        if (requestId !== combined.requestId) return;
        if (!result || !result.success) {
            combined.neteaseError = bridgeFailure(result, 'error.network');
            combined.neteaseDone = true;
            return;
        }
        const results = result.results || {};
        const songs = results.songs || [];
        const playlists = results.playlists || [];
        songs.forEach((song) => combined.rows.push(neteaseSongRow(song)));
        playlists.forEach((playlist) => combined.rows.push(neteasePlaylistRow(playlist)));
        combined.neteaseOffset += songs.length;
        if (!songs.length) combined.neteaseDone = true;
    } catch (err) {
        if (requestId !== combined.requestId) return;
        combined.neteaseError = t('error.network');
        combined.neteaseDone = true;
    }
}

/** Ask the Bilibili source for the next page. */
async function loadCombinedBili(requestId) {
    if (combined.biliDone) return;
    try {
        if (!bridgeHas('search_bilibili_tracks')) {
            combined.biliError = t('error.unknown');
            combined.biliDone = true;
            return;
        }
        const result = await state.api.search_bilibili_tracks(
            combined.keyword, 'all', biliOrderFor(combined.sort), combined.biliPage, 20);
        if (requestId !== combined.requestId) return;
        if (!result || !result.success) {
            combined.biliError = bridgeFailure(result, 'error.network');
            combined.biliDone = true;
            return;
        }
        const tracks = result.tracks || [];
        tracks.forEach((track) => combined.rows.push(biliTrackRow(track)));
        combined.biliPage += 1;
        if (!tracks.length) combined.biliDone = true;
    } catch (err) {
        if (requestId !== combined.requestId) return;
        combined.biliError = t('error.network');
        combined.biliDone = true;
    }
}

/** Fetch the next page of every source that has one. */
async function loadCombinedPage() {
    const requestId = combined.requestId + 1;
    combined.requestId = requestId;
    combined.loading = true;
    renderCombinedRows();
    const wanted = [];
    if (combined.source !== 'bilibili') wanted.push(loadCombinedNetease(requestId));
    if (combined.source !== 'netease') wanted.push(loadCombinedBili(requestId));
    await Promise.all(wanted);
    if (requestId !== combined.requestId) return;
    combined.loading = false;
    renderCombinedRows();
}

/**
 * Search both sources for a keyword.
 *
 * The two sources are asked separately on purpose: one of them failing
 * must not empty the page.
 */
async function renderCombinedSearch(keyword, filters) {
    const text = (keyword || '').trim();
    const options = filters || combinedFilters();
    combined.source = options.source;
    combined.category = options.category;
    combined.sort = options.sort;
    combined.duration = options.duration;
    combined.keyword = text;
    combined.rows = [];
    combined.neteaseOffset = 0;
    combined.biliPage = 1;
    combined.neteaseDone = false;
    combined.biliDone = false;
    combined.neteaseError = '';
    combined.biliError = '';
    // A source that is filtered out is finished by definition: without this
    // the "no more" line would never show when only one source is asked.
    if (combined.source === 'bilibili') combined.neteaseDone = true;
    if (combined.source === 'netease') combined.biliDone = true;
    updateFilterSelects();
    updateFilterChips();
    if (!text) {
        combined.loading = false;
        renderCombinedRows();
        return;
    }
    await loadCombinedPage();
}

/* ------------------------------------------------------------- bilibili */

/** One chip of the partition row. */
function biliChipElement(label, rid, active) {
    const chip = document.createElement('button');
    chip.className = active ? 'chip is-active' : 'chip';
    chip.type = 'button';
    chip.dataset.rid = rid;
    chip.setAttribute('aria-pressed', active ? 'true' : 'false');
    chip.textContent = label;
    chip.addEventListener('click', () => renderBili(rid));
    return chip;
}

/** The partition row: the hot list first, then the partitions. */
function renderBiliChips() {
    const row = document.getElementById('bili-chips');
    if (!row) return;
    row.innerHTML = '';
    row.appendChild(biliChipElement(t('bili.chip_trending'), 0, biliSection.rid === 0));
    biliSection.partitions.forEach((partition) => {
        if (!partition || partition.rid === 0) return;
        const key = BILI_CHIP_KEYS[partition.key];
        const label = key ? t(key) : (partition.name || String(partition.rid));
        row.appendChild(biliChipElement(label, partition.rid, biliSection.rid === partition.rid));
    });
}

/** One video card. */
function biliCardElement(track) {
    const card = document.createElement('button');
    card.className = 'video-card';
    card.type = 'button';
    card.setAttribute('aria-label', track.name || '');
    const thumb = document.createElement('div');
    thumb.className = 'vc-thumb';
    if (track.cover_url) {
        const image = document.createElement('img');
        image.src = track.cover_url;
        image.alt = '';
        image.loading = 'lazy';
        image.addEventListener('error', () => { image.remove(); });
        thumb.appendChild(image);
    }
    const duration = document.createElement('span');
    duration.className = 'vc-dur';
    duration.textContent = track.duration ? fmtTime(track.duration) : '';
    thumb.appendChild(duration);
    card.appendChild(thumb);
    const title = document.createElement('span');
    title.className = 'vc-title';
    title.textContent = track.name || '';
    card.appendChild(title);
    const sub = document.createElement('span');
    sub.className = 'vc-sub';
    const up = document.createElement('span');
    up.className = 'vc-up';
    up.textContent = t('bili.by', { name: track.artists || '' });
    const plays = document.createElement('span');
    plays.className = 'vc-plays';
    plays.textContent = t('bili.plays', { count: formatPlayCount(track.play_count) });
    sub.appendChild(up);
    sub.appendChild(plays);
    card.appendChild(sub);
    card.addEventListener('click', () => {
        const url = track.url || '';
        if (url) state.api.open_external(url);
    });
    return card;
}

/** Draw the grid, its skeleton and its states. */
function renderBiliResults() {
    const grid = document.getElementById('bili-results');
    const box = document.getElementById('bili-state');
    if (!grid) return;
    grid.innerHTML = '';
    if (box) box.innerHTML = '';
    if (biliSection.loading && !biliSection.items.length) {
        for (let i = 0; i < 6; i += 1) {
            const skeleton = document.createElement('div');
            skeleton.className = 'skeleton-card';
            const thumb = document.createElement('div');
            thumb.className = 'skeleton sk-thumb';
            skeleton.appendChild(thumb);
            const line = document.createElement('div');
            line.className = 'skeleton sk-line';
            skeleton.appendChild(line);
            const short = document.createElement('div');
            short.className = 'skeleton sk-line short';
            skeleton.appendChild(short);
            grid.appendChild(skeleton);
        }
    } else {
        biliSection.items.forEach((track) => grid.appendChild(biliCardElement(track)));
    }
    if (biliSection.loading) return;
    if (biliSection.error && box) {
        box.appendChild(buildStateBlock('close', t('bili.error_title'),
            t('bili.error_hint', { message: biliSection.error }),
            t('common.retry'), () => renderBili(biliSection.rid)));
        return;
    }
    if (!biliSection.items.length && box) {
        if (navigator.onLine === false) {
            box.appendChild(buildStateBlock('close', t('bili.offline_title'),
                t('bili.offline_hint')));
            return;
        }
        box.appendChild(buildStateBlock('list', t('bili.empty_title'),
            t('bili.empty_hint'), t('bili.refresh'), () => renderBili(biliSection.rid)));
    }
    const more = document.getElementById('btn-bili-more');
    if (more) {
        more.hidden = biliSection.exhausted || biliSection.loading;
        more.textContent = biliSection.loading ? t('common.loading') : t('bili.load_more');
        more.disabled = biliSection.loading;
    }
    const end = document.getElementById('bili-end');
    if (end) {
        // The button gives way to a line of text when the list is done.
        end.hidden = biliSection.loading || !biliSection.items.length || !biliSection.exhausted;
        end.textContent = t('bili.no_more');
    }
}

/** Read the partitions once; they do not change while the app runs. */
async function loadBiliPartitions() {
    if (biliSection.partitions.length) return;
    try {
        if (!bridgeHas('get_bilibili_partitions')) {
            biliSection.partitions = [];
            renderBiliChips();
            return;
        }
        const result = await state.api.get_bilibili_partitions();
        if (result && result.success) {
            biliSection.partitions = result.partitions || [];
        }
    } catch (err) {
        biliSection.partitions = [];
    }
    renderBiliChips();
}

/**
 * Show one partition of the Bilibili source.
 *
 * ``rid`` 0 is the site wide hot list; every other id is a partition.
 */
async function renderBili(rid) {
    biliSection.rid = rid;
    biliSection.page = 1;
    biliSection.items = [];
    biliSection.error = '';
    biliSection.loading = true;
    biliSection.exhausted = false;
    updateOfflineBars();
    renderBiliChips();
    renderBiliResults();
    loadBiliPartitions();
    try {
        if (!bridgeHas('get_bilibili_popular')) {
            biliSection.error = t('error.unknown');
            biliSection.loading = false;
            renderBiliResults();
            return;
        }
        const result = await state.api.get_bilibili_popular(rid, 1, 30);
        if (biliSection.rid !== rid) return;
        if (!result || !result.success) {
            biliSection.error = bridgeFailure(result, 'error.network');
            biliSection.loading = false;
            renderBiliResults();
            return;
        }
        const tracks = result.tracks || result.items || [];
        biliSection.items = tracks;
        biliSection.exhausted = tracks.length < 30;
        biliSection.loading = false;
        renderBiliResults();
    } catch (err) {
        if (biliSection.rid !== rid) return;
        biliSection.error = t('error.network');
        biliSection.loading = false;
        renderBiliResults();
    }
}

/** The next page of the open partition. */
async function loadMoreBili() {
    if (biliSection.loading || biliSection.exhausted) return;
    biliSection.loading = true;
    renderBiliResults();
    try {
        if (!bridgeHas('get_bilibili_popular')) {
            biliSection.error = t('error.unknown');
            biliSection.loading = false;
            renderBiliResults();
            return;
        }
        const result = await state.api.get_bilibili_popular(
            biliSection.rid, biliSection.page + 1, 30);
        if (!result || !result.success) {
            biliSection.error = bridgeFailure(result, 'error.network');
            biliSection.loading = false;
            renderBiliResults();
            return;
        }
        biliSection.page += 1;
        const tracks = result.tracks || result.items || [];
        tracks.forEach((track) => biliSection.items.push(track));
        biliSection.exhausted = tracks.length < 30;
        biliSection.loading = false;
        renderBiliResults();
    } catch (err) {
        biliSection.error = t('error.network');
        biliSection.loading = false;
        renderBiliResults();
    }
}

/* --------------------------------------------------------------- wiring */

/** The events of the three new views. */
function bindSectionEvents() {
    const grid = document.getElementById('home-grid');
    if (grid) {
        grid.addEventListener('click', (event) => {
            const card = event.target.closest('.home-card');
            if (!card) return;
            // detail 0 means the click came from the keyboard.
            openHomeEntry(card.dataset.entry, event.detail === 0);
        });
        grid.addEventListener('keydown', (event) => {
            const cards = Array.prototype.slice.call(grid.querySelectorAll('.home-card'));
            const index = cards.indexOf(document.activeElement);
            if (index < 0) return;
            const columns = window.getComputedStyle(grid).gridTemplateColumns.split(' ').length;
            let next = -1;
            if (event.key === 'ArrowRight') next = index % columns === columns - 1 ? index : index + 1;
            else if (event.key === 'ArrowLeft') next = index % columns === 0 ? index : index - 1;
            else if (event.key === 'ArrowDown') next = index + columns;
            else if (event.key === 'ArrowUp') next = index - columns;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = cards.length - 1;
            else if (event.key === ' ') {
                event.preventDefault();
                event.stopPropagation();
                cards[index].click();
                return;
            } else {
                return;
            }
            if (next < 0 || next >= cards.length) {
                event.preventDefault();
                return;
            }
            event.preventDefault();
            cards[next].focus();
        });
    }

    const retry = document.getElementById('btn-home-retry');
    if (retry) retry.addEventListener('click', () => showHome());

    const searchBack = document.getElementById('btn-search-back');
    if (searchBack) searchBack.addEventListener('click', () => backToHome());
    const biliBack = document.getElementById('btn-bili-back');
    if (biliBack) biliBack.addEventListener('click', () => backToHome());

    const form = document.getElementById('aggr-form');
    if (form) {
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            runCombinedSearch();
        });
    }
    const input = document.getElementById('aggr-input');
    if (input) {
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                runCombinedSearch();
            }
        });
        input.addEventListener('input', () => updateSearchButton());
    }

    const sourceSeg = document.getElementById('aggr-source');
    if (sourceSeg) {
        sourceSeg.addEventListener('click', (event) => {
            const tab = event.target.closest('.top-tab');
            if (!tab) return;
            setCombinedSource(tab.dataset.value);
            // The source decides which source is asked, so it needs a new
            // request; the other filters work on the rows already on screen.
            applyCombinedFilters(true);
        });
    }

    ['aggr-category', 'aggr-sort', 'aggr-duration'].forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('change', () => {
            // The sort is a request parameter of the Bilibili search, the
            // category and the length are applied to the loaded rows.
            applyCombinedFilters(id === 'aggr-sort');
        });
    });

    const more = document.getElementById('btn-aggr-more');
    if (more) more.addEventListener('click', () => loadCombinedPage());

    const refresh = document.getElementById('btn-bili-refresh');
    if (refresh) refresh.addEventListener('click', () => renderBili(biliSection.rid));
    const biliMore = document.getElementById('btn-bili-more');
    if (biliMore) biliMore.addEventListener('click', () => loadMoreBili());
}

/** Run the search the user asked for. */
function runCombinedSearch() {
    const input = document.getElementById('aggr-input');
    const keyword = input ? (input.value || '').trim() : '';
    updateSearchButton();
    if (!keyword) {
        showToast('warning', t('player.search_empty'));
        return;
    }
    renderCombinedSearch(keyword, combinedFilters());
}

/** The search button is disabled while there is nothing to search for. */
function updateSearchButton() {
    const input = document.getElementById('aggr-input');
    const button = document.getElementById('btn-aggr-search');
    if (!input || !button) return;
    button.disabled = !(input.value || '').trim();
    button.textContent = combined.loading ? t('search.loading') : t('search.btn');
}
