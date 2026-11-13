/**
 * SkycMedia - frontend logic.
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
        // 放在 loadSettings 之后：开关本身存在设置里，读早了永远是关。
        await loadSettings();
        checkForUpdates(false);
        const loginStatus = await state.api.get_login_status();
        state.loggedIn = !!(loginStatus && loginStatus.is_logged_in);
        await refreshLoginStatus();
        // The Bilibili account belongs in the header as well, so read it
        // right away instead of waiting for the Bilibili page to open.
        loadBiliAccount();
        // Pixiv 的接线放在这里：翻译已经装好（t() 能用），api 也已经在了。
        if (window.PixivUI) PixivUI.init();
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
    safeRender('renderBiliAccount', renderBiliAccount);
    safeRender('renderBiliFolderChips', renderBiliFolderChips);
    safeRender('renderBiliFavorites', renderBiliFavorites);
    // The like and the favourite button say whether the video is already
    // liked, so their text is state, not a fixed label: after a language
    // switch they have to be drawn again, not left on the static one.
    safeRender('renderBiliDetailHeader', renderBiliDetailHeader);
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
    const modal = document.getElementById(id);
    if (modal) modal.hidden = true;
    // The login poll must stop when the form goes away, otherwise it keeps
    // asking the backend about a QR code nobody can see any more.
    if (id === 'account-center-modal' && state.api) {
        state.api.cancel_login();
    }
    if (id === 'account-center-modal') stopBiliLoginPolling();
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
    if (name) {
        name.textContent = profile.nickname || t('account.not_logged_in');
        // The profile is the user's own page: clicking the identity opens
        // the account center, where both accounts live.
        name.classList.add('is-link');
        name.onclick = () => openAccountCenter();
    }
    if (avatar) {
        avatar.classList.add('is-link');
        avatar.onclick = () => openAccountCenter();
    }
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
    // 主动检查是用户自己开的开关；启动时这一次只在开关打开时跑。
    const wanted = force || (state.settings && state.settings.ui
        && state.settings.ui.check_updates_on_start === true);
    if (!wanted) return;
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
    fillBangumiSettings(s.bangumi);
    setValue('quality-select', s.download.quality);
    setChecked('hires-toggle', s.download.hires_enabled);
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

    // bilibili
    const bili = s.bili || {};
    setValue('bili-quality-select', String(bili.quality || 0));
    setChecked('bili-danmaku-check', bili.danmaku_enabled !== false);
    setValue('bili-danmaku-size', String(bili.danmaku_font_size || 18));
    setValue('bili-danmaku-opacity', String(Math.round((bili.danmaku_opacity ?? 0.9) * 100)));
    setChecked('bili-download-audio-check', bili.download_audio !== false);
    setChecked('bili-merge-audio-check', bili.merge_audio !== false);
    setChecked('bili-separate-window-check', bili.separate_player_window !== false);
    setChecked('bili-report-playback-check', bili.report_playback === true);
    setChecked('bili-resume-playback-check', bili.resume_playback !== false);
    setValue('bili-download-dir', bili.download_dir || '');
    setValue('bili-interest-tags', (bili.interest_tags || []).join('，'));
    setValue('bili-blocked-tags', (bili.blocked_tags || []).join('，'));
    updateBiliSettingsLabels();
    renderBiliSettingsAccount();

    // updates
    setChecked('update-on-start-check', s.ui.check_updates_on_start === true);
    setChecked('update-auto-install-check', s.ui.auto_install_updates === true);
    setChecked('update-prerelease-check', s.ui.update_include_prerelease === true);
    setChecked('update-skip-ssl-check', s.ui.update_skip_ssl_verify === true);
    setChecked('update-use-sdm-check', s.ui.update_use_sdm !== false);
    const threads = document.getElementById('update-threads-select');
    if (threads) threads.value = String(Number(s.ui.update_threads) || 8);

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
        const label = folderRowName(directory);
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
            check_updates_on_start: document.getElementById('update-on-start-check').checked,
            auto_install_updates: document.getElementById('update-auto-install-check').checked,
            update_include_prerelease: document.getElementById('update-prerelease-check').checked,
            update_skip_ssl_verify: document.getElementById('update-skip-ssl-check').checked,
            update_use_sdm: document.getElementById('update-use-sdm-check').checked,
            update_threads: Number(document.getElementById('update-threads-select').value) || 8,
        },
        auth: {
            remember_login: document.getElementById('remember-login-check').checked,
        },
        bili: {
            quality: parseInt(document.getElementById('bili-quality-select').value, 10) || 0,
            danmaku_enabled: document.getElementById('bili-danmaku-check').checked,
            danmaku_font_size: parseInt(document.getElementById('bili-danmaku-size').value, 10),
            danmaku_opacity: parseInt(document.getElementById('bili-danmaku-opacity').value, 10) / 100,
            download_audio: document.getElementById('bili-download-audio-check').checked,
            merge_audio: document.getElementById('bili-merge-audio-check').checked,
            download_dir: (document.getElementById('bili-download-dir').value || '').trim(),
            separate_player_window: document.getElementById('bili-separate-window-check').checked,
            report_playback: document.getElementById('bili-report-playback-check').checked,
            resume_playback: document.getElementById('bili-resume-playback-check').checked,
            interest_tags: splitTagWords(document.getElementById('bili-interest-tags').value),
            blocked_tags: splitTagWords(document.getElementById('bili-blocked-tags').value),
        },
        bangumi: {
            api_host: (document.getElementById('bangumi-api-host') || {}).value || '',
            web_host: (document.getElementById('bangumi-web-host') || {}).value || '',
            request_interval: parseFloat(
                (document.getElementById('bangumi-request-interval') || {}).value) || 1,
            skip_ssl_verify: !!(
                (document.getElementById('bangumi-skip-ssl') || {}).checked),
            mirrors: bangumiSettingsState.mirrors,
            resource_sites: bangumiSettingsState.sites,
        },
        debug: document.getElementById('debug-check').checked,
    };

    for (const category of ['download', 'playback', 'ui', 'auth', 'bili', 'bangumi']) {
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

/* ------------------------------------------------------- application update */

/** What the update panel knows right now. */
const appUpdate = {
    latest: '',
    version: '',
    notes: '',
    assetUrl: '',
    assetName: '',
    stage: 'idle',   // idle|available|downloaded|installing
    busy: false
};

/** Draw the update panel from ``appUpdate`` and the last answer. */
function renderAppUpdate(message) {
    const status = document.getElementById('update-status');
    const notes = document.getElementById('update-notes');
    const download = document.getElementById('btn-download-update');
    const install = document.getElementById('btn-install-update');
    const check = document.getElementById('btn-check-update');
    if (status && message) status.textContent = message;
    if (notes) {
        notes.hidden = !appUpdate.notes;
        notes.textContent = appUpdate.notes || '';
    }
    if (download) {
        download.hidden = appUpdate.stage === 'downloaded' || appUpdate.stage === 'installing';
        download.disabled = appUpdate.busy || !appUpdate.assetUrl;
    }
    if (install) install.hidden = appUpdate.stage !== 'downloaded';
    if (check) check.disabled = appUpdate.busy;
    // 不是「正在下」就把进度条收起来，别让它停在半格像卡住了。
    if (appUpdate.stage !== 'downloading') renderUpdateProgress(null);
}

/** 把「每秒多少字节」写成看得懂的样子。 */
function formatUpdateSpeed(bytesPerSecond) {
    const speed = Number(bytesPerSecond) || 0;
    if (speed <= 0) return '0 KB/s';
    if (speed >= 1024 * 1024) return (speed / (1024 * 1024)).toFixed(1) + ' MB/s';
    return Math.round(speed / 1024) + ' KB/s';
}

/** 更新包的下载进度：一条进度条 + 一行「百分比 · 速度 · 多少兆」。 */
function renderUpdateProgress(payload) {
    const bar = document.getElementById('update-progress');
    const fill = document.getElementById('update-progress-bar');
    const text = document.getElementById('update-progress-text');
    if (!bar || !fill || !text) return;
    if (!payload) {
        bar.hidden = true;
        text.hidden = true;
        if (fill) fill.style.width = '0%';
        return;
    }
    const total = Number(payload.total) || 0;
    const received = Number(payload.received) || 0;
    // 服务端没给长度时只知道「下了多少」，进度条就当作不确定态（走满格再淡掉）。
    const percent = total > 0
        ? Math.max(0, Math.min(100, (received * 100) / total))
        : Number(payload.percent) || 0;
    bar.hidden = false;
    text.hidden = false;
    fill.style.width = percent.toFixed(1) + '%';
    const parts = [percent.toFixed(total > 0 ? 0 : 1) + '%'];
    if (total > 0) parts.push(formatBytes(received) + ' / ' + formatBytes(total));
    if (payload.speed) parts.push(formatUpdateSpeed(payload.speed));
    text.textContent = parts.join(' · ');
}

/**
 * 后端推过来的下载进度。
 *
 * 频次由后端掐（四分之一秒一次）：不掐的话一个几十兆的包会推几千条，
 * 每次都是一遍 JS 执行，界面跟着掉帧。
 */
function onAppUpdateProgress(payload) {
    if (appUpdate.stage !== 'downloading') appUpdate.stage = 'downloading';
    renderUpdateProgress(payload);
}

/** Ask GitHub whether a newer release exists. */
async function checkAppUpdate(options) {
    const opts = options || {};
    if (appUpdate.busy) return;
    if (!bridgeHas('check_app_update')) return;
    appUpdate.busy = true;
    renderAppUpdate(t('settings.update_checking'));
    try {
        // 版本通道由设置里的开关决定（后端读 settings.ui），这里的参数
        // 只是留着，前端不用自己判断。
        const result = await state.api.check_app_update(!!opts.includePrerelease);
        if (!result || !result.success) {
            renderAppUpdate(t('settings.update_check_failed', {
                message: (result && result.message) || ''
            }));
            return;
        }
        appUpdate.latest = result.latest || '';
        appUpdate.version = result.version || '';
        appUpdate.notes = result.notes || '';
        // Pick the asset for this machine.  The backend classifies every
        // file as "macos-arm64" / "windows-x64" / "other", so the frontend
        // only has to compare that against the running platform instead of
        // parsing file names itself.
        const assets = result.assets || [];
        const platform = result.platform || '';
        const arch = result.arch || '';
        const exact = assets.find((asset) =>
            String(asset.kind || '') === platform + '-' + arch);
        const loose = assets.find((asset) =>
            String(asset.kind || '').split('-')[0] === platform);
        const chosen = exact || loose || assets[0] || null;
        appUpdate.assetUrl = chosen ? String(chosen.url || chosen.browser_url || '') : '';
        appUpdate.assetName = chosen ? String(chosen.name || '') : '';
        appUpdate.stage = result.available ? 'available' : 'idle';
        renderAppUpdate(result.available
            ? t('settings.update_available', { version: appUpdate.version })
            : t('settings.update_up_to_date', { version: result.current || '' }));
    } catch (err) {
        renderAppUpdate(t('settings.update_check_failed', { message: String(err) }));
    } finally {
        appUpdate.busy = false;
        renderAppUpdate();
    }
}

/** Download the release asset into the temp folder. */
async function downloadAppUpdate() {
    if (appUpdate.busy || !appUpdate.assetUrl) return;
    if (!bridgeHas('download_app_update')) return;
    appUpdate.busy = true;
    appUpdate.stage = 'downloading';
    renderUpdateProgress(null);
    renderAppUpdate(t('settings.update_downloading'));
    try {
        const result = await state.api.download_app_update(appUpdate.assetUrl, appUpdate.assetName);
        renderUpdateProgress(null);
        if (!result || !result.success) {
            appUpdate.stage = 'available';
            renderAppUpdate(t('settings.update_download_failed', {
                message: (result && result.message) || ''
            }));
            return;
        }
        appUpdate.assetPath = result.path || '';
        appUpdate.stage = 'downloaded';
        // 说清楚是谁下的：借到 SDM 就是分片并发的，退回内置就一条连接。
        // 用户嫌慢的时候，这一句就是答案。
        const engine = result.engine === 'sdm'
            ? t('settings.update_via_sdm')
            : t('settings.update_via_builtin');
        renderAppUpdate(t('settings.update_downloaded') + ' · ' + engine);
    } catch (err) {
        renderUpdateProgress(null);
        appUpdate.stage = 'available';
        renderAppUpdate(t('settings.update_download_failed', { message: String(err) }));
    } finally {
        appUpdate.busy = false;
        renderAppUpdate();
    }
}

/** Put the downloaded files in place and tell the user to restart. */
async function installAppUpdate() {
    if (appUpdate.busy || !appUpdate.assetPath) return;
    if (!bridgeHas('install_app_update')) return;
    if (!await confirmDialog('settings.update_install_confirm')) return;
    appUpdate.busy = true;
    appUpdate.stage = 'installing';
    renderAppUpdate(t('settings.update_installing'));
    try {
        const result = await state.api.install_app_update(appUpdate.assetPath);
        if (!result || !result.success) {
            appUpdate.stage = 'downloaded';
            renderAppUpdate(t('settings.update_install_failed', {
                message: (result && result.message) || ''
            }));
            return;
        }
        showToast('success', t('settings.update_installed'), 10000);
        renderAppUpdate(t('settings.update_restart_hint'));
    } catch (err) {
        appUpdate.stage = 'downloaded';
        renderAppUpdate(t('settings.update_install_failed', { message: String(err) }));
    } finally {
        appUpdate.busy = false;
        renderAppUpdate();
    }
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

/** Split a comma/space separated list of words, keeping the order. */
function splitTagWords(text) {
    return String(text || '')
        .split(/[,，;；\s]+/)
        .map((word) => word.trim())
        .filter((word, index, all) => word && all.indexOf(word) === index);
}

/** Pick a folder for the Bilibili downloads (separate from the music one). */
async function chooseBiliDirectory() {
    if (!bridgeHas('choose_directory')) return;
    const dir = await state.api.choose_directory();
    if (!dir) return;
    const input = document.getElementById('bili-download-dir');
    if (input) input.value = dir;
    if (state.settings && state.settings.bili) state.settings.bili.download_dir = dir;
}

/* ------------------------------------------------------------ account backup */

/** Pending import plan: the conflicts the user still has to decide about. */
const backup = {
    conflicts: [],
    summary: null,
    // Whether a NetEase account is signed in right now: it decides if the
    // NetEase half of the file can be restored at all.
    neteaseSignedIn: true,
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
    // The caller may hand over the whole answer (the counts then live in
    // ``local_restored``) or the restored part itself.  Reading only the
    // first shape made the import summary say "0 restored records" every
    // time, because the second shape fell through to the empty branch.
    const restored = data.local_restored || data;
    return (restored.play_counts || 0) + (restored.favorites || 0)
        + (restored.recent || 0);
}

/**
 * The name in a row of the backup list.
 *
 * The row clips from the left, and that needs the right-to-left direction
 * -- which mirrors the brackets of a Chinese name.  The text goes into
 * its own isolate so that it keeps the order it was written in.
 */
function folderRowName(text) {
    const span = document.createElement('span');
    const inner = document.createElement('bdi');
    inner.textContent = text || '';
    span.appendChild(inner);
    return span;
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

/**
 * Whether a NetEase account is signed in.
 *
 * The state is only read at startup, so a login that happened in the
 * meantime would still say "not signed in".  The bridge is asked again.
 */
async function neteaseSignedIn() {
    if (!state.api || !state.api.get_login_status) return !!state.loggedIn;
    try {
        const status = await state.api.get_login_status();
        return !!(status && status.is_logged_in);
    } catch (err) {
        return !!state.loggedIn;
    }
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
        // The file holds the data of both accounts; the Bilibili half has to
        // be counted on its own, or a Bilibili-only export would look empty.
        const biliFolders = result.bilibili_folders || 0;
        const biliVideos = result.bilibili_videos || 0;
        const hasBili = result.bilibili !== false && (biliFolders || biliVideos
            || result.bilibili === true);
        const hasNetease = result.netease !== false;
        const segments = [];
        if (hasNetease) {
            segments.push(t('backup.export_done', {
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
        }
        if (hasBili) {
            segments.push(hasNetease
                ? t('backup.export_bili', {folders: biliFolders, videos: biliVideos})
                : t('backup.export_bili_only', {
                    path: result.path || '',
                    folders: biliFolders,
                    videos: biliVideos,
                }));
        }
        setBackupStatus(segments.join(' · '));
        setBackupProgress(0, 0);
        const short = [];
        if (hasNetease) {
            short.push(t('backup.export_done_short', {
                playlists: result.playlists || 0,
                songs: result.songs || 0,
            }));
        }
        if (hasBili) {
            short.push(hasNetease
                ? t('backup.export_bili', {folders: biliFolders, videos: biliVideos})
                : t('backup.export_bili_only_short', {
                    folders: biliFolders,
                    videos: biliVideos,
                }));
        }
        showToast('success', short.join(' · '), 6000);
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
        // Whether a NetEase account is signed in decides if the NetEase half
        // of the file can be restored at all.
        backup.neteaseSignedIn = await neteaseSignedIn();
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
        const lines = [t('backup.conflict_summary', {
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
        })];
        // Only Bilibili is signed in: the NetEase half of the file cannot be
        // restored, and the numbers above would promise otherwise.
        const neteaseInFile = (stats.favorites_total || 0)
            + (stats.playlists_total || 0) + (stats.artists_total || 0);
        if (neteaseInFile && !backup.neteaseSignedIn) {
            lines.push(t('backup.netease_skip'));
        }
        summary.textContent = lines.join(' · ');
    }
    renderBackupBilibili(plan.bilibili || {});
    (plan.skipped || []).forEach((item) => {
        const row = document.createElement('div');
        row.className = 'folder-row';
        const name = folderRowName(t('backup.skip_' + (item.reason || 'other'), {
            name: item.name || '',
        }));
        row.appendChild(name);
        list.appendChild(row);
    });
    (plan.conflicts || []).forEach((item) => {
        const row = document.createElement('div');
        row.className = 'folder-row';
        const name = folderRowName(t('backup.conflict_row', {
            name: item.name || '',
            count: item.song_count || 0,
        }));
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

/**
 * The Bilibili half of the backup file.
 *
 * The file may carry the Bilibili folders even when nobody is signed in to
 * Bilibili right now.  The user has to be told which of the two it is: the
 * folders come back, or they are skipped until the account is signed in.
 */
function renderBackupBilibili(bili) {
    const block = document.getElementById('backup-bili-block');
    const summary = document.getElementById('backup-bili-summary');
    const list = document.getElementById('backup-bili-list');
    if (!block) return;
    const folders = (bili && bili.folders) || [];
    const folderCount = Number(bili && bili.folder_count) || folders.length;
    const videoCount = Number(bili && bili.video_count) || 0;
    if (list) list.innerHTML = '';
    if (!folderCount) {
        // Nothing of Bilibili in the file: there is nothing to say about it.
        block.hidden = true;
        return;
    }
    block.hidden = false;
    const signedIn = !!(bili && bili.signed_in);
    const account = (bili && bili.account) || {};
    const lines = [t('backup.bili_summary', {folders: folderCount, videos: videoCount})];
    if (account.uname) {
        lines.push(t('backup.bili_account', {name: account.uname}));
    }
    lines.push(signedIn ? t('backup.bili_restore') : t('backup.bili_skip'));
    if (summary) summary.textContent = lines.join(' · ');
    folders.forEach((folder) => {
        if (!list) return;
        const row = document.createElement('div');
        row.className = 'folder-row';
        const name = folderRowName(t('backup.bili_folder_row', {
            name: folder.title || '',
            count: (folder.videos || []).length,
        }));
        row.appendChild(name);
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
        const bili = result.bilibili || {};
        const status = t('backup.import_done', {
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
        });
        setBackupStatus(status);
        setBackupProgress(0, 0);
        const short = t('backup.import_done_short', {
            songs: result.songs_liked || 0,
            playlists: result.playlists_created || 0,
        });
        // The Bilibili half is counted on its own: it is a different account.
        if (bili.folders_created || bili.videos_added) {
            const biliText = t('backup.import_bili', {
                folders: bili.folders_created || 0,
                videos: bili.videos_added || 0,
            });
            setBackupStatus(`${status} · ${biliText}`);
            showToast('success', `${short} · ${biliText}`, 6000);
        } else {
            showToast('success', short, 6000);
        }
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

/* ================================================================
   The account center: both accounts in one place.

   The NetEase account used to live in the header button and the
   Bilibili one in a dropdown under a button of its own, and each of
   them had a login modal of its own.  Now there is one button, one
   modal, and inside it both accounts show their state, what can be
   done with them and the way in (login and register).
   ================================================================ */

/** What the account center knows about the two accounts. */
const accountCenter = {
    netease: { loggedIn: false, nickname: '', level: '', vip: '', avatar: '' },
    bilibili: { loggedIn: false, nickname: '', level: '', vip: '', avatar: '' },
    loginFor: ''
};

/**
 * Open the account center.
 *
 * @param {string} [which] 'netease' or 'bilibili' to open the login form
 *   of that account right away
 */
async function openAccountCenter(which) {
    accountCenter.loginFor = which || '';
    openModal('account-center-modal');
    await refreshAccountCenter();
    showAccountLogin(accountCenter.loginFor);
}

/** Put the login form of one account away, or take it out. */
function showAccountLogin(which) {
    accountCenter.loginFor = which || '';
    const netease = document.getElementById('ac-netease-login');
    const bili = document.getElementById('ac-bili-login');
    if (netease) netease.hidden = accountCenter.loginFor !== 'netease';
    if (bili) bili.hidden = accountCenter.loginFor !== 'bilibili';
    if (accountCenter.loginFor === 'netease') switchLoginTab('qrcode');
    if (accountCenter.loginFor === 'bilibili') switchBiliLoginTab('qr');
}

/** Read both accounts and draw the two cards. */
async function refreshAccountCenter() {
    await Promise.all([refreshNeteaseAccountCard(), refreshBiliAccountCard()]);
    renderAccountCenterButton();
}

/** The NetEase card of the account center. */
async function refreshNeteaseAccountCard() {
    const state_ = document.getElementById('ac-netease-state');
    if (!state_) return;
    const profileBox = document.getElementById('ac-netease-profile');
    const actions = document.getElementById('ac-netease-actions');
    const card = accountCenter.netease;
    let profile = null;
    try {
        const result = await state.api.get_user_profile(false);
        if (result && result.success) profile = result.profile;
    } catch (err) {
        profile = null;
    }
    card.loggedIn = !!profile;
    card.nickname = '';
    card.level = '';
    card.vip = '';
    card.avatar = '';
    if (profile) {
        card.nickname = profile.nickname || '';
        card.level = profile.level != null ? String(profile.level) : '';
        card.vip = profile.vipType > 0 ? `VIP ${profile.vipType}` : '';
        card.avatar = profile.avatarUrl || profile.avatarImgUrl || '';
    }
    state_.textContent = card.loggedIn
        ? t('account.center_signed_in') : t('account.center_signed_out');
    state_.className = 'ac-card-state' + (card.loggedIn ? ' is-on' : '');
    if (profileBox) {
        profileBox.hidden = !card.loggedIn;
        const face = document.getElementById('ac-netease-face');
        if (face) {
            if (card.avatar) {
                face.src = proxyCoverUrl(card.avatar);
                face.hidden = false;
                face.addEventListener('error', () => { face.hidden = true; });
            } else {
                face.removeAttribute('src');
                face.hidden = true;
            }
        }
        const name = document.getElementById('ac-netease-name');
        if (name) name.textContent = card.nickname;
        const meta = document.getElementById('ac-netease-meta');
        if (meta) {
            const parts = [];
            if (card.level) parts.push(t('account.level', { level: card.level }));
            if (card.vip) parts.push(card.vip);
            meta.textContent = parts.join(' · ');
        }
    }
    if (actions) {
        actions.innerHTML = '';
        if (card.loggedIn) {
            const detail = document.createElement('button');
            detail.className = 'btn btn-sm btn-secondary';
            detail.type = 'button';
            detail.textContent = t('account.center_profile');
            detail.addEventListener('click', () => openAccountPanel(false));
            actions.appendChild(detail);
            const logout = document.createElement('button');
            logout.className = 'btn btn-sm btn-secondary';
            logout.type = 'button';
            logout.textContent = t('account.center_logout');
            logout.addEventListener('click', async () => {
                if (!await confirmDialog('login.logout_confirm')) return;
                await state.api.logout();
                player.playlists = [];
                await refreshLoginStatus();
                await refreshAccountCenter();
            });
            actions.appendChild(logout);
        } else {
            const login = document.createElement('button');
            login.className = 'btn btn-sm btn-primary';
            login.type = 'button';
            login.textContent = t('account.center_login');
            login.addEventListener('click', () => showAccountLogin('netease'));
            actions.appendChild(login);
        }
    }
}

/** The Bilibili card of the account center. */
async function refreshBiliAccountCard() {
    const state_ = document.getElementById('ac-bili-state');
    if (!state_) return;
    const profileBox = document.getElementById('ac-bili-profile');
    const actions = document.getElementById('ac-bili-actions');
    const card = accountCenter.bilibili;
    let info = null;
    try {
        if (bridgeHas('get_bilibili_account')) {
            const result = await state.api.get_bilibili_account();
            if (result && result.success) info = result;
        }
    } catch (err) {
        info = null;
    }
    card.loggedIn = !!(info && info.logged_in);
    card.nickname = info ? (info.uname || '') : '';
    card.level = info ? (info.level ? String(info.level) : '') : '';
    card.vip = info ? (info.vip_label || '') : '';
    card.avatar = info ? (info.face || '') : '';
    state_.textContent = card.loggedIn
        ? t('account.center_signed_in') : t('account.center_signed_out');
    state_.className = 'ac-card-state' + (card.loggedIn ? ' is-on' : '');
    if (profileBox) {
        profileBox.hidden = !card.loggedIn;
        const face = document.getElementById('ac-bili-face');
        if (face) {
            if (card.avatar) {
                face.src = biliImageSrc(card.avatar);
                face.hidden = false;
                face.addEventListener('error', () => { face.hidden = true; });
            } else {
                face.removeAttribute('src');
                face.hidden = true;
            }
        }
        const name = document.getElementById('ac-bili-name');
        if (name) name.textContent = card.nickname || (info && info.mid) || '';
        const meta = document.getElementById('ac-bili-meta');
        if (meta) {
            const parts = [];
            if (card.level) parts.push(t('bili.level', { level: card.level }));
            if (card.vip) parts.push(t('bili.vip', { name: card.vip }));
            meta.textContent = parts.join(' · ');
        }
    }
    if (actions) {
        actions.innerHTML = '';
        if (card.loggedIn) {
            const messages = document.createElement('button');
            messages.className = 'btn btn-sm btn-secondary';
            messages.type = 'button';
            messages.textContent = t('bili.tab_messages');
            messages.addEventListener('click', () => {
                closeModal('account-center-modal');
                openBiliMessages();
            });
            actions.appendChild(messages);
            const upload = document.createElement('button');
            upload.className = 'btn btn-sm btn-secondary';
            upload.type = 'button';
            upload.textContent = t('bili.tab_upload');
            upload.addEventListener('click', () => {
                closeModal('account-center-modal');
                openBiliUpload();
            });
            actions.appendChild(upload);
            if (info && info.mid) {
                const space = document.createElement('button');
                space.className = 'btn btn-sm btn-secondary';
                space.type = 'button';
                space.textContent = t('bili.tab_space');
                space.addEventListener('click', () => {
                    closeModal('account-center-modal');
                    openBiliSpace(info.mid);
                });
                actions.appendChild(space);
            }
            const logout = document.createElement('button');
            logout.className = 'btn btn-sm btn-secondary';
            logout.type = 'button';
            logout.textContent = t('account.center_logout');
            logout.addEventListener('click', async () => {
                await logoutBili();
                await refreshAccountCenter();
            });
            actions.appendChild(logout);
        } else {
            const login = document.createElement('button');
            login.className = 'btn btn-sm btn-primary';
            login.type = 'button';
            login.textContent = t('account.center_login');
            login.addEventListener('click', () => showAccountLogin('bilibili'));
            actions.appendChild(login);
        }
    }
}

/**
 * The header button: the faces of whoever is signed in, and the way into
 * the account center.
 */
function renderAccountCenterButton() {
    const button = document.getElementById('btn-account-center');
    if (!button) return;
    button.innerHTML = '';
    const add = (avatar, isBili) => {
        const face = document.createElement('img');
        face.className = 'account-center-face';
        face.alt = '';
        face.loading = 'lazy';
        if (avatar) {
            face.src = isBili ? biliImageSrc(avatar) : proxyCoverUrl(avatar);
            face.addEventListener('error', () => { face.remove(); });
        }
        button.appendChild(face);
    };
    if (accountCenter.netease.loggedIn) add(accountCenter.netease.avatar, false);
    if (accountCenter.bilibili.loggedIn) add(accountCenter.bilibili.avatar, true);
    const label = document.createElement('span');
    label.textContent = t('account.center_title');
    button.appendChild(label);
    const names = [];
    if (accountCenter.netease.loggedIn) names.push(accountCenter.netease.nickname);
    if (accountCenter.bilibili.loggedIn) names.push(accountCenter.bilibili.nickname);
    button.title = names.length ? names.join(' · ') : t('account.center_title');
}

function updateLoginBadge(status) {
    status = status || {};
    const account = accountCenter.netease;
    account.loggedIn = !!(status.is_logged_in && status.user);
    account.nickname = account.loggedIn ? (status.user.nickname || '') : '';
    account.vip = account.loggedIn && status.user.vip_type > 0 ? 'VIP' : '';
    account.avatar = account.loggedIn ? (status.user.avatar_url || '') : '';
    renderAccountCenterButton();
    const card = document.getElementById('ac-netease-state');
    if (card && !document.getElementById('account-center-modal').hidden) {
        refreshNeteaseAccountCard();
    }
}

function openLoginModal() {
    openAccountCenter('netease');
}

function switchLoginTab(tab) {
    document.querySelectorAll('#login-tabs .tab').forEach((el) =>
        el.classList.toggle('is-active', el.dataset.tab === tab));
    document.querySelectorAll('#ac-netease-login .tab-panel').forEach((el) =>
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
        closeModal('account-center-modal');
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
        hires_enabled: document.getElementById('hires-toggle').checked,
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
        hires_enabled: download.hires_enabled === true,
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

/**
 * The view that is on screen right now.
 *
 * The header tabs need it to know which section the user is in: the same
 * tab means "play" in the NetEase half and something else in the Bilibili
 * half, so the click handler has to know where it is coming from.
 */
let lastView = 'home';

/** Switch between the download and the player view. */
function showView(name) {
    lastView = name;
    // Only the header tabs follow the view; the segmented control inside the
    // search page carries the class too and must keep its own state.
    document.querySelectorAll('#top-tabs .top-tab').forEach((tab) =>
        tab.classList.toggle('is-active', tab.dataset.view === name
            || tab.dataset.biliView === name));
    document.querySelectorAll('.view').forEach((view) =>
        view.classList.toggle('is-active', view.id === 'view-' + name));
    syncTopTabs(name);
    // Always refresh the current source when the player view becomes active.
    // When the queue was non-empty (e.g. a restored session) we used to skip
    // this reload, so #playlist-list / #track-list stayed at whatever stale
    // content the previous render left behind and the user opened the player
    // tab to a blank screen.  selectSource itself is idempotent so calling
    // it again on the same source is safe.
    if (name === 'player') {
        selectSource(player.sourceTab);
    }
    // The download page is drawn from the queue, which may have changed
    // while another view was on screen.
    if (name === 'bili-download') {
        renderBiliDownloadQueue();
    }
    // The Bangumi page re-reads whichever tab it is on: the local favourites
    // and the history may have changed in another view.
    // Pixiv 页自己管自己的六个标签：切回来时把它当前那一页刷新一遍，
    // 不然看到的是上一次留下的旧列表。
    if (name === 'pixiv' && window.PixivUI) {
        PixivUI.enter();
    }
    if (name === 'bangumi') {
        // 播放器在详情面板里，切走就停掉。
        closeBangumiPlayer();
        if (bangumiState.tab === 'favorites') loadBangumiFavorites(true);
        else if (bangumiState.tab === 'history') loadBangumiHistory(true);
    }
}

/**
 * Which section the user is in: home, the NetEase half or the Bilibili half.
 *
 * The header tabs are not the same everywhere -- the Bilibili section has its
 * own (space, messages) and a first start has no section at all -- so the set
 * of visible tabs follows the view instead of being fixed in the markup.
 */
function currentSection(name) {
    if (name === 'bili' || name === 'bili-download') return 'bili';
    if (name === 'bangumi') return 'bangumi';
    if (name === 'pixiv') return 'pixiv';
    if (name === 'home' || name === 'search') return name === 'search' ? 'search' : 'home';
    return 'netease';
}

/** Show the tabs that belong to the section the user is in. */
function syncTopTabs(name) {
    const section = currentSection(name);
    const tabs = document.getElementById('top-tabs');
    if (tabs) {
        tabs.querySelectorAll('.top-tab').forEach((tab) => {
            const sections = (tab.dataset.sections || '').split(' ').filter(Boolean);
            // On the home page and in the combined search nothing is shown:
            // there is no section to switch within yet.
            const show = section !== 'home' && section !== 'search'
                && sections.indexOf(section) >= 0;
            tab.hidden = !show;
        });
    }
    const exit = document.getElementById('btn-exit-section');
    if (exit) exit.hidden = section === 'home';
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
        // -- that is what "let me find it in my own files" means.
        await loadLocal(more);
        return;
    }
    if (!keyword) {
        showToast('warning', t('player.search_empty'));
        return;
    }
    // There is one search now and it covers both sources, so the box in the
    // sidebar only carries the keyword over to the search page.
    await openAggregateSearch(keyword);
}

/**
 * The one search: NetEase and Bilibili in the same result list.
 *
 * The sidebar used to search NetEase on its own and drop the songs into
 * the playlist column, which made the two sources feel like two programs.
 * Now the keyword goes to the search page, where both sources answer and
 * the filters (source, kind, length, order) work on the mixed list.
 */
async function openAggregateSearch(keyword) {
    const text = String(keyword || '').trim();
    if (!text) return;
    showView('search');
    const input = document.getElementById('aggr-input');
    if (input) {
        input.value = text;
        input.focus();
    }
    await runCombinedSearch();
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
            openAggregateSearch(item.dataset.value);
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
 * A Bilibili image URL, served through the loopback server.
 *
 * The webview does not talk to the Bilibili CDN on its own: the images
 * (covers and avatars live on ``*.hdslb.com``) are fetched by the backend
 * and handed over from there, the same way the sound is.  A direct CDN URL
 * in an ``<img>`` tag simply comes up empty.
 */
function biliImageSrc(url) {
    const raw = String(url || '').trim();
    if (!raw) return '';
    if (!raw.startsWith('http://') && !raw.startsWith('https://')) return raw;
    const base = state.mediaBaseUrl;
    if (!base) return '';
    return base + '/bili?u=' + encodeURIComponent(raw);
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
        setListTitle(t('player.source_' + tab));
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

/** The numbers next to the Bilibili sliders. */
function updateBiliSettingsLabels() {
    const size = document.getElementById('bili-danmaku-size-value');
    if (size) {
        size.textContent = document.getElementById('bili-danmaku-size').value + 'px';
    }
    const opacity = document.getElementById('bili-danmaku-opacity-value');
    if (opacity) {
        opacity.textContent = document.getElementById('bili-danmaku-opacity').value + '%';
    }
}

/** The account line in the Bilibili settings. */
function renderBiliSettingsAccount() {
    const line = document.getElementById('bili-settings-account');
    if (!line) return;
    if (biliAccount.loggedIn) {
        line.textContent = t('bili.settings_signed_in', {
            name: biliAccount.uname || biliAccount.mid || ''
        });
    } else {
        line.textContent = t('bili.settings_signed_out');
    }
    const login = document.getElementById('btn-bili-settings-login');
    const logout = document.getElementById('btn-bili-settings-logout');
    if (login) login.hidden = biliAccount.loggedIn;
    if (logout) logout.hidden = !biliAccount.loggedIn;
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
        const view = tab.dataset.view;
        if (view === 'bili-space' || view === 'bili-messages') {
            // These two live inside the Bilibili section as panels of their own;
            // the header tab is the way in, the panel is the way back out.
            showView('bili');
            if (view === 'bili-space') openBiliSpace(biliAccount.mid || '');
            else openBiliMessages();
            return;
        }
        // 「播放」和「下载」在 B站分区里指向 B站自己的页面：那两个视图是
        // 网易云的，直接跳过去就等于把用户踢回另一个分区。
        const section = currentSection(lastView);
        const target = (section === 'bili' && tab.dataset.biliView)
            ? tab.dataset.biliView : view;
        showView(target);
        // The section picker refreshes its counters every time it is opened.
        if (view === 'home') showHome();
    });

    const exitSection = document.getElementById('btn-exit-section');
    if (exitSection) {
        exitSection.addEventListener('click', () => {
            showView('home');
            showHome();
        });
    }

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
    // One button for both accounts now; it opens the account center.
    const accountButton = document.getElementById('btn-account-center');
    if (accountButton) {
        accountButton.addEventListener('click', () => openAccountCenter());
    }

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
    const biliDir = document.getElementById('btn-bili-download-dir');
    if (biliDir) biliDir.addEventListener('click', chooseBiliDirectory);
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
    // Hi-Res 开关：改完立刻存盘。它同时管下载和在线播放——关掉之后
    // 请求停在无损，不去试那首可能根本没有 Hi-Res 的歌。
    const hiresToggle = document.getElementById('hires-toggle');
    if (hiresToggle) {
        hiresToggle.addEventListener('change', async () => {
            const enabled = hiresToggle.checked;
            if (state.settings && state.settings.download) {
                state.settings.download.hires_enabled = enabled;
            }
            await state.api.update_settings('download', { hires_enabled: enabled });
        });
    }
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
        // Cmd/Ctrl+F goes to the one search, whatever is on screen.
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') {
            const tag = (event.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea') return;
            event.preventDefault();
            showView('search');
            const input = document.getElementById('aggr-input');
            if (input) input.focus();
            return;
        }
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
    closeModal('account-center-modal');
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
    // The Bilibili partition (its rid) that the search is narrowed down to.
    partition: 0,
    rows: [],
    neteaseOffset: 0,
    biliPage: 1,
    neteaseDone: false,
    biliDone: false,
    // 番剧/影视（PGC）只有第一页：聚合列表里的翻页由网易云和 B站视频两半驱动。
    pgcDone: false,
    neteaseError: '',
    biliError: '',
    pgcError: '',
    loading: false,
    requestId: 0
};

/**
 * The Bilibili partitions as a filter of the search.
 *
 * The Bilibili half of the search can be narrowed down to one partition,
 * the same way the Bilibili section itself works.  The row only shows up
 * when the Bilibili source is in the mix -- filtering NetEase by a
 * Bilibili partition would make no sense.
 */
async function renderCombinedPartitions() {
    const row = document.getElementById('aggr-bili-partitions');
    if (!row) return;
    if (combined.source === 'netease') {
        row.hidden = true;
        row.innerHTML = '';
        return;
    }
    row.hidden = false;
    await loadBiliPartitions();
    row.innerHTML = '';
    const hot = document.createElement('button');
    hot.className = 'chip' + (combined.partition ? '' : ' is-active');
    hot.type = 'button';
    hot.textContent = t('bili.chip_trending');
    hot.setAttribute('aria-pressed', combined.partition ? 'false' : 'true');
    hot.addEventListener('click', () => {
        combined.partition = 0;
        // runCombinedSearch() and not renderCombinedSearch(): the latter
        // needs the keyword handed to it, and without one it empties the
        // search instead of narrowing it down.
        runCombinedSearch();
    });
    row.appendChild(hot);
    (biliSection.partitions || []).forEach((partition) => {
        if (!partition || !partition.rid) return;
        const key = BILI_CHIP_KEYS[partition.key];
        const label = key ? t(key) : (partition.name || String(partition.rid));
        const chip = document.createElement('button');
        chip.className = 'chip' + (Number(combined.partition) === Number(partition.rid)
            ? ' is-active' : '');
        chip.type = 'button';
        chip.textContent = label;
        chip.setAttribute('aria-pressed',
            Number(combined.partition) === Number(partition.rid) ? 'true' : 'false');
        chip.addEventListener('click', () => {
            combined.partition = partition.rid;
            runCombinedSearch();
        });
        row.appendChild(chip);
    });
}

/** The Bilibili section: the partition that is open and what it holds. */
const biliSection = {
    rid: 0,
    page: 1,
    // Which batch of the recommendation feed is on screen.  The feed is
    // pulled with a growing fresh index, so "more" is a new batch and not
    // the same one again.
    recommendPage: 0,
    items: [],
    partitions: [],
    error: '',
    loading: false,
    exhausted: false,
    // 'recommend' is the B站 feed; 'hot' is the partition list.
    tab: 'recommend'
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
    const bangumiLine = document.getElementById('home-count-bangumi');
    const foot = document.getElementById('home-foot');
    if (neteaseLine) {
        neteaseLine.textContent = '--';
        neteaseLine.classList.remove('is-empty');
    }
    if (biliLine) {
        biliLine.textContent = '--';
        biliLine.classList.remove('is-empty');
    }
    if (bangumiLine) {
        bangumiLine.textContent = '--';
        bangumiLine.classList.remove('is-empty');
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

    // Bangumi: the number of locally saved favourites.  No request to a
    // server -- the list is a file on this machine.
    if (bangumiLine) {
        if (!bridgeHas('get_bangumi_favorites')) {
            bangumiLine.classList.add('is-empty');
        } else {
            try {
                const result = await state.api.get_bangumi_favorites();
                if (result && result.success) {
                    const count = (result.items || []).length;
                    bangumiLine.textContent = t('bangumi.tab_favorites') + ' ' + count;
                } else {
                    bangumiLine.classList.add('is-empty');
                }
            } catch (err) {
                bangumiLine.classList.add('is-empty');
            }
        }
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
    if (entry === 'local-video') {
        showSection('local-video');
        // The library is read again on every entry: the folders may have
        // gained a file since the page was last looked at.
        loadLocalVideoLibrary(true);
        return;
    }
    if (entry === 'bili') {
        showSection('bili');
        // The B站 section opens on the recommendation feed; the hot list is one
        // tab away.  The feed is what makes the section feel like it knows the
        // user, and the hot list is fine as a fallback.
        setBiliTab(biliSection.tab || 'recommend');
        // The account bar is asked for every time the page is opened: the
        // session may have ended in the meantime.
        loadBiliAccount();
        focusPageTitle('bili-title');
        return;
    }
    if (entry === 'pixiv') {
        showSection('pixiv');
        if (window.PixivUI) PixivUI.enter();
        focusPageTitle('pixiv-title');
        return;
    }
    if (entry === 'bangumi') {
        showSection('bangumi');
        // The Bangumi page has no account, so there is nothing to load up
        // front: the search box is where the user starts.
        setBangumiTab(bangumiState.tab || 'search');
        const input = document.getElementById('bangumi-search-input');
        if (byKeyboard && input) input.focus();
        else focusPageTitle('bangumi-title');
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
    lastView = name;
    document.querySelectorAll('.view').forEach((view) => {
        view.classList.toggle('is-active', view.id === 'view-' + name);
    });
    // Which header tabs exist (and which of them reads as active) is decided
    // by the section, not by the view: see syncTopTabs.
    document.querySelectorAll('#top-tabs .top-tab').forEach((tab) => {
        tab.classList.toggle('is-active', tab.dataset.view === name
            || tab.dataset.biliView === name);
    });
    syncTopTabs(name);
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
        duration: duration ? duration.value : 'all',
        partition: combined.partition || 0
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
    combined.partition = 0;
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
    if (row.kind === 'pgc') {
        badge.textContent = t('search.pgc_badge');
    } else {
        badge.textContent = row.source === 'bilibili'
            ? t('search.source_bili') : t('search.source_netease');
    }
    nameLine.appendChild(badge);
    main.appendChild(nameLine);
    const sub = document.createElement('div');
    sub.className = 'result-sub';
    if (row.kind === 'pgc') {
        // 番剧的「副标题」是类型、集数、评分和风格，不是上传者和播放量。
        const parts = [row.pgc.typeName, row.pgc.indexShow];
        if (row.pgc.score > 0) parts.push(t('search.pgc_score', { score: row.pgc.score.toFixed(1) }));
        if (row.pgc.styles.length) parts.push(row.pgc.styles.slice(0, 3).join(' / '));
        sub.textContent = parts.filter(Boolean).join(' · ');
    } else {
        sub.textContent = row.source === 'bilibili'
            ? t('search.sub_bili', { uploader: row.artists, plays: formatPlayCount(row.plays) })
            : t('search.sub_netease', { artist: row.artists, album: row.album });
    }
    main.appendChild(sub);
    element.appendChild(main);

    const duration = document.createElement('span');
    duration.className = 'result-dur';
    duration.textContent = row.durationMs ? fmtTime(row.durationMs / 1000) : '';
    element.appendChild(duration);

    // The row itself opens the result, but a video that only opens in the
    // browser used to look like a dead end.  The button says what it does.
    const play = document.createElement('button');
    play.className = 'result-play';
    play.type = 'button';
    play.setAttribute('aria-label', t('player.play'));
    play.setAttribute('data-icon', 'play');
    setIcon(play, 'play');
    play.addEventListener('click', (event) => {
        event.stopPropagation();
        openCombinedRow(row);
    });
    element.appendChild(play);

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
    if (row.kind === 'pgc') {
        // 番剧没有 bvid，播不了画面也就没有意义：先开选集，选哪集播哪集。
        openBiliBangumiEpisodes({
            season_id: row.pgc.seasonId,
            ep_id: 0,
            title: row.pgc.title,
            cover: row.pgc.cover
        });
        return;
    }
    if (row.source === 'bilibili') {
        // A video found by the search plays right here: in the player window
        // or in the modal, whichever the settings ask for.  Jumping out to
        // the browser left the result unplayable from the app.
        playBiliTrack(row.track);
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
        // 番剧/影视只在「全部」里出现：挑了「歌曲」「视频」这些类别还塞进来
        // 就变成了不受筛选影响的东西。
        if (row.kind === 'pgc' && category !== 'all') return false;
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
        const exhausted = combined.neteaseDone && combined.biliDone && combined.pgcDone;
        more.hidden = exhausted || combined.loading;
        more.textContent = combined.loading ? t('common.loading') : t('search.load_more');
        more.disabled = combined.loading;
    }
    if (end) {
        // The button gives way to a line of text: "no more" or "too many".
        // It only appears when there is a list to look at.
        const exhausted = combined.neteaseDone && combined.biliDone && combined.pgcDone;
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
        // The partition narrows the Bilibili half of the search down; 0
        // means "every partition", which is what the interface calls the
        // hot list.
        const partition = combined.partition ? String(combined.partition) : 'all';
        const result = await state.api.search_bilibili_tracks(
            combined.keyword, partition, biliOrderFor(combined.sort), combined.biliPage, 20);
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

/** Ask the Bilibili PGC catalogue (番剧 / 影视) once per search. */
async function loadCombinedPgc(requestId) {
    if (combined.pgcDone) return;
    try {
        if (!bridgeHas('search_bilibili_pgc')) {
            combined.pgcError = t('error.unknown');
            combined.pgcDone = true;
            return;
        }
        const result = await state.api.search_bilibili_pgc(combined.keyword, 1);
        if (requestId !== combined.requestId) return;
        if (!result || !result.success) {
            combined.pgcError = bridgeFailure(result, 'error.network');
            combined.pgcDone = true;
            return;
        }
        const items = result.items || [];
        // 番剧排在视频前面：搜一个番剧名，正片比二创更该先出现。
        items.forEach((item) => combined.rows.unshift(biliPgcRow(item)));
        combined.pgcDone = true;
    } catch (err) {
        if (requestId !== combined.requestId) return;
        combined.pgcError = t('error.network');
        combined.pgcDone = true;
    }
}

/** One row of the PGC catalogue: 番剧 / 国创 / 影视. */
function biliPgcRow(item) {
    const pgc = item || {};
    return {
        source: 'bilibili',
        kind: 'pgc',
        id: String(pgc.season_id || ''),
        name: pgc.title || '',
        artists: pgc.season_type_name || '',
        album: (pgc.styles || []).slice(0, 2).join(' / '),
        cover: pgc.cover || '',
        durationMs: 0,
        plays: 0,
        pgc: {
            seasonId: Number(pgc.season_id) || 0,
            title: pgc.title || '',
            cover: pgc.cover || '',
            indexShow: pgc.index_show || '',
            typeName: pgc.season_type_name || '',
            styles: pgc.styles || [],
            areas: pgc.areas || [],
            score: Number(pgc.score) || 0,
            url: pgc.url || ''
        }
    };
}

/** Fetch the next page of every source that has one. */
async function loadCombinedPage() {
    const requestId = combined.requestId + 1;
    combined.requestId = requestId;
    combined.loading = true;
    renderCombinedRows();
    const wanted = [];
    if (combined.source !== 'bilibili') wanted.push(loadCombinedNetease(requestId));
    if (combined.source !== 'netease') {
        wanted.push(loadCombinedBili(requestId));
        wanted.push(loadCombinedPgc(requestId));
    }
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
    combined.partition = Number(options.partition) || 0;
    // An empty call must not throw away the keyword that is already in
    // the box: the search is still there, it just was not asked for again.
    combined.keyword = text || combined.keyword;
    combined.rows = [];
    combined.neteaseOffset = 0;
    combined.biliPage = 1;
    combined.neteaseDone = false;
    combined.biliDone = false;
    combined.pgcDone = false;
    combined.neteaseError = '';
    combined.biliError = '';
    combined.pgcError = '';
    // A source that is filtered out is finished by definition: without this
    // the "no more" line would never show when only one source is asked.
    if (combined.source === 'bilibili') combined.neteaseDone = true;
    if (combined.source === 'netease') {
        combined.biliDone = true;
        combined.pgcDone = true;
    }
    updateFilterSelects();
    updateFilterChips();
    await renderCombinedPartitions();
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

/** One video card: the picture opens the page, the buttons play and download. */
function biliCardElement(track, options) {
    const opts = options || {};
    const wrap = document.createElement('div');
    wrap.className = 'bili-card-wrap';
    // 收藏夹里可以勾选：勾上几个，底下的批量条才出现。复选框放在卡片
    // 外面——按钮里塞按钮是不合法的标记。
    if (opts.selectable) {
        const pick = document.createElement('input');
        pick.type = 'checkbox';
        pick.className = 'bili-card-check';
        pick.checked = !!opts.checked;
        pick.setAttribute('aria-label', track.name || '');
        pick.addEventListener('click', (event) => event.stopPropagation());
        pick.addEventListener('change', () => {
            if (typeof opts.onSelect === 'function') opts.onSelect(!!pick.checked);
        });
        wrap.appendChild(pick);
    }
    const card = document.createElement('button');
    card.className = 'video-card';
    card.type = 'button';
    card.setAttribute('aria-label', track.name || '');
    const thumb = document.createElement('div');
    thumb.className = 'vc-thumb';
    if (track.cover_url) {
        const image = document.createElement('img');
        image.src = biliImageSrc(track.cover_url);
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
    // The uploader's name opens their page, when the list knows who it is.
    if (track.author_mid) {
        const link = document.createElement('button');
        link.className = 'vc-up is-link';
        link.type = 'button';
        link.textContent = t('bili.by', { name: track.artists || '' });
        link.addEventListener('click', (event) => {
            event.stopPropagation();
            openBiliSpace(track.author_mid);
        });
        sub.appendChild(link);
    } else {
        const up = document.createElement('span');
        up.className = 'vc-up';
        up.textContent = t('bili.by', { name: track.artists || '' });
        sub.appendChild(up);
    }
    const plays = document.createElement('span');
    plays.className = 'vc-plays';
    plays.textContent = t('bili.plays', { count: formatPlayCount(track.play_count) });
    sub.appendChild(plays);
    card.appendChild(sub);
    card.addEventListener('click', () => {
        const url = track.url || '';
        if (url) state.api.open_external(url);
    });
    wrap.appendChild(card);
    // The buttons sit next to the card, not inside it: a button inside a
    // button is invalid markup and the click would open the wrong thing.
    const actions = document.createElement('div');
    actions.className = 'bili-card-actions';
    const playButton = document.createElement('button');
    playButton.className = 'btn btn-sm btn-secondary';
    playButton.type = 'button';
    playButton.textContent = t('bili.play');
    playButton.addEventListener('click', () => playBiliTrack(track));
    const downloadButton = document.createElement('button');
    downloadButton.className = 'btn btn-sm btn-secondary';
    downloadButton.type = 'button';
    downloadButton.textContent = t('bili.download_add');
    downloadButton.addEventListener('click', () => queueBiliDownload(track));
    actions.appendChild(playButton);
    actions.appendChild(downloadButton);
    wrap.appendChild(actions);
    return wrap;
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
        // The recommendation feed has pages too, so the button is not just
        // for the partition list.
        const paged = biliSection.tab === 'hot' || biliSection.tab === 'recommend';
        more.hidden = !paged || biliSection.exhausted || biliSection.loading;
        more.textContent = biliSection.loading ? t('common.loading') : t('bili.load_more');
        more.disabled = biliSection.loading;
    }
    const end = document.getElementById('bili-end');
    if (end) {
        // The button gives way to a line of text when the list is done.
        const paged = biliSection.tab === 'hot' || biliSection.tab === 'recommend';
        end.hidden = !paged || biliSection.loading
            || !biliSection.items.length || !biliSection.exhausted;
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
    renderBiliDetailButtons();
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
    // The recommendation feed pages with its own growing fresh index, not
    // with the page number of a partition.
    if (biliSection.tab === 'recommend') {
        await loadBiliRecommend(false);
        return;
    }
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

/* ------------------------------------------- bilibili account and favourites */

/** The Bilibili account as the backend last reported it. */
const biliAccount = {
    loggedIn: false,
    mid: 0,
    uname: '',
    face: '',
    level: 0,
    vipLabel: ''
};

/** The login panel: the open method and the captcha that is pending. */
const biliLogin = {
    method: 'qr',
    timer: null,
    captchaKey: '',
    // The key the SMS send step handed out.  The login has to send it back,
    // otherwise Bilibili answers 86205（验证码失效）.
    smsCaptchaKey: '',
    // The phone number the code was sent to: a key only belongs to that one.
    smsTel: '',
    // The page Bilibili wants the slider solved on, when the risk control
    // fires before the code is sent.
    smsRiskUrl: '',
    gt: '',
    challenge: '',
    token: ''
};

/** The account's own favourites: the folders and the open one. */
/** 每页条数的选项。上限 40 是接口给的：ps=41 回来是 code -400。 */
const BILI_FAV_PAGE_SIZES = [10, 20, 30, 40];
const BILI_FAV_PAGE_SIZE_DEFAULT = 30;

const biliFavorites = {
    folders: [],
    folderId: null,
    /** 当前是第几页（从 1 开始）。 */
    page: 1,
    /** 每页多少条，界面可选，最大 40。 */
    pageSize: BILI_FAV_PAGE_SIZE_DEFAULT,
    /** 整个收藏夹的总条数；接口没说时是 0。 */
    total: 0,
    /** 只有当前这一页的内容，换页就整页替换。 */
    items: [],
    hasMore: false,
    loading: false,
    error: '',
    loaded: false,
    /** 勾选的视频（bvid）；换页和换收藏夹都清空。 */
    selected: new Set()
};

/** The video player: the open track, the streams and the chosen quality. */
const biliPlayer = {
    track: null,
    streams: null,
    quality: 0,
    title: '',
    author: '',
    authorMid: ''
};

/** The value of one of the login inputs. */
function biliInputValue(id) {
    const el = document.getElementById(id);
    return el ? String(el.value || '').trim() : '';
}

/**
 * The text of a login status.
 *
 * An unknown status has no key, so the caller falls back to the message the
 * backend sent.
 */
function biliStatusText(status) {
    const key = 'bili.status_' + (status || '');
    const text = t(key);
    return text === key ? '' : text;
}

/**
 * The message of a failed Bilibili call.
 *
 * -101 is "not signed in": it gets its own text, because the user has to log
 * in first, not retry.
 */
function biliFailure(result, fallbackKey) {
    if (!result) return t(fallbackKey || 'error.unknown');
    const code = result.code !== undefined && result.code !== null
        ? result.code : result.error_code;
    if (Number(code) === -101) return t('bili.login_required');
    return result.message || t(fallbackKey || 'error.unknown');
}

/**
 * The Bilibili account in the header.
 *
 * The NetEase account has its own button up there, and the Bilibili account
 * used to be visible only inside the Bilibili page -- so the header looked
 * like the app had one account.  Both belong there.
 */
function renderBiliHeaderAccount() {
    // The header has one account button now and it opens the account
    // center, so all this has to do is keep the center's copy of the
    // Bilibili account up to date.
    const card = accountCenter.bilibili;
    card.loggedIn = !!biliAccount.loggedIn;
    card.nickname = biliAccount.uname || (biliAccount.mid ? String(biliAccount.mid) : '');
    card.level = biliAccount.level ? String(biliAccount.level) : '';
    card.vip = biliAccount.vipLabel || '';
    card.avatar = biliAccount.face || '';
    renderAccountCenterButton();
}

/** The account bar: the avatar, the name, the level and the way out. */
function renderBiliAccount() {
    const face = document.getElementById('bili-account-face');
    const name = document.getElementById('bili-account-name');
    const meta = document.getElementById('bili-account-meta');
    const actions = document.getElementById('bili-account-actions');
    if (!name || !meta || !actions) return;
    actions.innerHTML = '';
    // The name is the way into the account center from here.
    name.classList.add('is-link');
    name.onclick = () => openAccountCenter();
    if (biliAccount.loggedIn) {
        if (face) {
            face.src = biliImageSrc(biliAccount.face);
            face.hidden = !biliAccount.face;
        }
        // Without a nickname the account number is the only thing left.
        name.textContent = biliAccount.uname
            || t('bili.signed_in', { name: biliAccount.mid });
        const parts = [];
        if (biliAccount.level) parts.push(t('bili.level', { level: biliAccount.level }));
        if (biliAccount.vipLabel) parts.push(t('bili.vip', { name: biliAccount.vipLabel }));
        meta.textContent = parts.join(' · ');
        // The three things that need the account: the messages, the upload
        // and the own page.
        const messages = document.createElement('button');
        messages.className = 'btn btn-sm btn-secondary';
        messages.type = 'button';
        messages.textContent = t('bili.tab_messages');
        messages.addEventListener('click', openBiliMessages);
        actions.appendChild(messages);
        const upload = document.createElement('button');
        upload.className = 'btn btn-sm btn-secondary';
        upload.type = 'button';
        upload.textContent = t('bili.tab_upload');
        upload.addEventListener('click', openBiliUpload);
        actions.appendChild(upload);
        if (biliAccount.mid) {
            const space = document.createElement('button');
            space.className = 'btn btn-sm btn-secondary';
            space.type = 'button';
            space.textContent = t('bili.tab_space');
            space.addEventListener('click', () => openBiliSpace(biliAccount.mid));
            actions.appendChild(space);
        }
        // Who this account follows, and who follows it.
        const relations = document.createElement('button');
        relations.className = 'btn btn-sm btn-secondary';
        relations.type = 'button';
        relations.textContent = t('bili.relations');
        relations.addEventListener('click', () => openBiliRelations());
        actions.appendChild(relations);
        const logout = document.createElement('button');
        logout.className = 'btn btn-sm btn-secondary';
        logout.type = 'button';
        logout.textContent = t('bili.logout');
        logout.addEventListener('click', logoutBili);
        actions.appendChild(logout);
        return;
    }
    if (face) {
        face.removeAttribute('src');
        face.hidden = true;
    }
    name.textContent = t('bili.signed_out');
    meta.textContent = '';
    const login = document.createElement('button');
    login.className = 'btn btn-sm btn-primary';
    login.type = 'button';
    login.textContent = t('bili.login_button');
    login.addEventListener('click', openBiliLoginModal);
    actions.appendChild(login);
}

/** Read the account from the backend and draw the bar. */
async function loadBiliAccount() {
    if (!bridgeHas('get_bilibili_account')) {
        renderBiliAccount();
        return;
    }
    try {
        const result = await state.api.get_bilibili_account();
        if (result && result.success) {
            biliAccount.loggedIn = !!result.logged_in;
            biliAccount.mid = result.mid || 0;
            biliAccount.uname = result.uname || '';
            biliAccount.face = result.face || '';
            biliAccount.level = result.level || 0;
            biliAccount.vipLabel = result.vip_label || '';
        } else {
            biliAccount.loggedIn = false;
        }
    } catch (err) {
        biliAccount.loggedIn = false;
    }
    renderBiliAccount();
    renderBiliHeaderAccount();
    renderBiliSettingsAccount();
    // The favourites need the account, so they are read again when the tab
    // is open and the account has just changed.
    if (biliSection.tab === 'favorites') loadBiliFolders();
}

/* ------------------------------------------------------------ bili login */

/** Open the login panel. */
function openBiliLoginModal() {
    openAccountCenter('bilibili');
}

/** One of the five ways to sign in. */
function switchBiliLoginTab(tab) {
    biliLogin.method = tab;
    document.querySelectorAll('#bili-login-tabs .tab').forEach((el) =>
        el.classList.toggle('is-active', el.dataset.tab === tab));
    document.querySelectorAll('#ac-bili-login .tab-panel').forEach((el) =>
        el.classList.toggle('is-active', el.dataset.panel === tab));
    // Only the QR code is polled; the other methods are one-shot requests.
    stopBiliLoginPolling();
    if (tab === 'qr') refreshBiliQrcode();
    if (tab === 'password') loadBiliCaptcha();
}

/** Ask for a new QR code and start watching it. */
async function refreshBiliQrcode() {
    const image = document.getElementById('bili-qrcode-image');
    const status = document.getElementById('bili-qrcode-status');
    const refresh = document.getElementById('btn-bili-qrcode-refresh');
    if (!bridgeHas('bili_login_qrcode')) {
        if (status) status.textContent = t('error.unknown');
        return;
    }
    if (refresh) refresh.hidden = true;
    try {
        const result = await state.api.bili_login_qrcode();
        if (!result || !result.success) {
            if (status) status.textContent = biliFailure(result, 'error.unknown');
            return;
        }
        // 二维码是自己画的（后端返回 data URL）：B站给的地址是扫码确认页，
        // 直接塞进 <img> 是什么都显示不出来。
        if (image) {
            image.src = result.qrcode_data_url || biliImageSrc(result.qrcode_url);
        }
        if (status) status.textContent = t('bili.status_waiting');
        startBiliLoginPolling();
    } catch (err) {
        if (status) status.textContent = t('error.network');
    }
}

/** Watch the QR code until it is used, expires or the panel closes. */
function startBiliLoginPolling() {
    stopBiliLoginPolling();
    biliLogin.timer = setInterval(pollBiliLogin, 2000);
    pollBiliLogin();
}

/** Stop watching the QR code. */
function stopBiliLoginPolling() {
    if (biliLogin.timer) {
        clearInterval(biliLogin.timer);
        biliLogin.timer = null;
    }
}

/** One step of the QR login. */
async function pollBiliLogin() {
    const modal = document.getElementById('ac-bili-login');
    if (modal && modal.hidden) {
        stopBiliLoginPolling();
        return;
    }
    if (!bridgeHas('bili_login_poll')) return;
    let result = null;
    try {
        result = await state.api.bili_login_poll();
    } catch (err) {
        return;
    }
    if (!result) return;
    const status = result.status || '';
    const text = document.getElementById('bili-qrcode-status');
    if (text) {
        text.textContent = biliStatusText(status) || result.message || t('bili.status_waiting');
    }
    const refresh = document.getElementById('btn-bili-qrcode-refresh');
    if (refresh) refresh.hidden = status !== 'expired';
    if (status === 'success') {
        stopBiliLoginPolling();
        await finishBiliLogin();
        return;
    }
    if (status === 'expired' || status === 'failed' || status === 'signed_out') {
        stopBiliLoginPolling();
    }
}

/** The login worked: close the panel and show who is signed in. */
async function finishBiliLogin() {
    stopBiliLoginPolling();
    closeModal('account-center-modal');
    showToast('success', t('bili.signed_in_toast'));
    await loadBiliAccount();
}

/** The captcha parameters of the password login. */
async function loadBiliCaptcha() {
    if (!bridgeHas('bili_captcha')) return;
    try {
        const result = await state.api.bili_captcha();
        if (!result || !result.success) return;
        biliLogin.gt = result.gt || '';
        biliLogin.challenge = result.challenge || '';
        biliLogin.token = result.token || '';
        biliLogin.captchaKey = '';
        const token = document.getElementById('bili-captcha-token');
        if (token) token.value = biliLogin.token;
    } catch (err) {
        // No captcha today: the password login works without one.
    }
}

/** Sign in with the account and the password. */
async function submitBiliPassword() {
    const username = biliInputValue('bili-username');
    const password = biliInputValue('bili-password');
    const captcha = biliInputValue('bili-captcha');
    if (!bridgeHas('bili_login_password')) {
        showToast('error', t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.bili_login_password(
            username, password, biliLogin.captchaKey, captcha, biliLogin.challenge, '', '');
        if (!result || !result.success) {
            if (result && result.captcha_required) {
                // The backend asks for a captcha: keep its key for the next try.
                biliLogin.captchaKey = result.captcha_key || '';
                showToast('warning', result.message || t('bili.captcha_hint'), 9000);
                return;
            }
            showToast('error', biliFailure(result, 'bili.status_failed'));
            return;
        }
        await finishBiliLogin();
    } catch (err) {
        showToast('error', t('bili.status_failed'));
    }
}

/** Show or hide the hint about the slider Bilibili asks for. */
function setBiliSmsRisk(visible, url) {
    const block = document.getElementById('bili-sms-risk');
    if (block) block.hidden = !visible;
    biliLogin.smsRiskUrl = visible ? (url || '') : '';
}

/** Ask for the SMS code of the login. */
async function sendBiliSmsCode() {
    const tel = biliInputValue('bili-phone');
    if (!bridgeHas('bili_send_sms_code')) {
        showToast('error', t('error.unknown'));
        return;
    }
    // A code that was not sent leaves no key behind: the old one is dead.
    biliLogin.smsCaptchaKey = '';
    biliLogin.smsTel = '';
    setBiliSmsRisk(false);
    try {
        const result = await state.api.bili_send_sms_code(tel, '86');
        if (!result || !result.success) {
            // The risk control wants the slider before the code is sent.
            if (result && result.captcha_required) {
                setBiliSmsRisk(true, result.recaptcha_url || '');
                showToast('warning', result.message || t('bili.sms_risk_hint'), 9000);
                return;
            }
            showToast('error', biliFailure(result, 'bili.status_failed'));
            return;
        }
        // The key belongs to this phone number and to this code; the login
        // step has to send it back.
        biliLogin.smsCaptchaKey = result.captcha_key || '';
        biliLogin.smsTel = tel;
        showToast('success', result.message || t('bili.send_code'));
    } catch (err) {
        showToast('error', t('error.network'));
    }
}

/** Open the page Bilibili wants the slider solved on. */
function openBiliSmsRiskPage() {
    const url = biliLogin.smsRiskUrl || '';
    if (!url) return;
    if (!state.api || !state.api.open_external) return;
    state.api.open_external(url);
}

/** Leave the SMS login and use the QR code instead. */
function switchToBiliQrLogin() {
    setBiliSmsRisk(false);
    switchBiliLoginTab('qr');
}

/** Sign in with the SMS code. */
async function submitBiliSms() {
    const tel = biliInputValue('bili-phone');
    const code = biliInputValue('bili-sms-code');
    if (!bridgeHas('bili_login_sms')) {
        showToast('error', t('error.unknown'));
        return;
    }
    // The key was handed out for one phone number; a different one has none,
    // and the old key dies with it.
    let captchaKey = '';
    if (biliLogin.smsTel === tel) {
        captchaKey = biliLogin.smsCaptchaKey;
    } else {
        biliLogin.smsCaptchaKey = '';
        biliLogin.smsTel = '';
    }
    try {
        const result = await state.api.bili_login_sms(tel, code, '86', captchaKey);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.status_failed'));
            return;
        }
        await finishBiliLogin();
    } catch (err) {
        showToast('error', t('bili.status_failed'));
    }
}

/** Sign in with a cookie that was copied from the browser. */
async function submitBiliCookie() {
    const cookie = biliInputValue('bili-cookie');
    if (!bridgeHas('bili_login_cookie')) {
        showToast('error', t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.bili_login_cookie(cookie);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.status_failed'));
            return;
        }
        await finishBiliLogin();
    } catch (err) {
        showToast('error', t('bili.status_failed'));
    }
}

/** Ask for the SMS code of the registration. */
async function sendBiliRegisterCode() {
    const tel = biliInputValue('bili-register-phone');
    if (!bridgeHas('bili_register_send_code')) {
        showToast('error', t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.bili_register_send_code(tel, '86');
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.status_failed'));
            return;
        }
        showToast('success', result.message || t('bili.send_code'));
    } catch (err) {
        showToast('error', t('error.network'));
    }
}

/** Create a new account. */
async function submitBiliRegister() {
    const username = biliInputValue('bili-register-username');
    const password = biliInputValue('bili-register-password');
    const tel = biliInputValue('bili-register-phone');
    const code = biliInputValue('bili-register-code');
    if (!bridgeHas('bili_register')) {
        showToast('error', t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.bili_register(username, password, tel, code, '86');
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.status_failed'));
            return;
        }
        await finishBiliLogin();
    } catch (err) {
        showToast('error', t('bili.status_failed'));
    }
}

/** Sign out and forget the favourites. */
async function logoutBili() {
    if (!bridgeHas('bili_logout')) {
        showToast('error', t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.bili_logout();
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.status_failed'));
            return;
        }
        biliAccount.loggedIn = false;
        biliAccount.uname = '';
        biliAccount.face = '';
        biliFavorites.folders = [];
        biliFavorites.items = [];
        biliFavorites.folderId = null;
        biliFavorites.loaded = false;
        renderBiliAccount();
        renderBiliFolderChips();
        renderBiliFavorites();
        showToast('info', t('bili.status_signed_out'));
    } catch (err) {
        showToast('error', t('error.network'));
    }
}

/* --------------------------------------------------------- bili favourites */

/** Switch between the recommendation feed, the hot list, the favourites and the history. */
function setBiliTab(tab) {
    const known = ['recommend', 'hot', 'favorites', 'history'];
    biliSection.tab = known.indexOf(tab) >= 0 ? tab : 'recommend';
    document.querySelectorAll('#bili-tabs .top-tab').forEach((el) =>
        el.classList.toggle('is-active', el.dataset.tab === biliSection.tab));
    const listTab = biliSection.tab === 'hot' || biliSection.tab === 'recommend';
    ['bili-chips', 'bili-results', 'bili-state', 'bili-end'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.hidden = !listTab;
    });
    const more = document.getElementById('btn-bili-more');
    if (more) more.hidden = biliSection.tab !== 'hot' && biliSection.tab !== 'recommend';
    const favorites = document.getElementById('bili-favorites');
    if (favorites) favorites.hidden = biliSection.tab !== 'favorites';
    const history = document.getElementById('bili-history');
    if (history) history.hidden = biliSection.tab !== 'history';
    if (biliSection.tab === 'favorites') loadBiliFolders();
    if (biliSection.tab === 'history') loadBiliHistory(true);
    if (biliSection.tab === 'recommend') loadBiliRecommend(true);
    if (biliSection.tab === 'hot' && !biliSection.items.length) renderBili(biliSection.rid);
}

/**
 * The recommendation feed, narrowed down by the user's own words.
 *
 * B站网页端没有「兴趣标签」这套接口（只有逐条的「不感兴趣」），所以偏好只能
 * 在本地生效：屏蔽词命中的条目不显示，兴趣词命中的排到前面。
 */
function applyBiliPreferences(items) {
    const bili = (state.settings && state.settings.bili) || {};
    const words = (value) => (Array.isArray(value) ? value : [])
        .map((word) => String(word || '').trim().toLowerCase())
        .filter(Boolean);
    const blocked = words(bili.blocked_tags);
    const interest = words(bili.interest_tags);
    if (!blocked.length && !interest.length) return items;
    const textOf = (item) => ((item && (item.name || '')) + ' ' + (item && (item.artists || '')))
        .toLowerCase();
    const kept = items.filter((item) => {
        const text = textOf(item);
        return !blocked.some((word) => text.indexOf(word) >= 0);
    });
    if (!interest.length) return kept;
    const score = (item) => {
        const text = textOf(item);
        return interest.reduce((total, word) => total + (text.indexOf(word) >= 0 ? 1 : 0), 0);
    };
    // Stable: the ones that match stay in the order B站 sent them.
    return kept
        .map((item, index) => ({ item, index, score: score(item) }))
        .sort((a, b) => (b.score - a.score) || (a.index - b.index))
        .map((entry) => entry.item);
}

/** Read a new batch of recommendations. */
async function loadBiliRecommend(reset) {
    if (!bridgeHas('get_bilibili_recommend')) return;
    if (reset) {
        biliSection.items = [];
        biliSection.error = '';
        biliSection.recommendPage = 0;
    }
    biliSection.loading = true;
    biliSection.exhausted = false;
    renderBiliResults();
    try {
        // The feed is pulled with a growing fresh index: the first batch is
        // the home page, every later one is the next page of the scroll.
        const scroll = biliSection.recommendPage > 0;
        biliSection.recommendPage += 1;
        const result = await state.api.get_bilibili_recommend(
            24, biliSection.recommendPage, scroll);
        if (!result || !result.success) {
            biliSection.error = biliFailure(result, 'error.network');
            biliSection.loading = false;
            renderBiliResults();
            return;
        }
        const rows = result.tracks || result.items || [];
        // "More" appends; only a fresh start replaces what is on screen.
        biliSection.items = scroll
            ? biliSection.items.concat(applyBiliPreferences(rows))
            : applyBiliPreferences(rows);
        // An empty batch is how the feed says there is nothing left.
        biliSection.exhausted = !rows.length;
    } catch (err) {
        biliSection.error = t('error.network');
    }
    biliSection.loading = false;
    renderBiliResults();
}

/** Draw the row of favourite folders. */
function renderBiliFolderChips() {
    const row = document.getElementById('bili-folder-chips');
    if (!row) return;
    row.innerHTML = '';
    biliFavorites.folders.forEach((folder) => {
        if (!folder) return;
        const chip = document.createElement('button');
        chip.className = biliFavorites.folderId === folder.id ? 'chip is-active' : 'chip';
        chip.type = 'button';
        const count = t('bili.folder_media_count', { count: folder.media_count || 0 });
        chip.textContent = (folder.title || '') + ' · ' + count;
        chip.addEventListener('click', () => openBiliFolder(folder.id));
        row.appendChild(chip);
    });
}

/** Open one folder and read its first page. */
function openBiliFolder(folderId) {
    biliFavorites.selected.clear();
    biliFavorites.folderId = folderId;
    biliFavorites.page = 1;
    biliFavorites.total = 0;
    biliFavorites.items = [];
    biliFavorites.hasMore = false;
    biliFavorites.error = '';
    renderBiliFolderChips();
    loadBiliFavoritePage(1);
}

/** Read the folders of the account. */
async function loadBiliFolders() {
    biliFavorites.loading = true;
    renderBiliFavorites();
    try {
        if (!bridgeHas('get_bilibili_favorites')) {
            biliFavorites.error = t('error.unknown');
            biliFavorites.loading = false;
            renderBiliFavorites();
            return;
        }
        const result = await state.api.get_bilibili_favorites();
        if (!result || !result.success) {
            biliFavorites.folders = [];
            biliFavorites.error = biliFailure(result, 'bili.favorite_login_hint');
            biliFavorites.loading = false;
            renderBiliFolderChips();
            renderBiliFavorites();
            return;
        }
        biliFavorites.folders = result.folders || [];
        biliFavorites.loaded = true;
        biliFavorites.loading = false;
        renderBiliFolderChips();
        if (!biliFavorites.folderId && biliFavorites.folders.length) {
            openBiliFolder(biliFavorites.folders[0].id);
            return;
        }
        renderBiliFavorites();
    } catch (err) {
        biliFavorites.error = t('error.network');
        biliFavorites.loading = false;
        renderBiliFavorites();
    }
}

/**
 * Read one page of the open folder.
 *
 * 这是分页而不是"加载更多"：传进来的页码就是最终显示的那一页，格子里
 * 只有这一页的内容。之前是累加，翻到后面页时前面几百条全留在 DOM 里，
 * 越往后越卡，而且没法回到已经翻过的位置。
 *
 * 换页会清空勾选。批量下载/移动用的是「当前页上的勾选」，跨页留着勾选
 * 会让看不见的条目被一起处理——那是最难查的一类误操作。
 */
async function loadBiliFavoritePage(page) {
    if (biliFavorites.loading) return;
    if (!biliFavorites.folderId) {
        renderBiliFavorites();
        return;
    }
    const target = Math.max(1, Math.floor(Number(page) || 1));
    biliFavorites.loading = true;
    biliFavorites.error = '';
    renderBiliFavorites();
    try {
        if (!bridgeHas('get_bilibili_favorite_videos')) {
            biliFavorites.error = t('error.unknown');
            biliFavorites.loading = false;
            renderBiliFavorites();
            return;
        }
        const result = await state.api.get_bilibili_favorite_videos(
            biliFavorites.folderId, target, biliFavorites.pageSize);
        if (!result || !result.success) {
            biliFavorites.error = biliFailure(result, 'bili.favorite_login_hint');
            biliFavorites.loading = false;
            renderBiliFavorites();
            return;
        }
        biliFavorites.items = result.items || [];
        biliFavorites.page = target;
        biliFavorites.hasMore = !!result.has_more;
        biliFavorites.total = Number(result.total) || 0;
        biliFavorites.selected.clear();
        biliFavorites.loading = false;
        renderBiliFavorites();
    } catch (err) {
        biliFavorites.error = t('error.network');
        biliFavorites.loading = false;
        renderBiliFavorites();
    }
}

/** 一共几页；接口没给总数时返回 0，表示只能靠 has_more 判断。 */
function biliFavPageCount() {
    const size = biliFavorites.pageSize || 1;
    if (biliFavorites.total > 0) {
        return Math.max(1, Math.ceil(biliFavorites.total / size));
    }
    return 0;
}

/** 上一页 / 下一页。 */
function gotoBiliFavoritePage(page) {
    const target = Math.max(1, Math.floor(Number(page) || 1));
    if (target === biliFavorites.page && !biliFavorites.error) return;
    loadBiliFavoritePage(target);
}

/** 换每页条数：回到第一页重新读。 */
function setBiliFavPageSize(size) {
    const value = Math.max(1, Math.min(Math.floor(Number(size) || 0), 40));
    if (!value || value === biliFavorites.pageSize) return;
    biliFavorites.pageSize = value;
    biliFavorites.selected.clear();
    loadBiliFavoritePage(1);
}

/** Draw the favourites: the folders, the grid and the states. */
function renderBiliFavorites() {
    const grid = document.getElementById('bili-fav-results');
    const box = document.getElementById('bili-fav-state');
    if (!grid) return;
    grid.innerHTML = '';
    if (box) box.innerHTML = '';
    if (!biliAccount.loggedIn) {
        if (box) {
            box.appendChild(buildStateBlock('user-plus', t('bili.favorite_login_hint'), ''));
        }
        return;
    }
    if (!biliFavorites.folders.length) {
        if (box) box.appendChild(buildStateBlock('folder', t('bili.no_folders'), ''));
        return;
    }
    biliFavorites.items.forEach((track) => grid.appendChild(biliCardElement(track, {
        selectable: true,
        checked: biliFavorites.selected.has(track.bvid),
        onSelect: (on) => toggleBiliFavSelection(track.bvid, on)
    })));
    renderBiliFavBulk();
    if (biliFavorites.loading) return;
    if (biliFavorites.error && box) {
        box.appendChild(buildStateBlock('close', t('bili.error_title'),
            t('bili.error_hint', { message: biliFavorites.error }),
            t('common.retry'), () => loadBiliFavoritePage(biliFavorites.page)));
        return;
    }
    if (!biliFavorites.items.length && box) {
        box.appendChild(buildStateBlock('list', t('bili.favorites_empty'), ''));
    }
    renderBiliFavPager();
}

/**
 * 页码条：上一页 / 第 N / M 页 / 下一页 / 每页条数。
 *
 * 「还有没有下一页」有两个来源：接口给的总数算出来的页数，以及 has_more。
 * 总数拿不到时（接口没回 media_count）只靠 has_more，这时不知道最后一页
 * 是第几页，标签就只写"第 N 页"。
 */
function renderBiliFavPager() {
    const pager = document.getElementById('bili-fav-pager');
    if (!pager) return;

    const label = document.getElementById('bili-fav-page-label');
    const prev = document.getElementById('btn-bili-fav-prev');
    const next = document.getElementById('btn-bili-fav-next');
    const sizeBox = document.getElementById('bili-fav-page-size');

    const open = !!biliFavorites.folderId
        && (biliFavorites.items.length > 0 || biliFavorites.page > 1);
    pager.hidden = !open;

    const pages = biliFavPageCount();
    if (label) {
        label.textContent = pages > 1
            ? t('bili.page_status', { page: biliFavorites.page, pages })
            : t('bili.page_status_unknown', { page: biliFavorites.page });
    }
    if (prev) prev.disabled = biliFavorites.loading || biliFavorites.page <= 1;
    if (next) {
        const beyond = pages > 0 ? biliFavorites.page >= pages : !biliFavorites.hasMore;
        next.disabled = biliFavorites.loading || beyond;
    }
    if (sizeBox && String(sizeBox.value) !== String(biliFavorites.pageSize)) {
        sizeBox.value = String(biliFavorites.pageSize);
    }
}

/* --------------------------------------- 收藏夹的多选：批量下载与改收藏夹 */

/** Tick / untick one video of the folder grid. */
function toggleBiliFavSelection(bvid, on) {
    if (!bvid) return;
    if (on) biliFavorites.selected.add(bvid);
    else biliFavorites.selected.delete(bvid);
    renderBiliFavBulk();
}

/** The bulk bar only exists while something is ticked. */
function renderBiliFavBulk() {
    const bar = document.getElementById('bili-fav-bulk');
    if (!bar) return;
    const size = biliFavorites.selected.size;
    bar.hidden = size === 0;
    const count = document.getElementById('bili-fav-bulk-count');
    if (count) count.textContent = t('bili.bulk_selected', { count: size });
}

/** Drop the selection and redraw the grid. */
function clearBiliFavSelection() {
    biliFavorites.selected.clear();
    renderBiliFavorites();
}

/** The ticked videos, in the order they sit on screen. */
function selectedBiliFavTracks() {
    return biliFavorites.items.filter((item) => biliFavorites.selected.has(item.bvid));
}

/** The aids of the ticked videos; the batch routes need the numbers. */
function selectedBiliFavAids() {
    return selectedBiliFavTracks()
        .map((track) => Number(track.aid) || 0)
        .filter((aid) => aid > 0);
}

/**
 * Put every ticked video into the download queue and jump to that page.
 *
 * 队列本身已经有「全选 / 全部开始」，这里只负责把一批视频一次性放进去，
 * 剩下的交给下载页——延迟由后端按次排队，不在界面上控制。
 */
function bulkDownloadBiliFavorites() {
    const tracks = selectedBiliFavTracks();
    if (!tracks.length) return;
    let added = 0;
    tracks.forEach((track) => {
        if (queueBiliDownload(track, true)) added += 1;
    });
    if (added) showToast('success', t('bili.bulk_queued', { count: added }));
    clearBiliFavSelection();
    if (added) showView('bili-download');
}

/** Which batch action is waiting for a target folder. */
const biliFavTarget = { action: 'move' };

/** Ask which folder the ticked videos should go to. */
async function openBiliFavTargetModal(mode) {
    const tracks = selectedBiliFavTracks();
    if (!tracks.length) return;
    if (!bridgeHas('bili_favorite_move')) {
        showToast('error', t('error.unknown'));
        return;
    }
    biliFavTarget.action = mode === 'copy' ? 'copy' : 'move';
    const heading = document.getElementById('bili-fav-target-title');
    if (heading) {
        heading.textContent = t(biliFavTarget.action === 'copy' ? 'bili.bulk_copy' : 'bili.bulk_move');
    }
    const list = document.getElementById('bili-fav-target-list');
    if (!list) return;
    list.innerHTML = '';
    openModal('bili-fav-target-modal');
    let folders = biliFavorites.folders || [];
    if (!folders.length && bridgeHas('get_bilibili_favorites')) {
        try {
            const result = await state.api.get_bilibili_favorites();
            folders = (result && result.folders) || [];
        } catch (err) {
            folders = [];
        }
    }
    const source = String(biliFavorites.folderId || '');
    const usable = folders.filter((folder) => folder && String(folder.id) !== source);
    if (!usable.length) {
        list.appendChild(buildStateBlock('folder', t('bili.no_other_folders'), ''));
        return;
    }
    usable.forEach((folder) => {
        const button = document.createElement('button');
        button.className = 'btn btn-sm btn-secondary bili-fav-item';
        button.type = 'button';
        button.textContent = (folder.title || '') + ' · '
            + t('bili.folder_media_count', { count: folder.media_count || 0 });
        button.addEventListener('click', () => applyBiliFavTarget(String(folder.id)));
        list.appendChild(button);
    });
}

/** Move (or copy) the ticked videos into the folder the user picked. */
async function applyBiliFavTarget(targetId) {
    const source = String(biliFavorites.folderId || '');
    const aids = selectedBiliFavAids();
    if (!source || !targetId || !aids.length) {
        showToast('error', t('bili.bulk_move_failed'));
        return;
    }
    try {
        const result = await state.api.bili_favorite_move(
            source, targetId, aids, biliFavTarget.action === 'copy');
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.bulk_move_failed'));
            return;
        }
        closeModal('bili-fav-target-modal');
        showToast('success', t('bili.bulk_moved', { count: Number(result.count) || aids.length }));
        clearBiliFavSelection();
        await loadBiliFavoritePage(biliFavorites.page);
    } catch (err) {
        showToast('error', t('bili.bulk_move_failed'));
    }
}

/** Take the ticked videos out of the folder that is open. */
async function bulkRemoveBiliFavorites() {
    const folder = String(biliFavorites.folderId || '');
    const aids = selectedBiliFavAids();
    if (!folder || !aids.length) return;
    if (!bridgeHas('bili_favorite_batch_delete')) {
        showToast('error', t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.bili_favorite_batch_delete(folder, aids);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.bulk_remove_failed'));
            return;
        }
        showToast('success', t('bili.bulk_removed', { count: Number(result.count) || aids.length }));
        clearBiliFavSelection();
        // The folder lost videos, so its count changed too: read both the
        // list of folders and the open folder again.
        await loadBiliFolders();
        await loadBiliFavoritePage(biliFavorites.page);
    } catch (err) {
        showToast('error', t('bili.bulk_remove_failed'));
    }
}

/* ------------------------------------------------------- 收藏夹管理 */

/**
 * Open the folder manager.
 *
 * The list it shows is the one the favourites page uses, so both stay
 * the same after a change.
 */
function openBiliFolderManage() {
    if (!bridgeHas('get_bilibili_favorites')) {
        showToast('error', t('error.unknown'));
        return;
    }
    openModal('bili-folder-modal');
    loadBiliFolders();
    renderBiliFolderList();
}

/** Draw the folders with the buttons that change them. */
function renderBiliFolderList() {
    const list = document.getElementById('bili-folder-list');
    const state = document.getElementById('bili-folder-state');
    if (!list) return;
    list.innerHTML = '';
    if (state) state.textContent = '';
    if (!biliAccount.loggedIn) {
        if (state) state.textContent = t('bili.favorite_login_hint');
        return;
    }
    if (!biliFavorites.folders.length) {
        if (state) state.textContent = t('bili.no_folders');
        return;
    }
    biliFavorites.folders.forEach((folder) => {
        if (!folder) return;
        const row = document.createElement('div');
        row.className = 'bili-folder-row';
        const name = document.createElement('span');
        name.className = 'bili-folder-name';
        name.textContent = folder.title || '';
        row.appendChild(name);
        const count = document.createElement('span');
        count.className = 'bili-folder-count';
        count.textContent = t('bili.folder_media_count', { count: folder.media_count || 0 });
        row.appendChild(count);
        const privacy = document.createElement('button');
        privacy.className = 'btn btn-sm btn-ghost';
        privacy.type = 'button';
        privacy.textContent = folder.private
            ? t('bili.folder_private') : t('bili.folder_public');
        privacy.addEventListener('click', () => toggleBiliFolderPrivacy(folder));
        row.appendChild(privacy);
        const rename = document.createElement('button');
        rename.className = 'btn btn-sm btn-ghost';
        rename.type = 'button';
        rename.textContent = t('bili.folder_rename');
        rename.addEventListener('click', () => renameBiliFolder(folder));
        row.appendChild(rename);
        const remove = document.createElement('button');
        remove.className = 'btn btn-sm btn-ghost';
        remove.type = 'button';
        remove.textContent = t('bili.folder_delete');
        remove.addEventListener('click', () => deleteBiliFolder(folder));
        row.appendChild(remove);
        list.appendChild(row);
    });
}

/** Create a folder. */
async function createBiliFolder() {
    if (!bridgeHas('bili_create_favorite_folder')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const title = biliInputValue('bili-folder-new-title');
    if (!title) return;
    const intro = biliInputValue('bili-folder-new-intro');
    const privateBox = document.getElementById('bili-folder-new-private');
    const privacy = privateBox && privateBox.checked ? 1 : 0;
    try {
        const result = await state.api.bili_create_favorite_folder(title, intro, privacy);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.folder_create'));
            return;
        }
        const titleInput = document.getElementById('bili-folder-new-title');
        const introInput = document.getElementById('bili-folder-new-intro');
        if (titleInput) titleInput.value = '';
        if (introInput) introInput.value = '';
        showToast('success', t('bili.folder_created'));
        await loadBiliFolders();
        renderBiliFolderList();
    } catch (err) {
        showToast('error', t('bili.folder_create'));
    }
}

/** Ask for a new name and rename the folder. */
async function renameBiliFolder(folder) {
    if (!folder || !bridgeHas('bili_edit_favorite_folder')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const title = window.prompt(t('bili.folder_create_title'), folder.title || '');
    if (title === null) return;
    const name = String(title).trim();
    if (!name || name === folder.title) return;
    try {
        const result = await state.api.bili_edit_favorite_folder(
            folder.id, name, folder.intro || '', folder.private ? 1 : 0);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.folder_rename'));
            return;
        }
        showToast('success', t('bili.folder_renamed'));
        await loadBiliFolders();
        renderBiliFolderList();
    } catch (err) {
        showToast('error', t('bili.folder_rename'));
    }
}

/** Switch a folder between public and private. */
async function toggleBiliFolderPrivacy(folder) {
    if (!folder || !bridgeHas('bili_edit_favorite_folder')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const next = folder.private ? 0 : 1;
    try {
        const result = await state.api.bili_edit_favorite_folder(
            folder.id, folder.title || '', folder.intro || '', next);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.folder_set_private'));
            return;
        }
        showToast('success', t('bili.folder_privacy_changed', {
            state: next ? t('bili.folder_private') : t('bili.folder_public')
        }));
        await loadBiliFolders();
        renderBiliFolderList();
    } catch (err) {
        showToast('error', t('bili.folder_set_private'));
    }
}

/** Delete a folder. */
async function deleteBiliFolder(folder) {
    if (!folder || !bridgeHas('bili_delete_favorite_folder')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const name = folder.title || '';
    if (!window.confirm(t('bili.folder_delete_confirm', { name: name }))) return;
    try {
        const result = await state.api.bili_delete_favorite_folder(folder.id);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.folder_delete'));
            return;
        }
        showToast('success', t('bili.folder_deleted'));
        // The open folder may be the one that was deleted.
        if (biliFavorites.folderId === folder.id) {
            biliFavorites.folderId = null;
            biliFavorites.items = [];
        }
        await loadBiliFolders();
        renderBiliFolderList();
        renderBiliFavorites();
    } catch (err) {
        showToast('error', t('bili.folder_delete'));
    }
}

/* ------------------------------------------------------- 关注与粉丝 */

const biliRelations = {
    seg: 'followings',
    page: 1,
    items: [],
    hasMore: false,
    loading: false,
    error: ''
};

/** Open the list of who this account follows and who follows it. */
function openBiliRelations() {
    if (!bridgeHas('get_bilibili_followings')) {
        showToast('error', t('error.unknown'));
        return;
    }
    openModal('bili-relations-modal');
    biliRelations.seg = 'followings';
    biliRelations.page = 1;
    biliRelations.items = [];
    biliRelations.hasMore = false;
    const chips = document.querySelectorAll('#bili-relations-segments .chip');
    chips.forEach((chip) => {
        chip.classList.toggle('is-active', chip.dataset.seg === 'followings');
    });
    loadBiliRelations(true);
}

/** Read one page of the relation list. */
async function loadBiliRelations(reset) {
    if (biliRelations.loading) return;
    const followers = biliRelations.seg === 'followers';
    const method = followers ? 'get_bilibili_followers' : 'get_bilibili_followings';
    if (!bridgeHas(method)) {
        showToast('error', t('error.unknown'));
        return;
    }
    if (!reset && !biliRelations.hasMore) return;
    biliRelations.loading = true;
    renderBiliRelations();
    try {
        const page = reset ? 1 : biliRelations.page + 1;
        const result = followers
            ? await state.api.get_bilibili_followers(biliAccount.mid || '', page, 30)
            : await state.api.get_bilibili_followings(biliAccount.mid || '', page, 30);
        if (!result || !result.success) {
            biliRelations.error = biliFailure(result, 'bili.relations_failed');
            biliRelations.loading = false;
            renderBiliRelations();
            return;
        }
        const items = result.items || [];
        if (reset) biliRelations.items = [];
        items.forEach((item) => { if (item) biliRelations.items.push(item); });
        biliRelations.page = page;
        biliRelations.hasMore = !!result.has_more;
        biliRelations.error = '';
    } catch (err) {
        biliRelations.error = t('error.network');
    } finally {
        biliRelations.loading = false;
        renderBiliRelations();
    }
}

/** Draw the relation list. */
function renderBiliRelations() {
    const list = document.getElementById('bili-relations-list');
    const state = document.getElementById('bili-relations-state');
    const more = document.getElementById('btn-bili-relations-more');
    if (!list) return;
    list.innerHTML = '';
    if (state) state.textContent = biliRelations.loading ? t('common.loading') : '';
    if (!biliRelations.loading && biliRelations.error) {
        if (state) state.textContent = biliRelations.error;
        return;
    }
    if (!biliRelations.loading && !biliRelations.items.length) {
        if (state) state.textContent = t('bili.relations_empty');
        return;
    }
    biliRelations.items.forEach((item) => {
        if (!item) return;
        const row = document.createElement('div');
        row.className = 'bili-relation-row';
        if (item.face) {
            const face = document.createElement('img');
            face.className = 'bili-relation-face';
            face.alt = '';
            face.loading = 'lazy';
            face.src = biliImageSrc(item.face);
            face.addEventListener('error', () => { face.remove(); });
            row.appendChild(face);
        }
        const info = document.createElement('div');
        info.className = 'bili-relation-info';
        const name = document.createElement('button');
        name.className = 'bili-relation-name';
        name.type = 'button';
        name.textContent = item.uname || '';
        name.addEventListener('click', () => {
            if (item.mid) openBiliSpace(item.mid);
        });
        info.appendChild(name);
        const verify = item.official_verify && item.official_verify.desc
            ? item.official_verify.desc : '';
        if (verify) {
            const badge = document.createElement('span');
            badge.className = 'source-badge is-bili';
            badge.textContent = verify;
            info.appendChild(badge);
        }
        if (item.sign) {
            const sign = document.createElement('div');
            sign.className = 'bili-relation-sign';
            sign.textContent = item.sign;
            info.appendChild(sign);
        }
        row.appendChild(info);
        list.appendChild(row);
    });
    if (more) {
        more.hidden = !biliRelations.hasMore || biliRelations.loading;
        more.disabled = biliRelations.loading;
        more.textContent = biliRelations.loading
            ? t('common.loading') : t('bili.load_more');
    }
}

/* ------------------------------------------------------- 合集里的视频 */

const biliCollection = {
    id: '',
    mid: '',
    page: 1,
    items: [],
    hasMore: false,
    loading: false,
    error: ''
};

/** Open one subscribed collection and list what is in it. */
async function openBiliCollection(item) {
    if (!item || !item.id) return;
    const heading = document.getElementById('bili-collection-title');
    if (heading) heading.textContent = item.title || '';
    biliCollection.id = String(item.id);
    biliCollection.mid = String(item.author_mid || '');
    biliCollection.page = 1;
    biliCollection.items = [];
    biliCollection.hasMore = false;
    biliCollection.error = '';
    openModal('bili-collection-modal');
    await loadBiliCollectionVideos(true);
}

/** Read one page of the collection's videos. */
async function loadBiliCollectionVideos(reset) {
    if (!bridgeHas('get_bili_collection_videos')) {
        const stateEl = document.getElementById('bili-collection-videos-state');
        if (stateEl) stateEl.textContent = t('bili.need_desktop');
        return;
    }
    if (biliCollection.loading) return;
    if (!reset && !biliCollection.hasMore) return;
    if (reset) {
        biliCollection.page = 1;
        biliCollection.items = [];
        biliCollection.error = '';
    }
    biliCollection.loading = true;
    renderBiliCollectionVideos();
    try {
        const page = reset ? 1 : biliCollection.page + 1;
        const result = await state.api.get_bili_collection_videos(
            biliCollection.id, biliCollection.mid, page);
        if (result && result.success) {
            (result.tracks || result.items || []).forEach((entry) => {
                if (entry) biliCollection.items.push(entry);
            });
            biliCollection.page = page;
            biliCollection.hasMore = !!result.has_more;
        } else {
            biliCollection.error = (result && result.message) || t('error.network');
        }
    } catch (err) {
        biliCollection.error = t('error.network');
    }
    biliCollection.loading = false;
    renderBiliCollectionVideos();
}

/** Draw the collection's videos. */
function renderBiliCollectionVideos() {
    const grid = document.getElementById('bili-collection-videos');
    const stateEl = document.getElementById('bili-collection-videos-state');
    const more = document.getElementById('btn-bili-collection-videos-more');
    if (!grid) return;
    grid.innerHTML = '';
    if (stateEl) stateEl.textContent = biliCollection.loading ? t('common.loading') : '';
    if (biliCollection.error && stateEl && !biliCollection.loading) {
        stateEl.textContent = biliCollection.error;
    }
    if (!biliCollection.loading && !biliCollection.error && !biliCollection.items.length) {
        if (stateEl) stateEl.textContent = t('bili.collection_empty');
    }
    biliCollection.items.forEach((track) => grid.appendChild(biliCardElement(track)));
    if (more) {
        more.hidden = !biliCollection.hasMore || biliCollection.loading;
        more.disabled = biliCollection.loading;
        more.textContent = biliCollection.loading ? t('common.loading') : t('bili.load_more');
    }
}

/* --------------------------------------------------------- 番剧选集 */

/** Open the episode list of one followed bangumi. */
async function openBiliBangumiEpisodes(show) {
    const seasonId = Number(show && show.season_id) || 0;
    if (!seasonId) {
        // 没有季号就只能直接播最知道的那一集。
        await playBangumiShow(show);
        return;
    }
    if (!bridgeHas('get_bili_bangumi_season')) {
        await playBangumiShow(show);
        return;
    }
    const heading = document.getElementById('bili-episodes-title');
    if (heading) heading.textContent = (show && show.title) || '';
    const list = document.getElementById('bili-episode-list');
    const stateEl = document.getElementById('bili-episodes-state');
    if (!list) return;
    list.innerHTML = '';
    if (stateEl) stateEl.textContent = t('common.loading');
    openModal('bili-episodes-modal');
    try {
        const result = await state.api.get_bili_bangumi_season(seasonId);
        list.innerHTML = '';
        if (!result || !result.success) {
            if (stateEl) stateEl.textContent = (result && result.message) || t('error.network');
            return;
        }
        if (stateEl) stateEl.textContent = '';
        const episodes = result.episodes || [];
        if (!episodes.length) {
            if (stateEl) stateEl.textContent = t('bili.no_episodes');
            return;
        }
        episodes.forEach((episode, index) => {
            const button = document.createElement('button');
            button.className = 'btn btn-sm btn-secondary bili-episode-item';
            button.type = 'button';
            const label = String(episode.title || '').trim();
            const long = String(episode.long_title || '').trim();
            button.textContent = (label || ('EP' + (index + 1))) + (long ? ' · ' + long : '');
            button.addEventListener('click', () => {
                closeModal('bili-episodes-modal');
                playBangumiShow({
                    season_id: seasonId,
                    ep_id: Number(episode.ep_id) || 0,
                    title: (show && show.title) || '',
                    cover: (show && show.cover) || ''
                });
            });
            list.appendChild(button);
        });
    } catch (err) {
        if (stateEl) stateEl.textContent = t('error.network');
    }
}

/* ----------------------------------------------------------- bili playback */

/** The status line of the player. */
function setBiliPlayerStatus(code) {
    const status = document.getElementById('bili-player-status');
    if (!status) return;
    if (code === 'connecting') { status.textContent = t('bili.status_connecting'); return; }
    if (code === 'buffering') { status.textContent = t('bili.buffering'); return; }
    if (code === 'ready') { status.textContent = t('bili.video_ready'); return; }
    if (code === 'audio_only') { status.textContent = t('bili.audio_only'); return; }
    if (code === 'streaming') { status.textContent = t('bili.streaming'); return; }
    status.textContent = t('bili.video_error');
}

/**
 * The overlay on top of the picture of the inline player.
 *
 * Same piece the standalone window uses.  Without it the modal is a black
 * rectangle until the streams arrive, and a failure is one red word.
 */
let biliOverlay = null;

function ensureBiliOverlay() {
    if (biliOverlay || !window.BiliOverlay) return biliOverlay;
    biliOverlay = window.BiliOverlay.createOverlay({
        t: t,
        root: 'bili-overlay',
        title: 'bili-overlay-title',
        detail: 'bili-overlay-detail',
        spinner: 'bili-overlay-spinner',
        actions: 'bili-overlay-actions',
        retry: 'bili-overlay-retry',
        onRetry: () => {
            biliOverlay.hide();
            loadBiliStreams(biliPlayer.track && biliPlayer.track.bvid, biliPlayer.quality || 0);
        }
    });
    return biliOverlay;
}

function showBiliOverlay(kind, detailKey) {
    const view = ensureBiliOverlay();
    if (view) view.show(kind, detailKey);
}

function hideBiliOverlay() {
    if (biliOverlay) biliOverlay.hide();
}

/** The badges that say what the streams carry (HD, Dolby, Hi-Res). */
function renderBiliQualityBadges(streams) {
    const box = document.getElementById('bili-quality-badges');
    if (!box) return;
    box.innerHTML = '';
    const names = (streams.qualities || []).map((quality) =>
        (quality && quality.name ? String(quality.name).toLowerCase() : '')).join(' ');
    const add = (key) => {
        const badge = document.createElement('span');
        badge.className = 'source-badge is-bili';
        badge.textContent = t(key);
        box.appendChild(badge);
    };
    if (streams.dolby || names.indexOf('dolby') >= 0) add('bili.dolby');
    if (streams.flac || names.indexOf('flac') >= 0
        || names.indexOf('hi-res') >= 0 || names.indexOf('hires') >= 0) add('bili.hires');
    if (streams.hd || names.indexOf('hd') >= 0 || names.indexOf('1080') >= 0
        || names.indexOf('4k') >= 0) add('bili.hd');
}

/** The quality picker: the auto line and the qualities that are available. */
function renderBiliQualities(streams) {
    const select = document.getElementById('bili-quality');
    if (!select) return;
    select.innerHTML = '';
    const auto = document.createElement('option');
    auto.value = '0';
    auto.textContent = t('bili.quality_auto');
    select.appendChild(auto);
    const offered = ['0'];
    (streams.qualities || []).forEach((quality) => {
        if (!quality || !quality.available) return;
        const option = document.createElement('option');
        option.value = String(quality.id);
        option.textContent = quality.name || String(quality.id);
        select.appendChild(option);
        offered.push(option.value);
    });
    // The chosen quality stays, unless it is not in the list any more.
    // 名单里没有请求的那个画质时，显示真正在播的那个：请求 1080P、这条
    // 视频只有 720P，下拉框写「720P」比写「自动」更接近事实。
    const wanted = String(biliPlayer.quality || 0);
    const played = String(Number(streams.picked_quality) || 0);
    if (offered.indexOf(wanted) >= 0) select.value = wanted;
    else if (played !== '0' && offered.indexOf(played) >= 0) select.value = played;
    else select.value = '0';
    renderBiliQualityBadges(streams);
}

/**
 * The quality a fresh playback should start with.
 *
 * 设置里存的「默认画质」以前只在下载时用过，播放这边一路传的是 0，所以
 * 不管设置成什么都从「自动」开始。这里统一读一次设置，三个入口（内联
 * 播放器、独立窗口、番剧）都从它出发。
 */
function defaultBiliQuality() {
    const bili = (state.settings && state.settings.bili) || {};
    return Number(bili.quality) || 0;
}

/** Open the player for one track. */
function openBiliPlayer(track) {
    const name = document.getElementById('bili-video-name');
    if (name) name.textContent = (track && track.name) || '';
    const status = document.getElementById('bili-player-status');
    if (status) status.textContent = t('bili.buffering');
    openModal('bili-player-modal');
}

/**
 * Fetch the streams with a timeout so the overlay never gets stuck.
 *
 * The bridge call can hang (network, backend busy, etc.).  Without a
 * timeout the user sees "正在获取视频" forever.  This wraps the call in a
 * Promise.race with a 20-second deadline; on timeout it surfaces a
 * friendly error instead of leaving the spinner running.
 */
async function loadBiliStreams(bvid, quality) {
    const videoEl = document.getElementById('bili-video');
    if (!videoEl) return;
    if (!bvid) {
        setBiliPlayerStatus('error');
        showBiliOverlay('error', 'bili.overlay_no_stream');
        return;
    }
    showBiliOverlay('loading');
    // The standalone window and this one go through the same loader: the
    // two used to keep a copy each, and the copies drifted.
    const outcome = await window.BiliStreamLoader.load({
        api: state.api,
        bvid: bvid,
        quality: quality,
        videoEl: videoEl,
        onStatus: (code) => setBiliPlayerStatus(code),
        onStreams: (result) => {
            biliPlayer.streams = result;
            biliPlayer.quality = quality;
            // The details of the video: the detail panel needs the uploader too.
            biliPlayer.title = result.title || '';
            biliPlayer.author = result.author || '';
            biliPlayer.authorMid = result.author_mid || '';
            renderBiliQualities(result);
        },
        // If the user enabled "resume playback", check the watch history
        // and seek to the last position.
        resumePlayback: !!(state.settings && state.settings.bili
            && state.settings.bili.resume_playback)
    });
    if (outcome.ok) {
        hideBiliOverlay();
        armBiliPlaybackReport(videoEl);
        if (outcome.resumed > 0) {
            showToast('info', t('bili.resumed_from', { time: fmtTime(outcome.resumed) }), 3000);
        }
        return;
    }
    setBiliPlayerStatus('error');
    if (outcome.error === 'timeout') {
        showBiliOverlay('error', 'bili.overlay_timeout');
    } else {
        showBiliOverlay('error', 'bili.overlay_no_stream');
        showToast('error', biliFailure(outcome.result, 'bili.video_error'));
    }
}

/**
 * Report what the inline player is playing, once every 15 seconds.
 *
 * The backend refuses these unless "report playback" is on, so the player can
 * call it without asking first; what it buys is the Bilibili watch history and
 * a recommendation feed that knows what the user actually watches.
 */
const biliInlineReport = { timer: null, session: '', startTs: 0 };

function reportBiliInline(playType) {
    const bvid = (biliPlayer.track && biliPlayer.track.bvid) || biliDetail.bvid || '';
    const videoEl = document.getElementById('bili-video');
    if (!bvid || !videoEl || !bridgeHas('bili_report_playback')) return;
    const streams = biliPlayer.streams || {};
    const played = Math.max(0, Math.floor(Number(videoEl.currentTime) || 0));
    const duration = Math.floor(Number(videoEl.duration) || Number(streams.duration) || 0);
    try {
        state.api.bili_report_playback(
            bvid, Number(streams.cid || 0), played, duration,
            Number(biliPlayer.quality || 0), biliInlineReport.startTs,
            biliInlineReport.session, Number(playType || 0)
        ).catch(() => {});
    } catch (err) { /* 上报不能影响播放 */ }
}

function armBiliPlaybackReport(videoEl) {
    if (!videoEl || videoEl.dataset.reportBound === '1') return;
    videoEl.dataset.reportBound = '1';
    const start = () => {
        if (!biliInlineReport.session) {
            biliInlineReport.session = Math.random().toString(16).slice(2)
                + Date.now().toString(16);
            biliInlineReport.startTs = Math.floor(Date.now() / 1000);
        }
        reportBiliInline(1);
        if (!biliInlineReport.timer) {
            biliInlineReport.timer = setInterval(() => reportBiliInline(0), 15000);
        }
    };
    const stop = (playType) => {
        if (biliInlineReport.timer) {
            clearInterval(biliInlineReport.timer);
            biliInlineReport.timer = null;
        }
        if (playType) reportBiliInline(playType);
        if (playType === 4) biliInlineReport.session = '';
    };
    videoEl.addEventListener('playing', start);
    videoEl.addEventListener('pause', () => stop(2));
    videoEl.addEventListener('ended', () => stop(4));
}

/** Play one video in the client. */
async function playBiliTrack(track) {
    const bvid = (track && track.bvid) || '';
    if (!bvid) {
        showToast('error', t('bili.video_error'));
        return;
    }
    if (!bridgeHas('get_bilibili_video_streams')) {
        showToast('error', t('error.unknown'));
        return;
    }
    biliPlayer.track = track || null;
    // 设置里的默认画质从这里生效：以前这一行写死 0，不管设置成什么都从
    // 「自动」开始，看着就像设置没存下来。
    const quality = defaultBiliQuality();
    biliPlayer.quality = quality;
    // The video plays in a window of its own, so the main window stays
    // usable while it runs.  The window carries the comments and the
    // danmaku.  The setting can turn that off.
    const separate = !state.settings || !state.settings.bili
        || state.settings.bili.separate_player_window !== false;
    if (separate && bridgeHas('open_bili_player_window')) {
        try {
            const opened = await state.api.open_bili_player_window(bvid, 1, quality);
            if (opened && opened.success) return;
        } catch (err) {
            /* The window could not be opened: fall back to the old player. */
        }
    }
    openBiliPlayer(track);
    await loadBiliStreams(bvid, quality);
    // The detail panel lives inside the player, so it is filled once the
    // video is known.
    await openBiliDetail(bvid, {
        title: biliPlayer.title,
        author: biliPlayer.author,
        authorMid: biliPlayer.authorMid
    });
}

/**
 * Dev-only path: a plain browser cannot open a native window, so the
 * player page is opened as a tab.  It has no bridge there and says so.
 */
function openBiliPlayerWindowFallback(payload) {
    const url = (payload && payload.url) || '';
    if (!url) return;
    try {
        window.open(url, '_blank', 'width=1180,height=780');
    } catch (err) {
        /* The browser refused to open the tab. */
    }
}

/** Switch to another quality and keep the place in the video. */
async function changeBiliQuality(value) {
    const track = biliPlayer.track;
    const videoEl = document.getElementById('bili-video');
    if (!track || !track.bvid || !videoEl) return;
    const position = videoEl.currentTime || 0;
    const restore = () => {
        try { videoEl.currentTime = position; } catch (err) { /* 还没准备好 */ }
    };
    videoEl.addEventListener('loadedmetadata', restore, { once: true });
    await loadBiliStreams(track.bvid, Number(value) || 0);
    if (position) restore();
}

/** Stop the playback and let go of the media. */
function closeBiliPlayer() {
    const videoEl = document.getElementById('bili-video');
    if (videoEl) {
        try { videoEl.pause(); } catch (err) { /* 没有在播 */ }
        try {
            videoEl.removeAttribute('src');
            videoEl.load();
        } catch (err) { /* 忽略 */ }
    }
    if (window.stopBiliVideo) window.stopBiliVideo();
    biliPlayer.streams = null;
}

/** Download one video (and its audio track). */
/** The Bilibili downloads that are on screen right now, keyed by file path. */
const biliDownloads = new Map();

function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return value + ' B';
    if (value < 1024 * 1024) return (value / 1024).toFixed(0) + ' KB';
    if (value < 1024 * 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB';
    return (value / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

/** Draw the download stack; it disappears once nothing is left in it. */
function renderBiliDownloads() {
    const stack = document.getElementById('bili-download-stack');
    const rows = document.getElementById('bili-downloads-rows');
    const head = document.getElementById('bili-downloads-head');
    if (!stack || !rows) return;
    if (head) head.textContent = t('bili.download_progress_title');
    rows.innerHTML = '';
    biliDownloads.forEach((entry) => {
        const row = document.createElement('div');
        row.className = 'bili-download-row';
        const name = document.createElement('div');
        name.className = 'bili-download-name';
        name.textContent = entry.name || entry.path || '';
        name.title = entry.path || '';
        const bar = document.createElement('div');
        bar.className = 'bili-download-bar';
        const fill = document.createElement('div');
        fill.className = 'bili-download-fill';
        fill.style.width = Math.max(0, Math.min(100, entry.percent)) + '%';
        bar.appendChild(fill);
        const meta = document.createElement('div');
        meta.className = 'bili-download-meta';
        const done = entry.status === 'completed' || entry.status === 'merged';
        const failed = entry.status === 'failed' || entry.status === 'merge_skipped';
        if (entry.status === 'merging') {
            meta.textContent = t('bili.download_merging');
        } else if (failed) {
            meta.textContent = entry.message || t('bili.download_failed');
        } else if (done) {
            meta.textContent = t('bili.download_done') + ' · ' + formatBytes(entry.total);
        } else {
            meta.textContent = formatBytes(entry.downloaded) + ' / '
                + (entry.total ? formatBytes(entry.total) : '?')
                + ' · ' + Math.round(entry.percent) + '%';
        }
        row.appendChild(name);
        row.appendChild(bar);
        row.appendChild(meta);
        rows.appendChild(row);
    });
    stack.hidden = biliDownloads.size === 0;
}

/** One progress event from the backend (one per file). */
function onBiliDownloadProgress(payload) {
    if (!payload) return;
    const key = String(payload.path || payload.filename || payload.kind || 'bili');
    const status = String(payload.status || 'downloading');
    const entry = biliDownloads.get(key) || {
        path: payload.path || '', name: payload.filename || String(payload.path || '').split('/').pop(),
        percent: 0, downloaded: 0, total: 0, status: 'downloading', message: ''
    };
    entry.status = status;
    if (payload.filename) entry.name = String(payload.filename);
    if (payload.downloaded !== undefined) entry.downloaded = Number(payload.downloaded) || 0;
    if (payload.total) entry.total = Number(payload.total) || 0;
    if (payload.progress !== undefined) entry.percent = Number(payload.progress) || 0;
    if (payload.message) entry.message = String(payload.message);
    biliDownloads.set(key, entry);
    renderBiliDownloads();
    if (status === 'completed' || status === 'merged' || status === 'failed'
        || status === 'merge_skipped') {
        if (status === 'completed' || status === 'merged') {
            showToast('success', t('bili.download_done'), 4000);
        }
        if (status === 'merge_skipped') {
            showToast('warning', entry.message || t('bili.download_failed'), 8000);
        }
        // Leave the finished row on screen for a moment, then drop it.
        setTimeout(() => {
            biliDownloads.delete(key);
            renderBiliDownloads();
        }, status === 'failed' ? 12000 : 6000);
    }
}

async function downloadBiliTrack(track) {
    if (!track) return;
    if (!bridgeHas('download_bilibili_tracks')) {
        showToast('error', t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.download_bilibili_tracks(
            [track], { quality: biliPlayer.quality || 0 });
        if (result && result.success) {
            showToast('success', t('bili.downloading'));
            return;
        }
        showToast('error', biliFailure(result, 'bili.download_failed'));
    } catch (err) {
        showToast('error', t('bili.download_failed'));
    }
}

/* ------------------------------------------------- B站下载页：队列与批量 */

/**
 * 待下载的 B站视频。
 *
 * 一条视频可以先放进队列，再在下载页里勾选、一起下。单个下载走的是同一条
 * 队列，只是把勾选留在这一个上——两条路径调的是同一个后端方法，
 * 行为不会分叉。
 */
const biliDownloadQueue = {
    items: [],
    selected: new Set()
};

/** Put one video into the queue.  The same video is only queued once. */
function queueBiliDownload(track, quiet) {
    if (!track || !track.bvid) return false;
    if (biliDownloadQueue.items.some((item) => item.bvid === track.bvid)) {
        if (!quiet) showToast('info', t('bili.download_already_queued'));
        return false;
    }
    biliDownloadQueue.items.push({
        bvid: track.bvid,
        name: track.name || track.title || track.bvid,
        artists: track.artists || track.author || '',
        cover_url: track.cover_url || '',
        duration: track.duration || 0
    });
    biliDownloadQueue.selected.add(track.bvid);
    renderBiliDownloadQueue();
    if (!quiet) showToast('success', t('bili.download_queued'));
    return true;
}

/** Draw the queue of the download page. */
function renderBiliDownloadQueue() {
    const list = document.getElementById('bili-download-list');
    if (!list) return;
    const count = document.getElementById('bili-download-count');
    const start = document.getElementById('btn-bili-download-start');
    list.innerHTML = '';
    if (!biliDownloadQueue.items.length) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = t('bili.download_queue_empty');
        list.appendChild(empty);
    } else {
        biliDownloadQueue.items.forEach((item) => {
            const row = document.createElement('div');
            row.className = 'song-row';
            const check = document.createElement('input');
            check.type = 'checkbox';
            check.className = 'option checkbox';
            check.checked = biliDownloadQueue.selected.has(item.bvid);
            check.addEventListener('change', () => {
                if (check.checked) biliDownloadQueue.selected.add(item.bvid);
                else biliDownloadQueue.selected.delete(item.bvid);
                renderBiliDownloadQueue();
            });
            const label = document.createElement('label');
            label.className = 'song-main';
            const name = document.createElement('span');
            name.className = 'song-name';
            name.textContent = item.name;
            label.appendChild(name);
            if (item.artists) {
                const artist = document.createElement('span');
                artist.className = 'song-artist';
                artist.textContent = item.artists;
                label.appendChild(artist);
            }
            row.appendChild(check);
            row.appendChild(label);
            // One video can leave the queue on its own; the rest stay.
            const remove = document.createElement('button');
            remove.className = 'btn btn-sm btn-ghost';
            remove.type = 'button';
            remove.textContent = t('bili.download_remove');
            remove.addEventListener('click', () => {
                biliDownloadQueue.items = biliDownloadQueue.items
                    .filter((entry) => entry.bvid !== item.bvid);
                biliDownloadQueue.selected.delete(item.bvid);
                renderBiliDownloadQueue();
            });
            row.appendChild(remove);
            list.appendChild(row);
        });
    }
    const chosen = biliDownloadQueue.selected.size;
    if (count) count.textContent = chosen ? '(' + chosen + ')' : '';
    if (start) start.disabled = chosen === 0;
}

/** Download every checked video.  The backend takes the whole list. */
async function startBiliDownloads() {
    if (!bridgeHas('download_bilibili_tracks')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const chosen = biliDownloadQueue.items
        .filter((item) => biliDownloadQueue.selected.has(item.bvid));
    if (!chosen.length) return;
    const options = {
        quality: Number(document.getElementById('bili-download-quality')?.value || 0),
        max_concurrent: Number(document.getElementById('bili-download-concurrent')?.value || 2),
        // 取流之间至少隔这么久，一批几十条也不会把 B站打到限流。
        interval: Number(document.getElementById('bili-download-interval')?.value || 1.5),
        overwrite: !!document.getElementById('bili-download-overwrite')?.checked,
        merge_audio: !!document.getElementById('bili-download-merge')?.checked
    };
    try {
        const result = await state.api.download_bilibili_tracks(chosen, options);
        if (result && result.success) {
            showToast('success', t('bili.downloading'));
            // 清掉已经下好的，剩下的还留在队列里。
            biliDownloadQueue.items = biliDownloadQueue.items
                .filter((item) => !biliDownloadQueue.selected.has(item.bvid));
            biliDownloadQueue.selected.clear();
            renderBiliDownloadQueue();
            return;
        }
        showToast('error', biliFailure(result, 'bili.download_failed'));
    } catch (err) {
        showToast('error', t('bili.download_failed'));
    }
}

/* --------------------------------------- bili detail: counters, comments, danmaku */

/**
 * The state of the video detail panel.
 *
 * The like and the favourite are kept here: Bilibili has no query for
 * "which videos did I like", so the button can only follow what the user
 * did last.  A new video starts over.
 */
const biliDetail = {
    bvid: '',
    title: '',
    author: '',
    authorMid: '',
    stat: {},
    liked: false,
    coinGiven: 0,
    favorited: false,
    comments: [],
    commentPage: 1,
    commentHasMore: false,
    commentLoading: false,
    replyTo: null,
    danmaku: [],
    danmakuCursor: 0,
    danmakuMode: '',
    danmakuKeyword: ''
};

/** The danmaku that are on the picture at the moment. */
const biliDanmakuLive = [];
/** The lines the scrolling danmaku may use, and until when they are busy. */
const biliDanmakuRows = [];

/** The numbers of the video that is open. */
function renderBiliDetailStats() {
    const box = document.getElementById('bili-detail-stats');
    if (!box) return;
    box.innerHTML = '';
    const stat = biliDetail.stat || {};
    const add = (key, value) => {
        const chip = document.createElement('span');
        chip.className = 'source-badge is-bili';
        chip.textContent = t(key) + ' ' + formatPlayCount(value);
        box.appendChild(chip);
    };
    add('bili.stat_view', stat.view || 0);
    add('bili.stat_danmaku', stat.danmaku || 0);
    add('bili.stat_reply', stat.reply || 0);
    add('bili.stat_favorite', stat.favorite || 0);
    add('bili.stat_coin', stat.coin || 0);
    add('bili.stat_like', stat.like || 0);
}

/** The title, the uploader and the state of the three buttons. */
function renderBiliDetailHeader() {
    const title = document.getElementById('bili-detail-title');
    if (title) title.textContent = biliDetail.title || '';
    const up = document.getElementById('bili-detail-up');
    if (up) {
        up.textContent = biliDetail.author || '';
        up.hidden = !biliDetail.authorMid;
    }
    const like = document.getElementById('btn-bili-like');
    if (like) {
        like.textContent = biliDetail.liked ? t('bili.liked') : t('bili.like');
        like.classList.toggle('is-active', biliDetail.liked);
    }
    const favorite = document.getElementById('btn-bili-favorite');
    if (favorite) {
        favorite.textContent = biliDetail.favorited
            ? t('bili.favorite_done') : t('bili.favorite_add');
        favorite.classList.toggle('is-active', biliDetail.favorited);
    }
    // The coin buttons carry their label too: an empty button is a button
    // nobody dares to press.
    renderBiliDetailButtons();
}

/**
 * The plain labels of the buttons of the detail panel.
 *
 * The three buttons carry their text only once the details of a video are
 * loaded, so they stood empty until then -- a button without a word is a
 * button nobody dares to press.  This puts the plain label on them; the
 * open video replaces it with the state ("已赞", "已收藏").
 *
 * The labels are written every time, not only into an empty button: a
 * language switch has to change them too.
 */
function renderBiliDetailButtons() {
    const coin = document.getElementById('btn-bili-coin');
    if (coin) coin.textContent = t('bili.coin');
    const coinOne = document.getElementById('btn-bili-coin-one');
    if (coinOne) coinOne.textContent = t('bili.coin_one');
    const coinTwo = document.getElementById('btn-bili-coin-two');
    if (coinTwo) coinTwo.textContent = t('bili.coin_two');
    // The like and the favourite button carry the state of the open video.
    // Without one they get the plain label, so they are never empty.
    if (!biliDetail.bvid) {
        const like = document.getElementById('btn-bili-like');
        if (like) like.textContent = t('bili.like');
        const favorite = document.getElementById('btn-bili-favorite');
        if (favorite) favorite.textContent = t('bili.favorite_add');
    }
}

/** Read the counters of the video that is open. */
async function loadBiliDetailStat() {
    if (!biliDetail.bvid) return;
    if (!bridgeHas('get_bilibili_video_stat')) return;
    try {
        const result = await state.api.get_bilibili_video_stat(biliDetail.bvid);
        if (!result || !result.success) return;
        biliDetail.stat = result.stat || {};
        renderBiliDetailStats();
    } catch (err) {
        /* No counters: the panel is still usable without them. */
    }
}

/** Open the detail panel of a video and fill it. */
async function openBiliDetail(bvid, info) {
    const details = info || {};
    biliDetail.bvid = bvid || '';
    biliDetail.title = details.title || '';
    biliDetail.author = details.author || '';
    biliDetail.authorMid = details.authorMid || '';
    biliDetail.stat = {};
    biliDetail.liked = false;
    biliDetail.favorited = false;
    // 投币上限是「每个视频两枚」，所以这个数字跟着视频走：换一个视频就得
    // 从零开始，之前那个视频的枚数不该留在这里。
    biliDetail.coinGiven = 0;
    biliDetail.comments = [];
    biliDetail.commentPage = 1;
    biliDetail.commentHasMore = false;
    biliDetail.replyTo = null;
    biliDetail.danmaku = [];
    biliDetail.danmakuCursor = 0;
    biliDetail.danmakuMode = '';
    biliDetail.danmakuKeyword = '';
    clearBiliDanmakuLive();
    renderBiliDetailHeader();
    renderBiliDetailStats();
    renderBiliComments();
    renderBiliDanmaku();
    cancelBiliReply();
    await loadBiliDetailStat();
    await loadBiliComments(true);
    await loadBiliDanmaku();
}

/** Like the video that is open, or take the like back. */
async function likeBiliVideo() {
    const bvid = biliDetail.bvid;
    if (!bvid) return;
    if (!bridgeHas('bili_like_video')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const next = !biliDetail.liked;
    try {
        const result = await state.api.bili_like_video(bvid, next);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.like_done'));
            return;
        }
        biliDetail.liked = next;
        renderBiliDetailHeader();
        showToast('success', next ? t('bili.like_done') : t('bili.unlike_done'));
        await loadBiliDetailStat();
    } catch (err) {
        showToast('error', t('bili.like_done'));
    }
}

/**
 * Open the coin dialog and read the balance.
 *
 * 投币以前是贴在页面里的一块，投完不点掉就一直占着地方；现在是一个
 * 弹窗，右上角的 X、点背景、Esc 都能直接关，不需要先投一枚才能走。
 */
async function openBiliCoinPanel() {
    if (!biliDetail.bvid) return;
    openModal('bili-coin-modal');
    const box = document.getElementById('bili-coin-balance');
    if (box) box.textContent = '';
    if (!bridgeHas('get_bilibili_coin_balance')) return;
    try {
        const result = await state.api.get_bilibili_coin_balance();
        if (!result || !result.success) {
            if (box) box.textContent = biliFailure(result, 'bili.coin_failed');
            return;
        }
        if (box) box.textContent = t('bili.coin_balance', { count: result.balance || 0 });
    } catch (err) {
        if (box) box.textContent = t('bili.coin_failed');
    }
    await refreshBiliCoinState();
}

/**
 * How many coins this account already gave the open video.
 *
 * B站 allows two coins per submission.  The buttons show what is left instead
 * of letting the server refuse the click, and the hint says why they are gone.
 */
async function refreshBiliCoinState() {
    const bvid = biliDetail.bvid;
    if (!bvid || !bridgeHas('get_bilibili_coins_given')) return;
    let given = Number(biliDetail.coinGiven || 0);
    try {
        const result = await state.api.get_bilibili_coins_given(bvid);
        if (result && result.success) given = Number(result.given || 0);
    } catch (err) {
        /* keep the last known number */
    }
    biliDetail.coinGiven = given;
    const left = Math.max(0, 2 - given);
    const one = document.getElementById('btn-bili-coin-one');
    const two = document.getElementById('btn-bili-coin-two');
    if (one) one.disabled = left < 1;
    if (two) two.disabled = left < 2;
    const hint = document.getElementById('bili-coin-hint');
    if (hint) {
        hint.textContent = left > 0 ? t('bili.coin_left', { count: left }) : t('bili.coin_maxed');
    }
}

/** Throw one or two coins at the video that is open. */
async function coinBiliVideo(count) {
    const bvid = biliDetail.bvid;
    if (!bvid) return;
    if (!bridgeHas('bili_coin_video')) {
        showToast('error', t('error.unknown'));
        return;
    }
    // 先问一次服务器：这个视频已经投了几枚。数目是按视频算的，本地记着
    // 的那个数可能是上一个视频的，或者是上次点投币之前读到的旧值。
    await refreshBiliCoinState();
    if (count > Math.max(0, 2 - Number(biliDetail.coinGiven || 0))) {
        showToast('warning', t('bili.coin_maxed'));
        return;
    }
    try {
        const result = await state.api.bili_coin_video(bvid, count, false);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.coin_failed'));
            return;
        }
        showToast('success', t('bili.coin_success'));
        // 弹窗留着：余额和「已投满」的提示都要跟着变。
        await loadBiliDetailStat();
        await openBiliCoinPanel();
    } catch (err) {
        showToast('error', t('bili.coin_failed'));
    }
}

/**
 * Open the favourite picker.
 *
 * 不是「点一下某个收藏夹就等于收藏了」的一排按钮，而是勾选列表：B站一个
 * 视频本来就能同时进好几个收藏夹，勾上几个就是几个，取消勾选就是从那个
 * 收藏夹里拿出来。弹窗右上角的 X 和「取消」都能直接关掉，不必先选点什么
 * —— 以前那块贴在页面里的列表，选错了就只能硬着头皮选一个才能走。
 */
async function openBiliFavoritePanel() {
    const list = document.getElementById('bili-fav-list');
    const bvid = biliDetail.bvid;
    if (!list || !bvid) return;
    list.innerHTML = '';
    const loading = document.createElement('div');
    loading.className = 'muted';
    loading.textContent = t('common.loading');
    list.appendChild(loading);
    openModal('bili-fav-modal');
    const request = bridgeHas('get_bilibili_favorite_states')
        ? state.api.get_bilibili_favorite_states(bvid)
        : state.api.get_bilibili_favorites();
    try {
        const result = await request;
        const folders = (result && result.folders) || [];
        list.innerHTML = '';
        if (!folders.length) {
            list.appendChild(buildStateBlock('folder', t('bili.no_folders'), ''));
            return;
        }
        folders.forEach((folder) => {
            if (!folder) return;
            const row = document.createElement('label');
            row.className = 'bili-fav-item is-check';
            const check = document.createElement('input');
            check.type = 'checkbox';
            check.className = 'option checkbox';
            check.dataset.folder = String(folder.id || '');
            check.checked = !!folder.favoured;
            // 记下打开时是什么状态，保存时才知道哪些是这次改的。
            check.dataset.was = check.checked ? '1' : '0';
            const text = document.createElement('span');
            text.textContent = (folder.title || '') + ' · '
                + t('bili.folder_media_count', { count: folder.media_count || 0 });
            row.appendChild(check);
            row.appendChild(text);
            list.appendChild(row);
        });
    } catch (err) {
        list.innerHTML = '';
        list.appendChild(buildStateBlock('folder', t('bili.favorite_failed'), ''));
    }
}

/**
 * Send the picker: newly ticked folders in, unticked ones out, one request.
 *
 * 一次请求改完整个列表——和网页播放器一样：勾上的进 add_media_ids，
 * 取消勾选的进 del_media_ids，两边都不为空时也只发一条。
 */
async function saveBiliFavoriteSelection() {
    const bvid = biliDetail.bvid;
    const list = document.getElementById('bili-fav-list');
    if (!bvid || !list) return;
    const add = [];
    const remove = [];
    let stillFavoured = false;
    list.querySelectorAll('input[data-folder]').forEach((check) => {
        const id = check.dataset.folder;
        if (!id) return;
        if (check.checked) stillFavoured = true;
        if (check.checked && check.dataset.was !== '1') add.push(id);
        else if (!check.checked && check.dataset.was === '1') remove.push(id);
    });
    if (!add.length && !remove.length) {
        closeModal('bili-fav-modal');
        return;
    }
    if (!bridgeHas('bili_favorite_video_multi')) {
        // 老后端只有单个收藏夹的接口：挑了多个就分开一个个来。
        for (const folderId of add) {
            await state.api.bili_favorite_video(bvid, folderId, false);
        }
        for (const folderId of remove) {
            await state.api.bili_favorite_video(bvid, folderId, true);
        }
        biliDetail.favorited = stillFavoured;
        renderBiliDetailHeader();
        closeModal('bili-fav-modal');
        showToast('success', t('bili.favorite_saved'));
        await loadBiliDetailStat();
        return;
    }
    const button = document.getElementById('btn-bili-fav-save');
    if (button) button.disabled = true;
    try {
        const result = await state.api.bili_favorite_video_multi(bvid, add, remove);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.favorite_failed'));
            return;
        }
        biliDetail.favorited = stillFavoured;
        renderBiliDetailHeader();
        closeModal('bili-fav-modal');
        showToast('success', t('bili.favorite_saved'));
        await loadBiliDetailStat();
    } catch (err) {
        showToast('error', t('bili.favorite_failed'));
    } finally {
        if (button) button.disabled = false;
    }
}

/* -------------------------------------------------------------- comments */

/**
 * The replies of a comment that were not drawn yet.
 *
 * The backend keeps three replies of a comment at most; the rest is
 * behind the button, and the button is gone once the thread is open.
 */
const biliCommentThreads = {
    replies: {},
    loading: {}
};

/**
 * The rpid of the thread a comment belongs to.
 *
 * The comment on top of a thread is its own root; a reply knows the root
 * it hangs on.  Both are strings, and the string '0' means "there is
 * none" -- reading them with their truth would take '0' for a real id.
 */
function biliCommentRoot(comment, threadRoot) {
    if (!comment) return '';
    if (comment.root && comment.root !== '0') return comment.root;
    if (threadRoot) return threadRoot;
    return comment.rpid || '';
}

/** Whether the account has liked this comment. */
function biliCommentLiked(comment) {
    if (!comment || !comment.rpid) return false;
    return !!comment.liked;
}

/** The replies that are shown under a comment. */
function biliCommentChildren(comment, rootRpid, isRoot) {
    if (!comment) return [];
    if (isRoot && rootRpid && biliCommentThreads.replies[rootRpid]) {
        return biliCommentThreads.replies[rootRpid];
    }
    return comment.children || [];
}

/** One comment, with the replies the backend kept. */
function biliCommentElement(comment, child, threadRoot) {
    if (!comment) return null;
    const row = document.createElement('div');
    row.className = child ? 'bili-comment is-child' : 'bili-comment';
    const head = document.createElement('div');
    head.className = 'bili-comment-head';
    const face = document.createElement('img');
    face.className = 'bili-comment-face';
    face.alt = '';
    face.loading = 'lazy';
    if (comment.face) {
        face.src = biliImageSrc(comment.face);
        face.addEventListener('error', () => { face.remove(); });
    }
    head.appendChild(face);
    const name = document.createElement('button');
    name.className = 'bili-comment-name';
    name.type = 'button';
    name.textContent = comment.uname || '';
    name.addEventListener('click', () => {
        if (comment.mid) openBiliSpace(comment.mid);
    });
    head.appendChild(name);
    if (comment.level) {
        const level = document.createElement('span');
        level.className = 'source-badge is-bili';
        level.textContent = t('bili.level', { level: comment.level });
        head.appendChild(level);
    }
    row.appendChild(head);
    const body = document.createElement('div');
    body.className = 'bili-comment-body';
    body.textContent = comment.content || '';
    row.appendChild(body);
    const meta = document.createElement('div');
    meta.className = 'bili-comment-meta';
    if (comment.location) {
        const place = document.createElement('span');
        place.textContent = comment.location;
        meta.appendChild(place);
    }
    const reply = document.createElement('button');
    reply.className = 'btn btn-sm btn-link';
    reply.type = 'button';
    reply.textContent = t('bili.comment_reply');
    reply.addEventListener('click', () => startBiliReply(comment, threadRoot));
    meta.appendChild(reply);
    const like = document.createElement('button');
    like.className = 'btn btn-sm btn-link';
    like.type = 'button';
    like.textContent = biliCommentLikeLabel(comment);
    like.classList.toggle('is-liked', biliCommentLiked(comment));
    like.addEventListener('click', () => likeBiliComment(comment, like));
    meta.appendChild(like);
    row.appendChild(meta);
    const isRoot = !child;
    const rootRpid = isRoot ? (comment.rpid || '') : biliCommentRoot(comment, threadRoot);
    const children = biliCommentChildren(comment, rootRpid, isRoot);
    children.forEach((childComment) => {
        const node = biliCommentElement(childComment, true, rootRpid);
        if (node) row.appendChild(node);
    });
    // The button that opens the thread: only for the comment on top.
    if (isRoot) {
        const count = Number(comment.reply_count || 0);
        const shown = (comment.children || []).length;
        if (count > shown && !biliCommentThreads.replies[rootRpid]) {
            const more = document.createElement('button');
            more.className = 'btn btn-sm btn-link bili-comment-replies';
            more.type = 'button';
            more.disabled = !!biliCommentThreads.loading[rootRpid];
            more.textContent = biliCommentThreads.loading[rootRpid]
                ? t('bili.comment_replies_loading')
                : t('bili.comment_replies', { count: count });
            more.addEventListener('click', () => expandBiliCommentReplies(comment, rootRpid));
            row.appendChild(more);
        }
    }
    return row;
}

/** The text of the like button: the word and the counter. */
function biliCommentLikeLabel(comment) {
    const word = biliCommentLiked(comment) ? t('bili.comment_liked') : t('bili.comment_like');
    return word + ' ' + formatPlayCount(comment.like || 0);
}

/** Load every reply of a thread and show them all. */
async function expandBiliCommentReplies(comment, rootRpid) {
    if (!comment || !rootRpid) return;
    if (!bridgeHas('get_bilibili_comment_replies')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const bvid = biliDetail.bvid;
    if (!bvid) return;
    if (biliCommentThreads.loading[rootRpid]) return;
    biliCommentThreads.loading[rootRpid] = true;
    renderBiliComments();
    try {
        const replies = [];
        let page = 1;
        let more = true;
        while (more && page <= 10) {
            const result = await state.api.get_bilibili_comment_replies(bvid, rootRpid, page);
            if (!result || !result.success) {
                showToast('error', biliFailure(result, 'bili.comment_failed'));
                return;
            }
            const items = result.comments || [];
            items.forEach((item) => { if (item) replies.push(item); });
            more = !!result.has_more && items.length > 0;
            page += 1;
        }
        if (replies.length) biliCommentThreads.replies[rootRpid] = replies;
    } catch (err) {
        showToast('error', t('bili.comment_failed'));
    } finally {
        delete biliCommentThreads.loading[rootRpid];
        renderBiliComments();
    }
}

/** Draw the comment list. */
function renderBiliComments() {
    const list = document.getElementById('bili-comment-list');
    if (!list) return;
    list.innerHTML = '';
    if (!biliDetail.comments.length) {
        list.appendChild(buildStateBlock('message-circle', t('bili.comment_empty'), ''));
    } else {
        biliDetail.comments.forEach((comment) => {
            if (!comment) return;
            list.appendChild(biliCommentElement(comment, false));
        });
    }
    const more = document.getElementById('btn-bili-comment-more');
    if (more) {
        more.hidden = !biliDetail.commentHasMore || biliDetail.commentLoading;
        more.disabled = biliDetail.commentLoading;
        more.textContent = biliDetail.commentLoading
            ? t('common.loading') : t('bili.load_more');
    }
}

/** Read the comments of the video that is open. */
async function loadBiliComments(reset) {
    const bvid = biliDetail.bvid;
    if (!bvid) return;
    if (!bridgeHas('get_bilibili_comments')) {
        showToast('error', t('error.unknown'));
        return;
    }
    if (biliDetail.commentLoading) return;
    biliDetail.commentLoading = true;
    renderBiliComments();
    try {
        const page = reset ? 1 : biliDetail.commentPage + 1;
        const result = await state.api.get_bilibili_comments(bvid, page, 'hot');
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.comment_failed'));
            return;
        }
        const items = result.comments || [];
        if (reset) {
            biliDetail.comments = [];
            // The threads belong to the comments that were read before.
            biliCommentThreads.replies = {};
        }
        items.forEach((comment) => {
            if (comment) biliDetail.comments.push(comment);
        });
        biliDetail.commentPage = page;
        biliDetail.commentHasMore = !!result.has_more;
    } catch (err) {
        showToast('error', t('bili.comment_failed'));
    } finally {
        biliDetail.commentLoading = false;
        renderBiliComments();
    }
}

/** Write a comment, or a reply to the one that was picked. */
async function sendBiliComment() {
    const input = document.getElementById('bili-comment-input');
    const text = (input && input.value ? input.value : '').trim();
    if (!text) return;
    const bvid = biliDetail.bvid;
    if (!bvid) return;
    if (!bridgeHas('bili_post_comment')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const parent = biliDetail.replyTo ? biliDetail.replyTo.rpid : 0;
    const root = biliDetail.replyTo ? biliDetail.replyTo.root : 0;
    try {
        const result = await state.api.bili_post_comment(bvid, text, parent, root);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.comment_failed'));
            return;
        }
        if (input) input.value = '';
        cancelBiliReply();
        showToast('success', t('bili.like_done'));
        await loadBiliComments(true);
    } catch (err) {
        showToast('error', t('bili.comment_failed'));
    }
}

/**
 * Remember which comment the next message answers.
 *
 * A reply hangs on the comment it answers; the thread it belongs to is
 * the one of the comment on top.
 */
function startBiliReply(comment, threadRoot) {
    if (!comment) return;
    const root = comment.root && comment.root !== '0'
        ? comment.root
        : (threadRoot || comment.rpid || 0);
    biliDetail.replyTo = {
        rpid: comment.rpid || 0,
        root: root,
        name: comment.uname || ''
    };
    const bar = document.getElementById('bili-comment-reply');
    const target = document.getElementById('bili-comment-reply-target');
    if (target) target.textContent = t('bili.comment_reply_to', { name: comment.uname || '' });
    if (bar) bar.hidden = false;
    const input = document.getElementById('bili-comment-input');
    if (input) input.focus();
}

/** Forget the comment the next message would answer. */
function cancelBiliReply() {
    biliDetail.replyTo = null;
    const bar = document.getElementById('bili-comment-reply');
    if (bar) bar.hidden = true;
}

/** Like a comment, or take the like back. */
async function likeBiliComment(comment, button) {
    if (!comment || !comment.rpid) return;
    if (!bridgeHas('bili_like_comment')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const liked = biliCommentLiked(comment);
    try {
        const result = await state.api.bili_like_comment(comment.rpid, !liked);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.comment_failed'));
            return;
        }
        comment.liked = !liked;
        comment.like = Math.max(0, Number(comment.like || 0) + (liked ? -1 : 1));
        if (button) {
            button.textContent = biliCommentLikeLabel(comment);
            button.classList.toggle('is-liked', biliCommentLiked(comment));
        }
    } catch (err) {
        showToast('error', t('bili.comment_failed'));
    }
}

/* --------------------------------------------------------------- danmaku */

/**
 * Put the cursor on the danmaku that come after the given second.
 *
 * The cursor says how far the list has been read; when the filter changes
 * the list changes too, so the cursor has to be put back to the place the
 * video is at -- otherwise a burst of old danmaku lands on the picture.
 */
function resetBiliDanmakuCursor(currentTime) {
    const visible = biliVisibleDanmaku();
    const now = Number(currentTime || 0);
    let index = 0;
    while (index < visible.length && Number(visible[index].time || 0) < now) index += 1;
    biliDetail.danmakuCursor = index;
}

/** The danmaku that pass the current filter. */
function biliVisibleDanmaku() {
    const mode = biliDetail.danmakuMode;
    const keyword = (biliDetail.danmakuKeyword || '').trim().toLowerCase();
    const modes = mode === 'scroll' ? [1] : (mode === 'bottom' ? [4] : (mode === 'top' ? [5] : []));
    return biliDetail.danmaku.filter((item) => {
        if (!item) return false;
        if (modes.length && modes.indexOf(Number(item.mode || 0)) < 0) return false;
        if (keyword && String(item.text || '').toLowerCase().indexOf(keyword) < 0) return false;
        return true;
    });
}

/** The chips that pick the position of the danmaku that are shown. */
function renderBiliDanmakuModes() {
    const row = document.getElementById('bili-danmaku-modes');
    if (!row) return;
    row.innerHTML = '';
    const modes = [
        { key: 'scroll', label: 'bili.danmaku_mode_scroll' },
        { key: 'bottom', label: 'bili.danmaku_mode_bottom' },
        { key: 'top', label: 'bili.danmaku_mode_top' }
    ];
    modes.forEach((mode) => {
        const chip = document.createElement('button');
        chip.className = biliDetail.danmakuMode === mode.key ? 'chip is-active' : 'chip';
        chip.type = 'button';
        chip.textContent = t(mode.label);
        chip.addEventListener('click', () => {
            // Clicking the chip that is already on turns the filter off again.
            biliDetail.danmakuMode = biliDetail.danmakuMode === mode.key ? '' : mode.key;
            renderBiliDanmakuModes();
            resetBiliDanmakuCursor(biliVideoTime());
            renderBiliDanmaku();
        });
        row.appendChild(chip);
    });
}

/** Draw the danmaku list and say how many the filter hid. */
function renderBiliDanmaku() {
    const list = document.getElementById('bili-danmaku-list');
    if (!list) return;
    list.innerHTML = '';
    const items = biliVisibleDanmaku();
    if (!items.length) {
        list.appendChild(buildStateBlock('message-square', t('bili.danmaku_empty'), ''));
    } else {
        items.forEach((item) => {
            const row = document.createElement('div');
            row.className = 'bili-danmaku-row';
            const time = document.createElement('span');
            time.className = 'bili-danmaku-time';
            time.textContent = fmtTime(item.time || 0);
            row.appendChild(time);
            const text = document.createElement('span');
            text.className = 'bili-danmaku-text';
            text.textContent = item.text || '';
            row.appendChild(text);
            list.appendChild(row);
        });
    }
    const hidden = document.getElementById('bili-danmaku-hidden');
    if (hidden) {
        const count = biliDetail.danmaku.length - items.length;
        hidden.hidden = count <= 0;
        hidden.textContent = t('bili.danmaku_hidden', { count: count });
    }
}

/** Read the danmaku of the video that is open. */
async function loadBiliDanmaku() {
    const bvid = biliDetail.bvid;
    if (!bvid) return;
    if (!bridgeHas('get_bilibili_danmaku')) return;
    biliDetail.danmakuLoading = true;
    try {
        const result = await state.api.get_bilibili_danmaku(bvid, 0);
        if (!result || !result.success) return;
        biliDetail.danmaku = result.items || [];
        resetBiliDanmakuCursor(biliVideoTime());
        renderBiliDanmaku();
    } catch (err) {
        /* No danmaku: nothing to show. */
    } finally {
        biliDetail.danmakuLoading = false;
    }
}

/** Write one danmaku. */
async function sendBiliDanmaku() {
    const input = document.getElementById('bili-danmaku-input');
    const text = (input && input.value ? input.value : '').trim();
    if (!text) return;
    const bvid = biliDetail.bvid;
    if (!bvid) return;
    if (!bridgeHas('bili_send_danmaku')) {
        showToast('error', t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.bili_send_danmaku(bvid, text, 0, 16777215, 25, 1);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.danmaku_failed'));
            return;
        }
        if (input) input.value = '';
        showToast('success', t('bili.danmaku_send'));
        await loadBiliDanmaku();
    } catch (err) {
        showToast('error', t('bili.danmaku_failed'));
    }
}

/** The second the video is at, or zero when nothing plays. */
function biliVideoTime() {
    const videoEl = document.getElementById('bili-video');
    return videoEl ? Number(videoEl.currentTime || 0) : 0;
}

/** Take every danmaku off the picture. */
function clearBiliDanmakuLive() {
    biliDanmakuLive.length = 0;
    biliDanmakuRows.length = 0;
    const layer = document.getElementById('bili-danmaku-layer');
    if (layer) layer.innerHTML = '';
}

/**
 * The row a new danmaku may use.
 *
 * Two danmaku that scroll over the same line at the same time would sit on
 * top of each other, so every row remembers until when it is busy.
 */
function pickBiliDanmakuRow(now) {
    for (let index = 0; index < biliDanmakuRows.length; index += 1) {
        if (biliDanmakuRows[index] <= now) {
            biliDanmakuRows[index] = now + 8;
            return index;
        }
    }
    biliDanmakuRows.push(now + 8);
    return biliDanmakuRows.length - 1;
}

/**
 * Show the danmaku that belong to the time the video is at.
 *
 * The danmaku are sorted by time, so a cursor says how far we got; every
 * danmaku that passes the filter is put on the picture once and scrolls
 * away on its own.
 */
function updateBiliDanmakuLive(currentTime) {
    const layer = document.getElementById('bili-danmaku-layer');
    if (!layer) return;
    const now = Number(currentTime || 0);
    const visible = biliVisibleDanmaku();
    for (let index = biliDanmakuLive.length - 1; index >= 0; index -= 1) {
        if (biliDanmakuLive[index].start + 8 < now) {
            const entry = biliDanmakuLive[index];
            if (entry.el && entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
            biliDanmakuLive.splice(index, 1);
        }
    }
    while (biliDetail.danmakuCursor < visible.length) {
        const item = visible[biliDetail.danmakuCursor];
        biliDetail.danmakuCursor += 1;
        if (!item || Number(item.time || 0) > now) continue;
        const el = document.createElement('span');
        el.className = 'bili-danmaku-live';
        const mode = Number(item.mode || 0);
        if (mode === 4) el.classList.add('is-bottom');
        if (mode === 5) el.classList.add('is-top');
        el.textContent = item.text || '';
        el.style.color = '#' + ('000000' + (Number(item.color || 16777215)).toString(16)).slice(-6);
        if (mode === 1 || mode === 0) {
            // The danmaku starts at the right edge of the picture, not at the
            // right edge of the window: the layer is narrower than the
            // window, and starting further away only made the danmaku fly
            // in so slowly that it looked like nothing was happening.
            el.style.setProperty('--bili-start', (layer.clientWidth || 640) + 'px');
            const row = pickBiliDanmakuRow(now);
            el.style.top = (6 + row * 24) + 'px';
        }
        layer.appendChild(el);
        biliDanmakuLive.push({ el: el, start: now });
    }
}

/* --------------------------------------------------------- watch history */

/** The watched videos. */
const biliHistory = {
    items: [],
    page: 1,
    hasMore: false,
    loading: false,
    error: ''
};

/** Turn a history entry into a card the list understands. */
function biliHistoryTrack(item) {
    return {
        bvid: item.bvid || '',
        name: item.title || '',
        artists: item.author || '',
        author_mid: item.author_mid || '',
        cover_url: item.cover || '',
        duration: item.duration || 0,
        play_count: 0,
        url: 'https://www.bilibili.com/video/' + (item.bvid || '')
    };
}

/** Read the watched videos. */
async function loadBiliHistory(reset) {
    if (biliHistory.loading) return;
    if (!bridgeHas('get_bilibili_history')) {
        showToast('error', t('error.unknown'));
        return;
    }
    biliHistory.loading = true;
    renderBiliHistory();
    try {
        const page = reset ? 1 : biliHistory.page + 1;
        const result = await state.api.get_bilibili_history(page, 30);
        if (!result || !result.success) {
            biliHistory.error = biliFailure(result, 'bili.favorite_login_hint');
            return;
        }
        const items = result.items || [];
        if (reset) biliHistory.items = [];
        items.forEach((item) => {
            if (item) biliHistory.items.push(item);
        });
        biliHistory.page = page;
        biliHistory.hasMore = !!result.has_more;
        biliHistory.error = '';
    } catch (err) {
        biliHistory.error = t('error.network');
    } finally {
        biliHistory.loading = false;
        renderBiliHistory();
    }
}

/** Draw the watched videos. */
function renderBiliHistory() {
    const grid = document.getElementById('bili-history-results');
    const box = document.getElementById('bili-history-state');
    if (!grid) return;
    grid.innerHTML = '';
    if (box) box.innerHTML = '';
    if (!biliAccount.loggedIn) {
        if (box) box.appendChild(buildStateBlock('user-plus', t('bili.favorite_login_hint'), ''));
        return;
    }
    if (biliHistory.error && box) {
        box.appendChild(buildStateBlock('close', t('bili.error_title'),
            t('bili.error_hint', { message: biliHistory.error })));
        return;
    }
    if (!biliHistory.items.length) {
        if (box) box.appendChild(buildStateBlock('list', t('bili.history_empty'), ''));
        return;
    }
    biliHistory.items.forEach((item) => {
        grid.appendChild(biliCardElement(biliHistoryTrack(item)));
    });
    const more = document.getElementById('btn-bili-history-more');
    if (more) {
        more.hidden = !biliHistory.hasMore || biliHistory.loading;
        more.disabled = biliHistory.loading;
        more.textContent = biliHistory.loading ? t('common.loading') : t('bili.load_more');
    }
    const end = document.getElementById('bili-history-end');
    if (end) {
        end.hidden = biliHistory.loading || !biliHistory.items.length || biliHistory.hasMore;
        end.textContent = t('bili.no_more');
    }
}

/* --------------------------------------------------------- uploader page */

/** The uploader whose page is open. */
const biliSpace = {
    mid: '',
    profile: {},
    items: [],
    page: 1,
    hasMore: false,
    following: false,
    loading: false,
    error: ''
};

/** Open the page of an uploader. */
async function openBiliSpace(mid) {
    const id = String(mid || '');
    if (!id) return;
    biliSpace.mid = id;
    biliSpace.profile = {};
    biliSpace.items = [];
    biliSpace.page = 1;
    biliSpace.hasMore = false;
    biliSpace.following = false;
    biliSpace.error = '';
    openModal('bili-space-modal');
    renderBiliSpace();
    if (bridgeHas('get_bilibili_space')) {
        try {
            const result = await state.api.get_bilibili_space(id);
            if (result && result.success) {
                biliSpace.profile = result.profile || {};
            } else {
                biliSpace.error = biliFailure(result, 'error.network');
            }
        } catch (err) {
            biliSpace.error = t('error.network');
        }
    }
    if (bridgeHas('get_bilibili_follow_state')) {
        try {
            const result = await state.api.get_bilibili_follow_state(id);
            biliSpace.following = !!(result && result.following);
        } catch (err) {
            biliSpace.following = false;
        }
    }
    renderBiliSpace();
    await loadBiliSpaceVideos(true);
}

/** Read the videos of the uploader whose page is open. */
async function loadBiliSpaceVideos(reset) {
    const mid = biliSpace.mid;
    if (!mid) return;
    if (!bridgeHas('get_bilibili_space_videos')) return;
    if (biliSpace.loading) return;
    biliSpace.loading = true;
    renderBiliSpace();
    try {
        const page = reset ? 1 : biliSpace.page + 1;
        const result = await state.api.get_bilibili_space_videos(mid, page, 30);
        if (!result || !result.success) {
            biliSpace.error = biliFailure(result, 'error.network');
            return;
        }
        const items = result.items || [];
        if (reset) biliSpace.items = [];
        items.forEach((item) => {
            if (item) biliSpace.items.push(item);
        });
        biliSpace.page = page;
        biliSpace.hasMore = !!result.has_more;
    } catch (err) {
        biliSpace.error = t('error.network');
    } finally {
        biliSpace.loading = false;
        renderBiliSpace();
    }
}

/** Draw the uploader's page. */
function renderBiliSpace() {
    const face = document.getElementById('bili-space-face');
    const name = document.getElementById('bili-space-name');
    const meta = document.getElementById('bili-space-meta');
    const sign = document.getElementById('bili-space-sign');
    const follow = document.getElementById('btn-bili-space-follow');
    const videos = document.getElementById('bili-space-videos');
    const box = document.getElementById('bili-space-state');
    const profile = biliSpace.profile || {};
    if (face) {
        if (profile.face) {
            face.src = biliImageSrc(profile.face);
            face.hidden = false;
        } else {
            face.hidden = true;
        }
    }
    // On the user's own page the identity opens the account center; on
    // somebody else's page it is just a name.
    const ownSpace = !!biliAccount.loggedIn
        && String(biliAccount.mid) === String(biliSpace.mid);
    if (name) {
        name.textContent = profile.name || '';
        name.classList.toggle('is-link', ownSpace);
        name.onclick = ownSpace ? () => openAccountCenter() : null;
    }
    if (face) {
        face.classList.toggle('is-link', ownSpace);
        face.onclick = ownSpace ? () => openAccountCenter() : null;
    }
    if (meta) {
        const parts = [];
        if (profile.level) parts.push(t('bili.level', { level: profile.level }));
        parts.push(t('bili.followers', { count: formatPlayCount(profile.fans || 0) }));
        parts.push(t('bili.following_count', { count: formatPlayCount(profile.following || 0) }));
        parts.push(t('bili.videos_count', { count: formatPlayCount(profile.video_count || 0) }));
        meta.textContent = parts.join(' · ');
    }
    if (sign) sign.textContent = profile.sign || '';
    if (follow) {
        follow.textContent = biliSpace.following ? t('bili.following') : t('bili.follow');
        follow.classList.toggle('is-active', biliSpace.following);
    }
    if (videos) {
        videos.innerHTML = '';
        if (!biliSpace.items.length) {
            if (box) {
                box.innerHTML = '';
                box.appendChild(buildStateBlock('film', t('bili.space_empty'), ''));
            }
        } else {
            biliSpace.items.forEach((item) => {
                videos.appendChild(biliCardElement({
                    bvid: item.bvid || '',
                    name: item.title || '',
                    artists: item.author || '',
                    author_mid: item.author_mid || biliSpace.mid,
                    cover_url: item.cover || '',
                    duration: item.duration || 0,
                    play_count: item.play_count || 0,
                    url: 'https://www.bilibili.com/video/' + (item.bvid || '')
                }));
            });
        }
    }
    const more = document.getElementById('btn-bili-space-more');
    if (more) {
        more.hidden = !biliSpace.hasMore || biliSpace.loading;
        more.disabled = biliSpace.loading;
        more.textContent = biliSpace.loading ? t('common.loading') : t('bili.load_more');
    }
}

/** Follow the uploader whose page is open, or stop following. */
async function toggleBiliFollow() {
    const mid = biliSpace.mid;
    if (!mid) return;
    if (!bridgeHas('bili_follow_user')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const next = !biliSpace.following;
    try {
        const result = await state.api.bili_follow_user(mid, next);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'error.unknown'));
            return;
        }
        biliSpace.following = next;
        renderBiliSpace();
        showToast('success', next ? t('bili.follow') : t('bili.unfollow'));
    } catch (err) {
        showToast('error', t('error.unknown'));
    }
}

/* -------------------------------------------------------- private messages */

/** The conversations and the message that is open. */
const biliIm = {
    sessions: [],
    sessionId: '',
    talkerId: '',
    name: '',
    messages: [],
    loading: false,
    error: ''
};

/** Open the private messages. */
async function openBiliMessages() {
    biliIm.error = '';
    openModal('bili-messages-modal');
    renderBiliMessages();
    await loadBiliMessages();
}

/** Read the conversations. */
async function loadBiliMessages() {
    if (!bridgeHas('get_bilibili_messages')) {
        showToast('error', t('error.unknown'));
        return;
    }
    biliIm.loading = true;
    renderBiliMessages();
    try {
        const result = await state.api.get_bilibili_messages(20);
        if (!result || !result.success) {
            biliIm.error = biliFailure(result, 'bili.message_failed');
            return;
        }
        biliIm.sessions = result.items || [];
        biliIm.error = '';
        if (!biliIm.talkerId && biliIm.sessions.length) {
            await openBiliConversation(biliIm.sessions[0]);
        }
    } catch (err) {
        biliIm.error = t('error.network');
    } finally {
        biliIm.loading = false;
        renderBiliMessages();
    }
}

/** Open one conversation. */
async function openBiliConversation(session) {
    if (!session) return;
    biliIm.sessionId = session.session_id || '';
    biliIm.talkerId = session.talker_id || '';
    biliIm.name = session.name || '';
    biliIm.messages = [];
    renderBiliMessages();
    if (!bridgeHas('get_bilibili_message_history')) return;
    try {
        const result = await state.api.get_bilibili_message_history(
            biliIm.sessionId, biliIm.talkerId, 20);
        if (!result || !result.success) {
            biliIm.error = biliFailure(result, 'bili.message_failed');
            return;
        }
        biliIm.messages = result.items || [];
        biliIm.error = '';
    } catch (err) {
        biliIm.error = t('error.network');
    }
    renderBiliMessages();
}

/** Draw the conversations and the messages. */
function renderBiliMessages() {
    const sessions = document.getElementById('bili-im-sessions');
    const head = document.getElementById('bili-im-head');
    const messages = document.getElementById('bili-im-messages');
    const box = document.getElementById('bili-im-state');
    if (sessions) {
        sessions.innerHTML = '';
        if (!biliIm.sessions.length) {
            sessions.appendChild(buildStateBlock('message-circle', t('bili.message_empty'), ''));
        } else {
            biliIm.sessions.forEach((session) => {
                if (!session) return;
                const button = document.createElement('button');
                button.className = biliIm.talkerId === session.talker_id
                    ? 'bili-im-session is-active' : 'bili-im-session';
                button.type = 'button';
                // 每个会话左边是对方的头像：私信列表里认人靠它，光有名字不够。
                const ident = document.createElement('span');
                ident.className = 'bili-im-ident';
                if (session.face) {
                    const face = document.createElement('img');
                    face.className = 'bili-im-face';
                    face.alt = '';
                    face.loading = 'lazy';
                    face.src = biliImageSrc(session.face);
                    face.addEventListener('error', () => { face.remove(); });
                    ident.appendChild(face);
                }
                const name = document.createElement('span');
                name.className = 'bili-im-session-name';
                name.textContent = session.name || '';
                ident.appendChild(name);
                button.appendChild(ident);
                if (session.unread) {
                    const unread = document.createElement('span');
                    unread.className = 'source-badge is-bili';
                    unread.textContent = t('bili.message_unread', { count: session.unread });
                    button.appendChild(unread);
                }
                button.addEventListener('click', () => openBiliConversation(session));
                sessions.appendChild(button);
            });
        }
    }
    if (head) {
        head.innerHTML = '';
        const talking = biliIm.sessions.find((item) => item && item.talker_id === biliIm.talkerId);
        if (talking && talking.face) {
            const face = document.createElement('img');
            face.className = 'bili-im-face';
            face.alt = '';
            face.src = biliImageSrc(talking.face);
            face.addEventListener('error', () => { face.remove(); });
            head.appendChild(face);
        }
        const headName = document.createElement('span');
        headName.textContent = biliIm.name || '';
        head.appendChild(headName);
    }
    if (messages) {
        messages.innerHTML = '';
        if (!biliIm.messages.length) {
            messages.appendChild(buildStateBlock('message-square', t('bili.message_empty'), ''));
        } else {
            biliIm.messages.forEach((message) => {
                if (!message) return;
                const row = document.createElement('div');
                row.className = message.is_self
                    ? 'bili-im-message is-self' : 'bili-im-message';
                const text = document.createElement('span');
                text.textContent = message.content || '';
                row.appendChild(text);
                messages.appendChild(row);
            });
        }
    }
    if (box) {
        box.innerHTML = '';
        if (biliIm.error) {
            box.appendChild(buildStateBlock('close', t('bili.error_title'),
                t('bili.error_hint', { message: biliIm.error })));
        }
    }
}

/** Send a private message. */
async function sendBiliMessage() {
    const input = document.getElementById('bili-im-input');
    const text = (input && input.value ? input.value : '').trim();
    if (!text) return;
    if (!biliIm.talkerId) {
        showToast('error', t('bili.message_failed'));
        return;
    }
    if (!bridgeHas('bili_send_message')) {
        showToast('error', t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.bili_send_message(
            biliIm.talkerId, text, biliIm.sessionId);
        if (!result || !result.success) {
            showToast('error', biliFailure(result, 'bili.message_failed'));
            return;
        }
        if (input) input.value = '';
        showToast('success', t('bili.message_send'));
        await openBiliConversation({
            session_id: biliIm.sessionId,
            talker_id: biliIm.talkerId,
            name: biliIm.name
        });
    } catch (err) {
        showToast('error', t('bili.message_failed'));
    }
}

/* ---------------------------------------------------------------- upload */

/** The video that is about to be posted. */
const biliUpload = {
    videoPath: '',
    coverPath: '',
    categories: [],
    steps: []
};

/** Open the upload panel and read the categories. */
async function openBiliUpload() {
    biliUpload.videoPath = '';
    biliUpload.coverPath = '';
    biliUpload.steps = [];
    openModal('bili-upload-modal');
    const videoPath = document.getElementById('bili-upload-video-path');
    if (videoPath) videoPath.textContent = '';
    const coverPath = document.getElementById('bili-upload-cover-path');
    if (coverPath) coverPath.textContent = '';
    const progress = document.getElementById('bili-upload-progress');
    if (progress) progress.hidden = true;
    const error = document.getElementById('bili-upload-error');
    if (error) error.hidden = true;
    renderBiliUploadSteps();
    await loadBiliUploadCategories();
}

/** Read the categories a video can be posted to. */
async function loadBiliUploadCategories() {
    const select = document.getElementById('bili-upload-category');
    if (!select) return;
    if (!bridgeHas('get_bilibili_upload_categories')) return;
    try {
        const result = await state.api.get_bilibili_upload_categories();
        const categories = (result && result.categories) || [];
        biliUpload.categories = categories;
        select.innerHTML = '';
        categories.forEach((category) => {
            if (!category) return;
            const option = document.createElement('option');
            option.value = String(category.tid || '');
            option.textContent = category.name || '';
            select.appendChild(option);
        });
    } catch (err) {
        showToast('error', t('bili.upload_failed'));
    }
}

/** Pick the video file to post. */
async function pickBiliUploadVideo() {
    if (!bridgeHas('choose_file')) return;
    try {
        const result = await state.api.choose_file('video');
        if (!result || !result.success || !result.path) return;
        biliUpload.videoPath = result.path;
        const box = document.getElementById('bili-upload-video-path');
        if (box) box.textContent = result.path;
    } catch (err) {
        showToast('error', t('bili.upload_failed'));
    }
}

/** Pick the cover of the video to post. */
async function pickBiliUploadCover() {
    if (!bridgeHas('choose_file')) return;
    try {
        const result = await state.api.choose_file('image');
        if (!result || !result.success || !result.path) return;
        biliUpload.coverPath = result.path;
        const box = document.getElementById('bili-upload-cover-path');
        if (box) box.textContent = result.path;
    } catch (err) {
        showToast('error', t('bili.upload_failed'));
    }
}

/** Draw the steps of the upload. */
function renderBiliUploadSteps() {
    const box = document.getElementById('bili-upload-steps');
    if (!box) return;
    box.innerHTML = '';
    biliUpload.steps.forEach((step) => {
        if (!step) return;
        const row = document.createElement('div');
        row.className = step.ok ? 'bili-upload-step is-ok' : 'bili-upload-step is-failed';
        const name = document.createElement('span');
        name.className = 'bili-upload-step-name';
        name.textContent = step.name || '';
        row.appendChild(name);
        const message = document.createElement('span');
        message.className = 'bili-upload-step-message';
        message.textContent = step.message || '';
        row.appendChild(message);
        box.appendChild(row);
    });
}

/** Post the video to Bilibili. */
async function submitBiliUpload() {
    if (!biliUpload.videoPath) {
        showToast('warning', t('bili.upload_no_video'));
        return;
    }
    const title = document.getElementById('bili-upload-title');
    const select = document.getElementById('bili-upload-category');
    const desc = document.getElementById('bili-upload-desc');
    const tags = document.getElementById('bili-upload-tags');
    const name = title ? title.value.trim() : '';
    const tid = select ? Number(select.value || 0) : 0;
    if (!name) {
        showToast('warning', t('bili.upload_title'));
        return;
    }
    if (!tid) {
        showToast('warning', t('bili.upload_category'));
        return;
    }
    if (!bridgeHas('bili_upload_video')) {
        showToast('error', t('error.unknown'));
        return;
    }
    const progress = document.getElementById('bili-upload-progress');
    if (progress) progress.hidden = false;
    const error = document.getElementById('bili-upload-error');
    if (error) {
        error.hidden = true;
        error.textContent = '';
    }
    biliUpload.steps = [];
    renderBiliUploadSteps();
    try {
        const result = await state.api.bili_upload_video(
            biliUpload.videoPath, name, tid,
            desc ? desc.value : '', tags ? tags.value : '', biliUpload.coverPath);
        biliUpload.steps = (result && result.steps) || [];
        renderBiliUploadSteps();
        if (!result || !result.success) {
            const message = biliFailure(result, 'bili.upload_failed');
            if (error) {
                error.hidden = false;
                error.textContent = message;
            }
            showToast('error', message, 9000);
            return;
        }
        showToast('success', t('bili.upload_done'), 6000);
        closeModal('bili-upload-modal');
    } catch (err) {
        showToast('error', t('bili.upload_failed'), 9000);
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
    if (refresh) {
        refresh.addEventListener('click', () => {
            renderBili(biliSection.rid);
            loadBiliAccount();
            if (biliSection.tab === 'favorites') loadBiliFolders();
        });
    }
    const biliMore = document.getElementById('btn-bili-more');
    if (biliMore) biliMore.addEventListener('click', () => loadMoreBili());

    // The Bilibili download page.
    const biliDownloadBack = document.getElementById('btn-bili-download-back');
    if (biliDownloadBack) {
        biliDownloadBack.addEventListener('click', () => showView('bili'));
    }
    const biliDownloadRefresh = document.getElementById('btn-bili-download-refresh');
    if (biliDownloadRefresh) {
        biliDownloadRefresh.addEventListener('click', () => {
            renderBiliDownloadQueue();
        });
    }
    const biliDownloadStart = document.getElementById('btn-bili-download-start');
    if (biliDownloadStart) {
        biliDownloadStart.addEventListener('click', () => startBiliDownloads());
    }
    const biliDownloadAll = document.getElementById('btn-bili-download-select-all');
    if (biliDownloadAll) {
        biliDownloadAll.addEventListener('click', () => {
            biliDownloadQueue.items.forEach((item) => {
                biliDownloadQueue.selected.add(item.bvid);
            });
            renderBiliDownloadQueue();
        });
    }
    const biliDownloadNone = document.getElementById('btn-bili-download-select-none');
    if (biliDownloadNone) {
        biliDownloadNone.addEventListener('click', () => {
            biliDownloadQueue.selected.clear();
            renderBiliDownloadQueue();
        });
    }
    const biliDownloadClear = document.getElementById('btn-bili-download-clear');
    if (biliDownloadClear) {
        biliDownloadClear.addEventListener('click', () => {
            biliDownloadQueue.items = [];
            biliDownloadQueue.selected.clear();
            renderBiliDownloadQueue();
        });
    }
    const biliDownloadFolder = document.getElementById('btn-bili-download-open-folder');
    if (biliDownloadFolder) {
        biliDownloadFolder.addEventListener('click', () => {
            if (bridgeHas('open_bili_download_folder')) {
                state.api.open_bili_download_folder();
            }
        });
    }

    // The update panel.
    const updateCheck = document.getElementById('btn-check-update');
    if (updateCheck) updateCheck.addEventListener('click', () => checkAppUpdate());
    const updateDownload = document.getElementById('btn-download-update');
    if (updateDownload) updateDownload.addEventListener('click', () => downloadAppUpdate());
    const updateInstall = document.getElementById('btn-install-update');
    if (updateInstall) updateInstall.addEventListener('click', () => installAppUpdate());

    // The Bilibili settings tab.
    const biliSize = document.getElementById('bili-danmaku-size');
    if (biliSize) {
        biliSize.addEventListener('input', updateBiliSettingsLabels);
    }
    const biliOpacity = document.getElementById('bili-danmaku-opacity');
    if (biliOpacity) {
        biliOpacity.addEventListener('input', updateBiliSettingsLabels);
    }
    const biliSettingsLogin = document.getElementById('btn-bili-settings-login');
    if (biliSettingsLogin) {
        biliSettingsLogin.addEventListener('click', () => openBiliLoginModal());
    }
    const biliSettingsLogout = document.getElementById('btn-bili-settings-logout');
    if (biliSettingsLogout) {
        biliSettingsLogout.addEventListener('click', () => logoutBili());
    }

    // The Bilibili tabs: the hot list and the account's own favourites.
    const biliTabs = document.getElementById('bili-tabs');
    if (biliTabs) {
        biliTabs.addEventListener('click', (event) => {
            const tab = event.target.closest('.top-tab');
            if (!tab) return;
            setBiliTab(tab.dataset.tab);
        });
    }
    const favPrev = document.getElementById('btn-bili-fav-prev');
    if (favPrev) {
        favPrev.addEventListener('click', () => gotoBiliFavoritePage(biliFavorites.page - 1));
    }
    const favNext = document.getElementById('btn-bili-fav-next');
    if (favNext) {
        favNext.addEventListener('click', () => gotoBiliFavoritePage(biliFavorites.page + 1));
    }
    const favPageSize = document.getElementById('bili-fav-page-size');
    if (favPageSize) {
        favPageSize.addEventListener('change', () => setBiliFavPageSize(favPageSize.value));
    }
    // 订阅的合集一页只有 20 条，要能往下翻。
    const collectionsMore = document.getElementById('btn-bili-collection-more');
    if (collectionsMore) collectionsMore.addEventListener('click', loadMoreBiliCollections);
    const historyMore = document.getElementById('btn-bili-history-more');
    if (historyMore) historyMore.addEventListener('click', () => loadBiliHistory(false));

    // The detail panel: the three buttons, the comments and the danmaku.
    const biliLike = document.getElementById('btn-bili-like');
    if (biliLike) biliLike.addEventListener('click', likeBiliVideo);
    const biliCoin = document.getElementById('btn-bili-coin');
    if (biliCoin) biliCoin.addEventListener('click', openBiliCoinPanel);
    const biliFavorite = document.getElementById('btn-bili-favorite');
    if (biliFavorite) biliFavorite.addEventListener('click', openBiliFavoritePanel);
    const coinOne = document.getElementById('btn-bili-coin-one');
    if (coinOne) coinOne.addEventListener('click', () => coinBiliVideo(1));
    const coinTwo = document.getElementById('btn-bili-coin-two');
    if (coinTwo) coinTwo.addEventListener('click', () => coinBiliVideo(2));
    const favSave = document.getElementById('btn-bili-fav-save');
    if (favSave) favSave.addEventListener('click', saveBiliFavoriteSelection);
    // 收藏夹的多选：勾上几个视频之后才出现的这一条。
    const favBulkDownload = document.getElementById('btn-bili-fav-download');
    if (favBulkDownload) favBulkDownload.addEventListener('click', bulkDownloadBiliFavorites);
    const favBulkMove = document.getElementById('btn-bili-fav-move');
    if (favBulkMove) favBulkMove.addEventListener('click', () => openBiliFavTargetModal('move'));
    const favBulkCopy = document.getElementById('btn-bili-fav-copy');
    if (favBulkCopy) favBulkCopy.addEventListener('click', () => openBiliFavTargetModal('copy'));
    const favBulkRemove = document.getElementById('btn-bili-fav-batch-remove');
    if (favBulkRemove) favBulkRemove.addEventListener('click', bulkRemoveBiliFavorites);
    const favBulkClear = document.getElementById('btn-bili-fav-clear');
    if (favBulkClear) favBulkClear.addEventListener('click', clearBiliFavSelection);
    const collectionMore = document.getElementById('btn-bili-collection-videos-more');
    if (collectionMore) collectionMore.addEventListener('click', () => loadBiliCollectionVideos(false));
    const detailUp = document.getElementById('bili-detail-up');
    if (detailUp) detailUp.addEventListener('click', () => openBiliSpace(biliDetail.authorMid));
    const commentSend = document.getElementById('btn-bili-comment-send');
    if (commentSend) commentSend.addEventListener('click', sendBiliComment);
    const commentCancel = document.getElementById('btn-bili-comment-cancel');
    if (commentCancel) commentCancel.addEventListener('click', cancelBiliReply);
    const commentMore = document.getElementById('btn-bili-comment-more');
    if (commentMore) commentMore.addEventListener('click', () => loadBiliComments(false));
    const danmakuSend = document.getElementById('btn-bili-danmaku-send');
    if (danmakuSend) danmakuSend.addEventListener('click', sendBiliDanmaku);
    const danmakuKeyword = document.getElementById('bili-danmaku-keyword');
    if (danmakuKeyword) {
        danmakuKeyword.addEventListener('input', () => {
            biliDetail.danmakuKeyword = danmakuKeyword.value || '';
            resetBiliDanmakuCursor(biliVideoTime());
            renderBiliDanmaku();
        });
    }

    // The uploader's page, the private messages and the upload.
    const spaceFollow = document.getElementById('btn-bili-space-follow');
    if (spaceFollow) spaceFollow.addEventListener('click', toggleBiliFollow);
    const spaceMore = document.getElementById('btn-bili-space-more');
    if (spaceMore) spaceMore.addEventListener('click', () => loadBiliSpaceVideos(false));
    const imSend = document.getElementById('btn-bili-im-send');
    if (imSend) imSend.addEventListener('click', sendBiliMessage);
    const uploadVideo = document.getElementById('btn-bili-upload-video');
    if (uploadVideo) uploadVideo.addEventListener('click', pickBiliUploadVideo);
    const uploadCover = document.getElementById('btn-bili-upload-cover');
    if (uploadCover) uploadCover.addEventListener('click', pickBiliUploadCover);
    const uploadSubmit = document.getElementById('btn-bili-upload-submit');
    if (uploadSubmit) uploadSubmit.addEventListener('click', submitBiliUpload);

    // The danmaku follow the video: every tick puts the ones of that second
    // on the picture.
    const biliVideo = document.getElementById('bili-video');
    if (biliVideo) {
        biliVideo.addEventListener('timeupdate', () => {
            updateBiliDanmakuLive(biliVideo.currentTime);
        });
    }

    // The login panel: five ways to sign in.
    const loginTabs = document.getElementById('bili-login-tabs');
    if (loginTabs) {
        loginTabs.addEventListener('click', (event) => {
            const tab = event.target.closest('.tab');
            if (!tab) return;
            switchBiliLoginTab(tab.dataset.tab);
        });
    }
    const qrRefresh = document.getElementById('btn-bili-qrcode-refresh');
    if (qrRefresh) qrRefresh.addEventListener('click', () => refreshBiliQrcode());
    const passwordLogin = document.getElementById('btn-bili-password-login');
    if (passwordLogin) passwordLogin.addEventListener('click', () => submitBiliPassword());
    const smsSend = document.getElementById('btn-bili-sms-send');
    if (smsSend) smsSend.addEventListener('click', () => sendBiliSmsCode());
    const smsLogin = document.getElementById('btn-bili-sms-login');
    if (smsLogin) smsLogin.addEventListener('click', () => submitBiliSms());
    const smsRiskOpen = document.getElementById('btn-bili-sms-risk-open');
    if (smsRiskOpen) smsRiskOpen.addEventListener('click', openBiliSmsRiskPage);
    const smsRiskQr = document.getElementById('btn-bili-sms-risk-qr');
    if (smsRiskQr) smsRiskQr.addEventListener('click', switchToBiliQrLogin);
    // The key belongs to the number the code was sent to: a new number means
    // an old key, and the old key is void.
    const smsPhone = document.getElementById('bili-phone');
    if (smsPhone) {
        smsPhone.addEventListener('input', () => {
            if (biliInputValue('bili-phone') !== biliLogin.smsTel) {
                biliLogin.smsCaptchaKey = '';
                biliLogin.smsTel = '';
            }
        });
    }
    const cookieLogin = document.getElementById('btn-bili-cookie-login');
    if (cookieLogin) cookieLogin.addEventListener('click', () => submitBiliCookie());
    const registerSend = document.getElementById('btn-bili-register-send');
    if (registerSend) registerSend.addEventListener('click', () => sendBiliRegisterCode());
    const registerSubmit = document.getElementById('btn-bili-register-submit');
    if (registerSubmit) registerSubmit.addEventListener('click', () => submitBiliRegister());

    // The player: the quality picker and the two ways to leave it.
    const quality = document.getElementById('bili-quality');
    if (quality) quality.addEventListener('change', () => changeBiliQuality(quality.value));
    document.querySelectorAll('[data-close="account-center-modal"]').forEach((el) => {
        el.addEventListener('click', () => stopBiliLoginPolling());
    });
    document.querySelectorAll('[data-close="bili-player-modal"]').forEach((el) => {
        el.addEventListener('click', () => closeBiliPlayer());
    });
    // The capture phase runs before the generic Esc handler, so the playback
    // and the polling are stopped before the panel disappears.
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        const open = document.querySelector('.modal:not([hidden])');
        if (!open) return;
        if (open.id === 'bili-player-modal') closeBiliPlayer();
        if (open.id === 'account-center-modal') stopBiliLoginPolling();
    }, true);
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


/* ========================================================================
 * 收藏三件事：收藏夹 / 订阅的合集 / 追的番剧
 * ====================================================================== */

/** Which of the three the favourites page is showing. */
const biliFavSeg = {
    kind: 'folders',
    collections: [],
    // 订阅的合集要翻页：一页只有 20 条，这个账号有 65 个。
    collectionPage: 1,
    collectionHasMore: false,
    collectionTotal: 0,
    bangumi: [],
    loading: false,
    error: ''
};

/** Switch between the folders, the collections and the bangumi. */
function setBiliFavSegment(seg) {
    const known = ['folders', 'collections', 'bangumi'];
    biliFavSeg.kind = known.indexOf(seg) >= 0 ? seg : 'folders';
    document.querySelectorAll('#bili-fav-segments .chip').forEach((chip) =>
        chip.classList.toggle('is-active', chip.dataset.seg === biliFavSeg.kind));
    const boxes = {
        folders: 'bili-seg-folders',
        collections: 'bili-seg-collections',
        bangumi: 'bili-seg-bangumi'
    };
    Object.keys(boxes).forEach((key) => {
        const box = document.getElementById(boxes[key]);
        if (box) box.hidden = key !== biliFavSeg.kind;
    });
    if (biliFavSeg.kind === 'folders') loadBiliFolders();
    if (biliFavSeg.kind === 'collections') loadBiliCollections(false);
    if (biliFavSeg.kind === 'bangumi') loadBiliBangumi(false);
}

/** A plain tile: a cover, a title, a line under it and an optional badge. */
function biliTileElement(options) {
    const opts = options || {};
    const card = document.createElement('button');
    card.className = 'video-card is-tile';
    card.type = 'button';
    card.setAttribute('aria-label', opts.title || '');
    const thumb = document.createElement('div');
    thumb.className = 'vc-thumb';
    if (opts.cover) {
        const image = document.createElement('img');
        image.src = opts.cover;
        image.alt = '';
        image.loading = 'lazy';
        image.addEventListener('error', () => { image.remove(); });
        thumb.appendChild(image);
    }
    if (opts.badge) {
        const badge = document.createElement('span');
        badge.className = 'vc-dur';
        badge.textContent = opts.badge;
        thumb.appendChild(badge);
    }
    if (opts.badgePercent > 0) {
        const bar = document.createElement('span');
        bar.className = 'vc-progress';
        bar.style.width = Math.min(100, opts.badgePercent) + '%';
        thumb.appendChild(bar);
    }
    card.appendChild(thumb);
    const title = document.createElement('span');
    title.className = 'vc-title';
    title.textContent = opts.title || '';
    card.appendChild(title);
    const sub = document.createElement('span');
    sub.className = 'vc-sub';
    const up = document.createElement('span');
    up.className = 'vc-up';
    up.textContent = opts.sub || '';
    sub.appendChild(up);
    card.appendChild(sub);
    card.addEventListener('click', () => {
        if (typeof opts.onClick === 'function') opts.onClick();
    });
    return card;
}

/** The collections this account follows. */
async function loadBiliCollections(force) {
    const grid = document.getElementById('bili-collection-results');
    const stateEl = document.getElementById('bili-collection-state');
    if (!grid) return;
    if (biliFavSeg.collections.length && !force) {
        renderBiliCollections();
        return;
    }
    if (!bridgeHas('get_bili_subscribed_collections')) {
        if (stateEl) stateEl.textContent = t('bili.need_desktop');
        return;
    }
    biliFavSeg.loading = true;
    biliFavSeg.error = '';
    renderBiliCollections();
    try {
        const result = await state.api.get_bili_subscribed_collections();
        if (result && result.success) {
            biliFavSeg.collections = result.collections || [];
            biliFavSeg.collectionPage = 1;
            biliFavSeg.collectionHasMore = !!result.has_more;
            biliFavSeg.collectionTotal = Number(result.total) || 0;
        } else {
            biliFavSeg.collections = [];
            biliFavSeg.collectionHasMore = false;
            biliFavSeg.error = (result && result.message) || t('error.network');
        }
    } catch (err) {
        biliFavSeg.collections = [];
        biliFavSeg.collectionHasMore = false;
        biliFavSeg.error = t('error.network');
    }
    biliFavSeg.loading = false;
    renderBiliCollections();
}

/** Read the next page of the subscribed collections. */
async function loadMoreBiliCollections() {
    if (biliFavSeg.loading || !biliFavSeg.collectionHasMore) return;
    biliFavSeg.loading = true;
    renderBiliCollections();
    try {
        const page = biliFavSeg.collectionPage + 1;
        const result = await state.api.get_bili_subscribed_collections(page);
        if (result && result.success) {
            const items = result.collections || [];
            items.forEach((item) => biliFavSeg.collections.push(item));
            biliFavSeg.collectionPage = page;
            biliFavSeg.collectionHasMore = !!result.has_more;
        } else {
            biliFavSeg.collectionHasMore = false;
            biliFavSeg.error = (result && result.message) || t('error.network');
        }
    } catch (err) {
        biliFavSeg.collectionHasMore = false;
        biliFavSeg.error = t('error.network');
    }
    biliFavSeg.loading = false;
    renderBiliCollections();
}

function renderBiliCollections() {
    const grid = document.getElementById('bili-collection-results');
    const stateEl = document.getElementById('bili-collection-state');
    const more = document.getElementById('btn-bili-collection-more');
    if (!grid) return;
    grid.innerHTML = '';
    if (stateEl) stateEl.textContent = biliFavSeg.loading ? t('common.loading') : '';
    if (more) {
        more.hidden = !biliFavSeg.collectionHasMore || biliFavSeg.loading;
        more.disabled = biliFavSeg.loading;
    }
    if (!biliFavSeg.loading && biliFavSeg.error) {
        if (stateEl) stateEl.textContent = biliFavSeg.error;
        return;
    }
    if (!biliFavSeg.loading && !biliFavSeg.collections.length) {
        if (stateEl) stateEl.textContent = t('bili.no_collections');
        return;
    }
    biliFavSeg.collections.forEach((item) => {
        const count = Number(item.count) || 0;
        grid.appendChild(biliTileElement({
            cover: item.cover ? biliImageSrc(item.cover) : '',
            title: item.title || '',
            sub: item.author_name
                ? t('bili.collection_sub', { name: item.author_name, count: count })
                : t('bili.collection_count', { count: count }),
            badge: count ? t('bili.video_count', { count: count }) : '',
            // 点开就是这个合集里有哪些视频，比跳到 UP 主空间再自己找直接。
            onClick: () => openBiliCollection(item)
        }));
    });
}

/** The bangumi this account follows. */
async function loadBiliBangumi(force) {
    const grid = document.getElementById('bili-bangumi-results');
    const stateEl = document.getElementById('bili-bangumi-state');
    if (!grid) return;
    if (biliFavSeg.bangumi.length && !force) {
        renderBiliBangumi();
        return;
    }
    if (!bridgeHas('get_bili_bangumi')) {
        if (stateEl) stateEl.textContent = t('bili.need_desktop');
        return;
    }
    biliFavSeg.loading = true;
    biliFavSeg.error = '';
    renderBiliBangumi();
    try {
        const result = await state.api.get_bili_bangumi();
        if (result && result.success) {
            biliFavSeg.bangumi = result.shows || [];
        } else {
            biliFavSeg.bangumi = [];
            biliFavSeg.error = (result && result.message) || t('error.network');
        }
    } catch (err) {
        biliFavSeg.bangumi = [];
        biliFavSeg.error = t('error.network');
    }
    biliFavSeg.loading = false;
    renderBiliBangumi();
}

function renderBiliBangumi() {
    const grid = document.getElementById('bili-bangumi-results');
    const stateEl = document.getElementById('bili-bangumi-state');
    if (!grid) return;
    grid.innerHTML = '';
    if (stateEl) stateEl.textContent = biliFavSeg.loading ? t('common.loading') : '';
    if (!biliFavSeg.loading && biliFavSeg.error) {
        if (stateEl) stateEl.textContent = biliFavSeg.error;
        return;
    }
    if (!biliFavSeg.loading && !biliFavSeg.bangumi.length) {
        if (stateEl) stateEl.textContent = t('bili.no_bangumi');
        return;
    }
    biliFavSeg.bangumi.forEach((show) => {
        const total = Number(show.total) || 0;
        const sub = show.progress
            ? show.progress
            : (total ? t('bili.total_eps', { count: total }) : (show.ep_index || ''));
        grid.appendChild(biliTileElement({
            cover: show.cover ? biliImageSrc(show.cover) : '',
            title: show.title || '',
            sub: sub,
            badgePercent: Number(show.progress_percent) || 0,
            onClick: () => openBiliBangumiEpisodes(show)
        }));
    });
}

/**
 * Play one bangumi episode.
 *
 * A bangumi has no bvid, so the separate window (which is addressed by
 * bvid) cannot show it; this one always plays in the main window.  The
 * episode handed over is the newest one the follow list knows about --
 * B站 does not say which episode the user was on.
 */
async function playBangumiShow(show) {
    const seasonId = Number(show && show.season_id) || 0;
    const episodeId = Number(show && show.ep_id) || 0;
    if (!seasonId) {
        showToast('error', t('bili.video_error'));
        return;
    }
    if (!bridgeHas('get_bili_bangumi_streams')) {
        showToast('error', t('bili.need_desktop'));
        return;
    }
    biliPlayer.track = { name: (show && show.title) || '', bvid: '', cover: (show && show.cover) || '' };
    // 番剧也照设置里的默认画质来；PGC 那边 quality=0 表示「能看的最高」。
    const quality = defaultBiliQuality();
    biliPlayer.quality = quality;
    openBiliPlayer(biliPlayer.track);
    showBiliOverlay('loading');
    let result = null;
    try {
        result = await state.api.get_bili_bangumi_streams(seasonId, episodeId, quality);
    } catch (err) {
        result = null;
    }
    if (!result || !result.success) {
        setBiliPlayerStatus('error');
        showBiliOverlay('error', 'bili.overlay_no_stream');
        // B站 answers "大会员专享限制" itself, and that is worth showing as is.
        showToast('error', (result && result.message) || t('bili.video_error'));
        return;
    }
    biliPlayer.streams = result;
    biliPlayer.title = result.title || (show && show.title) || '';
    renderBiliQualities(result);
    const videoEl = document.getElementById('bili-video');
    const played = await window.BiliStreamLoader.play(videoEl, result, {
        onStatus: (code) => setBiliPlayerStatus(code)
    });
    if (played) {
        hideBiliOverlay();
    } else {
        setBiliPlayerStatus('error');
        showBiliOverlay('error', 'bili.overlay_no_stream');
    }
}

/* ========================================================================
 * 别人的动态
 * ====================================================================== */

const biliSpaceSeg = { kind: 'videos', dynamics: [], loading: false, error: '' };

function setBiliSpaceSegment(seg) {
    biliSpaceSeg.kind = seg === 'dynamics' ? 'dynamics' : 'videos';
    document.querySelectorAll('#bili-space-segments .chip').forEach((chip) =>
        chip.classList.toggle('is-active', chip.dataset.seg === biliSpaceSeg.kind));
    const videos = document.getElementById('bili-space-seg-videos');
    const dynamics = document.getElementById('bili-space-seg-dynamics');
    if (videos) videos.hidden = biliSpaceSeg.kind !== 'videos';
    if (dynamics) dynamics.hidden = biliSpaceSeg.kind !== 'dynamics';
    if (biliSpaceSeg.kind === 'dynamics') loadBiliDynamics();
}

async function loadBiliDynamics(force) {
    const grid = document.getElementById('bili-space-dynamics');
    const stateEl = document.getElementById('bili-dynamics-state');
    if (!grid) return;
    const mid = biliSpace.mid || '';
    if (!mid) return;
    if (biliSpaceSeg.dynamics.length && !force) {
        renderBiliDynamics();
        return;
    }
    if (!bridgeHas('get_bili_dynamics')) {
        if (stateEl) stateEl.textContent = t('bili.need_desktop');
        return;
    }
    biliSpaceSeg.loading = true;
    biliSpaceSeg.error = '';
    renderBiliDynamics();
    try {
        const result = await state.api.get_bili_dynamics(mid, 1);
        if (result && result.success) {
            biliSpaceSeg.dynamics = result.items || [];
        } else {
            biliSpaceSeg.dynamics = [];
            biliSpaceSeg.error = (result && result.message) || t('error.network');
        }
    } catch (err) {
        biliSpaceSeg.dynamics = [];
        biliSpaceSeg.error = t('error.network');
    }
    biliSpaceSeg.loading = false;
    renderBiliDynamics();
}

function renderBiliDynamics() {
    const grid = document.getElementById('bili-space-dynamics');
    const stateEl = document.getElementById('bili-dynamics-state');
    if (!grid) return;
    grid.innerHTML = '';
    if (stateEl) stateEl.textContent = biliSpaceSeg.loading ? t('common.loading') : '';
    if (!biliSpaceSeg.loading && biliSpaceSeg.error) {
        if (stateEl) stateEl.textContent = biliSpaceSeg.error;
        return;
    }
    if (!biliSpaceSeg.loading && !biliSpaceSeg.dynamics.length) {
        if (stateEl) stateEl.textContent = t('bili.no_dynamics');
        return;
    }
    biliSpaceSeg.dynamics.forEach((item) => {
        const row = document.createElement('div');
        row.className = 'bili-dynamic is-' + (item.type || 'other');
        const head = document.createElement('div');
        head.className = 'bd-head';
        if (item.author_face) {
            const face = document.createElement('img');
            face.className = 'bd-face';
            face.src = biliImageSrc(item.author_face);
            face.alt = '';
            face.loading = 'lazy';
            face.addEventListener('error', () => { face.remove(); });
            head.appendChild(face);
        }
        const who = document.createElement('span');
        who.className = 'bd-name';
        who.textContent = item.author_name || '';
        head.appendChild(who);
        const when = document.createElement('span');
        when.className = 'bd-time';
        when.textContent = item.pub_time_text || '';
        head.appendChild(when);
        row.appendChild(head);

        if (item.text) {
            const text = document.createElement('div');
            text.className = 'bd-text';
            text.textContent = item.text;
            row.appendChild(text);
        }
        // A video post is a card you can play; a picture post is a strip.
        if (item.bvid) {
            const thumb = document.createElement('button');
            thumb.className = 'bd-video';
            thumb.type = 'button';
            if (item.cover) {
                const image = document.createElement('img');
                image.src = biliImageSrc(item.cover);
                image.alt = '';
                image.loading = 'lazy';
                image.addEventListener('error', () => { image.remove(); });
                thumb.appendChild(image);
            }
            const name = document.createElement('span');
            name.className = 'bd-video-title';
            name.textContent = item.title || '';
            thumb.appendChild(name);
            thumb.addEventListener('click', () => {
                playBiliTrack({
                    bvid: item.bvid,
                    name: item.title || '',
                    cover_url: item.cover || '',
                    duration: 0
                });
            });
            row.appendChild(thumb);
        }
        if (Array.isArray(item.images) && item.images.length) {
            const strip = document.createElement('div');
            strip.className = 'bd-images';
            item.images.slice(0, 9).forEach((src) => {
                const image = document.createElement('img');
                image.src = biliImageSrc(src);
                image.alt = '';
                image.loading = 'lazy';
                image.addEventListener('error', () => { image.remove(); });
                strip.appendChild(image);
            });
            row.appendChild(strip);
        }
        grid.appendChild(row);
    });
}

/* ========================================================================
 * 本地视频（实验性）
 * ====================================================================== */

const localVideo = {
    items: [],
    dirs: [],
    errors: [],
    loading: false,
    error: '',
    folder: '',
    keyword: ''
};

async function loadLocalVideoLibrary(force) {
    const grid = document.getElementById('local-video-results');
    const stateEl = document.getElementById('local-video-state');
    if (!grid) return;
    if (!bridgeHas('get_local_video_library')) {
        if (stateEl) stateEl.textContent = t('bili.need_desktop');
        return;
    }
    if (localVideo.items.length && !force) {
        renderLocalVideos();
        return;
    }
    localVideo.loading = true;
    localVideo.error = '';
    renderLocalVideos();
    try {
        const result = await state.api.get_local_video_library();
        if (result && result.success) {
            localVideo.items = result.items || [];
            localVideo.dirs = result.dirs || [];
            localVideo.errors = result.errors || [];
        } else {
            localVideo.items = [];
            localVideo.dirs = (result && result.dirs) || [];
            localVideo.error = (result && result.message) || t('error.network');
        }
    } catch (err) {
        localVideo.items = [];
        localVideo.error = t('error.network');
    }
    localVideo.loading = false;
    renderLocalVideos();
    renderLocalVideoFolders();
}

function renderLocalVideoFolders() {
    const row = document.getElementById('local-video-folders');
    if (!row) return;
    row.innerHTML = '';
    const all = document.createElement('button');
    all.className = 'chip' + (localVideo.folder ? '' : ' is-active');
    all.type = 'button';
    all.textContent = t('local_video.all_folders');
    all.addEventListener('click', () => {
        localVideo.folder = '';
        renderLocalVideoFolders();
        renderLocalVideos();
    });
    row.appendChild(all);
    localVideo.dirs.forEach((dir) => {
        const name = String(dir).split('/').filter(Boolean).pop() || String(dir);
        const chip = document.createElement('button');
        chip.className = 'chip' + (localVideo.folder === dir ? ' is-active' : '');
        chip.type = 'button';
        chip.textContent = name;
        chip.title = dir;
        chip.addEventListener('click', () => {
            localVideo.folder = localVideo.folder === dir ? '' : dir;
            renderLocalVideoFolders();
            renderLocalVideos();
        });
        row.appendChild(chip);
    });
}

function renderLocalVideos() {
    const grid = document.getElementById('local-video-results');
    const stateEl = document.getElementById('local-video-state');
    if (!grid) return;
    grid.innerHTML = '';
    if (stateEl) stateEl.textContent = localVideo.loading ? t('common.loading') : '';
    if (!localVideo.loading && localVideo.error) {
        if (stateEl) stateEl.textContent = localVideo.error;
        return;
    }
    if (!localVideo.loading && !localVideo.dirs.length) {
        if (stateEl) stateEl.textContent = t('local_video.no_folder');
        return;
    }
    const keyword = localVideo.keyword.trim().toLowerCase();
    const rows = localVideo.items.filter((item) => {
        const path = String(item.path || '');
        if (localVideo.folder && path.indexOf(localVideo.folder) !== 0) return false;
        if (keyword && String(item.name || '').toLowerCase().indexOf(keyword) < 0) return false;
        return true;
    });
    if (!localVideo.loading && !rows.length) {
        if (stateEl) stateEl.textContent = t('local_video.no_video');
        return;
    }
    rows.forEach((item) => {
        const seconds = Number(item.duration) || 0;
        grid.appendChild(biliTileElement({
            cover: '',
            title: item.name || '',
            sub: seconds > 0
                ? fmtTime(Math.round(seconds))
                : t('local_video.size', { size: formatBytes(Number(item.size) || 0) }),
            onClick: () => playLocalVideoFile(item)
        }));
    });
    const end = document.getElementById('local-video-end');
    if (end) {
        end.hidden = !rows.length;
        end.textContent = t('local_video.count', { count: rows.length });
    }
}

async function playLocalVideoFile(item) {
    if (!item || !item.path) return;
    if (!bridgeHas('open_local_video')) {
        showToast('error', t('bili.need_desktop'));
        return;
    }
    try {
        const result = await state.api.open_local_video(item.path, '');
        if (!result || !result.success) {
            showToast('error', (result && result.message) || t('local_video.open_failed'));
        }
    } catch (err) {
        showToast('error', t('local_video.open_failed'));
    }
}

async function addLocalVideoFolder() {
    if (!bridgeHas('add_local_video_folder')) {
        showToast('error', t('bili.need_desktop'));
        return;
    }
    try {
        const result = await state.api.add_local_video_folder();
        if (result && result.success && !result.cancelled) {
            localVideo.dirs = result.dirs || [];
            localVideo.items = [];
            renderLocalVideoFolders();
            await loadLocalVideoLibrary(true);
        }
    } catch (err) {
        showToast('error', t('error.unknown'));
    }
}

/* ------------------------------------------------------------------ wiring */

function bindNewBiliFeatures() {
    const segments = document.getElementById('bili-fav-segments');
    if (segments) {
        segments.addEventListener('click', (event) => {
            const chip = event.target.closest('.chip');
            if (!chip) return;
            setBiliFavSegment(chip.dataset.seg);
        });
    }
    const spaceSegments = document.getElementById('bili-space-segments');
    if (spaceSegments) {
        spaceSegments.addEventListener('click', (event) => {
            const chip = event.target.closest('.chip');
            if (!chip) return;
            setBiliSpaceSegment(chip.dataset.seg);
        });
    }
    // The favourites page: the folder manager and the relations list.
    const folderManage = document.getElementById('btn-bili-folder-manage');
    if (folderManage) folderManage.addEventListener('click', openBiliFolderManage);
    const folderCreate = document.getElementById('btn-bili-folder-create');
    if (folderCreate) folderCreate.addEventListener('click', createBiliFolder);
    const relationsSegments = document.getElementById('bili-relations-segments');
    if (relationsSegments) {
        relationsSegments.addEventListener('click', (event) => {
            const chip = event.target.closest('.chip');
            if (!chip) return;
            biliRelations.seg = chip.dataset.seg || 'followings';
            biliRelations.page = 1;
            biliRelations.items = [];
            biliRelations.hasMore = false;
            relationsSegments.querySelectorAll('.chip').forEach((item) => {
                item.classList.toggle('is-active', item === chip);
            });
            loadBiliRelations(true);
        });
    }
    const relationsMore = document.getElementById('btn-bili-relations-more');
    if (relationsMore) relationsMore.addEventListener('click', () => loadBiliRelations(false));
    const addButton = document.getElementById('btn-local-video-add');
    if (addButton) addButton.addEventListener('click', addLocalVideoFolder);
    const refreshButton = document.getElementById('btn-local-video-refresh');
    if (refreshButton) refreshButton.addEventListener('click', () => loadLocalVideoLibrary(true));
    const backButton = document.getElementById('btn-local-video-back');
    if (backButton) backButton.addEventListener('click', () => showView('home'));
    const search = document.getElementById('local-video-search');
    if (search) {
        search.addEventListener('input', () => {
            localVideo.keyword = search.value || '';
            renderLocalVideos();
        });
    }
}


/* ================================================================ Bangumi
 *
 * 番组计划的资料库。跟 B站那一半不一样：这里**没有账号**，收藏和观看
 * 历史只存在本机（bridge 写到 bangumi.json），不跟任何服务器同步。
 *
 * 三块：搜索、收藏、观看历史。点一个条目开详情，里面有选集和资源链接。
 */

const bangumiState = {
    tab: 'search',
    keyword: '',
    results: [],
    loading: false,
    error: '',
    favorites: [],
    history: [],
    subject: null,
    episodes: [],
    resources: [],
    detailLoading: false
};

/** 封面走本地代理：图床在 lain.bangumi.lat，网页不能直接连。 */
function bangumiImageSrc(url) {
    const raw = String(url || '').trim();
    if (!raw) return '';
    if (!raw.startsWith('http://') && !raw.startsWith('https://')) return raw;
    const base = state.mediaBaseUrl;
    if (!base) return '';
    return base + '/bgm?u=' + encodeURIComponent(raw);
}

/** 一个番剧条目渲染成卡片。 */
function bangumiTileElement(item, options) {
    const opts = options || {};
    const card = document.createElement('button');
    card.className = 'video-card is-tile';
    card.type = 'button';
    const title = item.name_cn || item.name || '';
    card.setAttribute('aria-label', title);
    const thumb = document.createElement('div');
    thumb.className = 'vc-thumb';
    const cover = item.cover || (item.images && item.images.common);
    if (cover) {
        const image = document.createElement('img');
        image.src = bangumiImageSrc(cover);
        image.alt = '';
        image.loading = 'lazy';
        image.addEventListener('error', () => { image.remove(); });
        thumb.appendChild(image);
    }
    if (opts.badge) {
        const badge = document.createElement('span');
        badge.className = 'vc-dur';
        badge.textContent = opts.badge;
        thumb.appendChild(badge);
    }
    card.appendChild(thumb);
    const label = document.createElement('span');
    label.className = 'vc-title';
    label.textContent = title;
    card.appendChild(label);
    if (opts.sub) {
        const sub = document.createElement('span');
        sub.className = 'vc-sub';
        sub.textContent = opts.sub;
        card.appendChild(sub);
    }
    card.addEventListener('click', () => opts.onClick && opts.onClick(item));
    return card;
}

/** 切标签页。 */
function setBangumiTab(tab) {
    const known = ['search', 'favorites', 'history', 'source'];
    bangumiState.tab = known.indexOf(tab) >= 0 ? tab : 'search';
    document.querySelectorAll('#bangumi-tabs .top-tab').forEach((button) =>
        button.classList.toggle('is-active', button.dataset.bangumiTab === bangumiState.tab));
    known.forEach((key) => {
        const box = document.getElementById('bangumi-seg-' + key);
        if (box) box.hidden = key !== bangumiState.tab;
    });
    if (bangumiState.tab === 'favorites') loadBangumiFavorites(false);
    if (bangumiState.tab === 'history') loadBangumiHistory(false);
    if (bangumiState.tab === 'source') loadBangumiSource();
}

/** 搜番剧。 */
async function searchBangumi(force) {
    const keyword = String(bangumiState.keyword || '').trim();
    const grid = document.getElementById('bangumi-results');
    const stateEl = document.getElementById('bangumi-state');
    if (!grid) return;
    if (!keyword) {
        bangumiState.results = [];
        renderBangumiResults();
        return;
    }
    if (bangumiState.results.length && !force) {
        renderBangumiResults();
        return;
    }
    bangumiState.loading = true;
    bangumiState.error = '';
    renderBangumiResults();
    try {
        const result = await state.api.search_bangumi(keyword, 24, 2);
        if (result && result.success) {
            bangumiState.results = result.items || [];
        } else {
            bangumiState.results = [];
            bangumiState.error = (result && result.error_key)
                ? t(result.error_key) : t('bangumi.search_failed');
        }
    } catch (err) {
        bangumiState.results = [];
        bangumiState.error = t('bangumi.error.network');
    }
    bangumiState.loading = false;
    renderBangumiResults();
}

function renderBangumiResults() {
    const grid = document.getElementById('bangumi-results');
    const stateEl = document.getElementById('bangumi-state');
    if (!grid) return;
    grid.innerHTML = '';
    if (stateEl) {
        stateEl.textContent = bangumiState.loading ? t('common.loading')
            : (bangumiState.error || '');
    }
    if (!bangumiState.loading && bangumiState.error) return;
    if (!bangumiState.loading && !bangumiState.results.length) {
        if (stateEl) stateEl.textContent = t('bangumi.search_empty');
        return;
    }
    bangumiState.results.forEach((item) => {
        const count = Number(item.total_episodes) || 0;
        const score = item.rating_score;
        grid.appendChild(bangumiTileElement(item, {
            badge: count ? t('bangumi.detail_total_episodes', { count: count }) : '',
            sub: score ? t('bangumi.detail_rating') + ' ' + score : '',
            onClick: (entry) => openBangumiSubject(entry.id)
        }));
    });
}

/** 本地收藏。 */
async function loadBangumiFavorites(force) {
    const grid = document.getElementById('bangumi-favorites');
    const stateEl = document.getElementById('bangumi-favorites-state');
    if (!grid) return;
    if (bangumiState.favorites.length && !force) {
        renderBangumiFavorites();
        return;
    }
    try {
        const result = await state.api.get_bangumi_favorites();
        bangumiState.favorites = (result && result.items) || [];
    } catch (err) {
        bangumiState.favorites = [];
    }
    renderBangumiFavorites();
}

function renderBangumiFavorites() {
    const grid = document.getElementById('bangumi-favorites');
    const stateEl = document.getElementById('bangumi-favorites-state');
    if (!grid) return;
    grid.innerHTML = '';
    if (!bangumiState.favorites.length) {
        if (stateEl) stateEl.textContent = t('bangumi.fav_empty');
        return;
    }
    if (stateEl) stateEl.textContent = '';
    bangumiState.favorites.forEach((item) => {
        grid.appendChild(bangumiTileElement(item, {
            onClick: (entry) => openBangumiSubject(entry.subject_id)
        }));
    });
}

/** 本地观看历史。 */
async function loadBangumiHistory(force) {
    const grid = document.getElementById('bangumi-history');
    const stateEl = document.getElementById('bangumi-history-state');
    const clear = document.getElementById('btn-bangumi-clear-history');
    if (!grid) return;
    if (bangumiState.history.length && !force) {
        renderBangumiHistory();
        return;
    }
    try {
        const result = await state.api.get_bangumi_history();
        bangumiState.history = (result && result.items) || [];
    } catch (err) {
        bangumiState.history = [];
    }
    renderBangumiHistory();
}

function renderBangumiHistory() {
    const grid = document.getElementById('bangumi-history');
    const stateEl = document.getElementById('bangumi-history-state');
    const clear = document.getElementById('btn-bangumi-clear-history');
    if (!grid) return;
    grid.innerHTML = '';
    if (clear) clear.hidden = !bangumiState.history.length;
    if (!bangumiState.history.length) {
        if (stateEl) stateEl.textContent = t('bangumi.history_empty');
        return;
    }
    if (stateEl) stateEl.textContent = '';
    bangumiState.history.forEach((item) => {
        const count = Number(item.ep) || 0;
        grid.appendChild(bangumiTileElement(item, {
            badge: count ? t('bangumi.history_watched', { ep: count }) : '',
            sub: item.name_cn || item.name || '',
            onClick: (entry) => openBangumiSubject(entry.subject_id)
        }));
    });
}

/** 开一个条目的详情：资料 + 选集 + 资源链接。 */
async function openBangumiSubject(subjectId) {
    const detail = document.getElementById('bangumi-detail');
    if (!detail) return;
    detail.hidden = false;
    document.getElementById('bangumi-seg-search').hidden = true;
    document.getElementById('bangumi-seg-favorites').hidden = true;
    document.getElementById('bangumi-seg-history').hidden = true;
    const sourceSeg = document.getElementById('bangumi-seg-source');
    if (sourceSeg) sourceSeg.hidden = true;
    bangumiState.detailLoading = true;
    bangumiState.subject = null;
    bangumiState.episodes = [];
    bangumiState.resources = [];
    renderBangumiDetail();
    try {
        const result = await state.api.get_bangumi_subject(subjectId);
        if (result && result.success) {
            bangumiState.subject = result.subject;
        }
        const eps = await state.api.get_bangumi_episodes(subjectId);
        if (eps && eps.success) bangumiState.episodes = eps.episodes || [];
        const res = await state.api.get_bangumi_resource_links(subjectId);
        if (res && res.success) bangumiState.resources = res.links || [];
    } catch (err) {
        /* 详情拿不到就显示空的，不该把整个界面弄崩 */
    }
    bangumiState.detailLoading = false;
    renderBangumiDetail();
}

/** 关掉详情，回到列表。 */
function closeBangumiSubject() {
    const detail = document.getElementById('bangumi-detail');
    if (detail) detail.hidden = true;
    setBangumiTab(bangumiState.tab);
}

function renderBangumiDetail() {
    const subject = bangumiState.subject;
    const titleEl = document.getElementById('bangumi-detail-title');
    const coverEl = document.getElementById('bangumi-detail-cover');
    const metaEl = document.getElementById('bangumi-detail-meta');
    const summaryEl = document.getElementById('bangumi-detail-summary');
    const favButton = document.getElementById('btn-bangumi-favorite');
    const episodesEl = document.getElementById('bangumi-episodes');
    const episodesState = document.getElementById('bangumi-episodes-state');
    const resourcesEl = document.getElementById('bangumi-resources');
    const resourcesState = document.getElementById('bangumi-resources-state');
    if (!titleEl) return;

    if (!subject) {
        titleEl.textContent = bangumiState.detailLoading ? t('common.loading') : '';
        if (episodesEl) episodesEl.innerHTML = '';
        if (episodesState) episodesState.textContent = '';
        if (resourcesEl) resourcesEl.innerHTML = '';
        if (resourcesState) resourcesState.textContent = '';
        return;
    }

    titleEl.textContent = subject.name_cn || subject.name || '';
    const images = subject.images || {};
    const cover = images.large || images.common || images.medium || '';
    if (coverEl) {
        if (cover) {
            coverEl.src = bangumiImageSrc(cover);
            coverEl.hidden = false;
        } else {
            coverEl.hidden = true;
        }
    }

    const meta = [];
    const count = Number(subject.total_episodes) || 0;
    if (count) meta.push(t('bangumi.detail_total_episodes', { count: count }));
    if (subject.rating_score) meta.push(t('bangumi.detail_rating') + ' ' + subject.rating_score);
    const collected = (subject.collection && subject.collection.collect) || 0;
    if (collected) meta.push(t('bangumi.detail_collected', { count: collected }));
    if (subject.platform) meta.push(subject.platform);
    if (subject.date) meta.push(subject.date);
    if (metaEl) metaEl.textContent = meta.join(' · ');
    if (summaryEl) summaryEl.textContent = subject.summary || '';

    if (favButton) {
        favButton.textContent = subject.favorite
            ? t('bangumi.fav_remove') : t('bangumi.fav_add');
        favButton.classList.toggle('is-primary', !!subject.favorite);
    }

    if (episodesEl) {
        episodesEl.innerHTML = '';
        bangumiState.episodes.forEach((ep) => {
            const row = document.createElement('div');
            row.className = 'bangumi-episode';
            const name = document.createElement('span');
            name.className = 'bangumi-episode-name';
            name.textContent = ep.label || ep.name_cn || ep.name || '';
            row.appendChild(name);
            if (ep.type_name && ep.type !== 0) {
                const tag = document.createElement('span');
                tag.className = 'bangumi-episode-type';
                tag.textContent = ep.type_name;
                row.appendChild(tag);
            }
            if (ep.duration) {
                const dur = document.createElement('span');
                dur.className = 'bangumi-episode-duration';
                dur.textContent = ep.duration;
                row.appendChild(dur);
            }
            if (ep.airdate) {
                const date = document.createElement('span');
                date.className = 'bangumi-episode-date';
                date.textContent = ep.airdate;
                row.appendChild(date);
            }
            const action = document.createElement('button');
            action.className = 'btn btn-sm btn-secondary';
            action.type = 'button';
            action.textContent = t('bangumi.resource_links');
            action.addEventListener('click', () => {
                showBangumiEpisodeLinks(ep);
            });
            row.appendChild(action);
            episodesEl.appendChild(row);
        });
    }
    if (episodesState) {
        episodesState.textContent = bangumiState.detailLoading ? t('bangumi.episodes_loading')
            : (bangumiState.episodes.length ? '' : t('bangumi.episodes_empty'));
    }

    renderBangumiResources();
    renderBangumiQueue();
}

function renderBangumiResources() {
    const list = document.getElementById('bangumi-resources');
    const stateEl = document.getElementById('bangumi-resources-state');
    if (!list) return;
    list.innerHTML = '';
    if (!bangumiState.resources.length) {
        if (stateEl) stateEl.textContent = t('bangumi.resource_empty');
        return;
    }
    if (stateEl) stateEl.textContent = '';
    bangumiState.resources.forEach((link) => {
        const row = document.createElement('div');
        row.className = 'bangumi-resource';
        const name = document.createElement('span');
        name.className = 'bangumi-resource-name';
        name.textContent = link.name || link.id || '';
        row.appendChild(name);
        const url = link.search_url || link.detail_url;
        if (url) {
            const anchor = document.createElement('a');
            anchor.className = 'bangumi-resource-link';
            anchor.href = url;
            anchor.textContent = url;
            // The webview does not open a second window, so a plain link with
            // target="_blank" does nothing at all.  The click goes to the
            // system browser instead.
            anchor.addEventListener('click', (event) => {
                if (event && event.preventDefault) event.preventDefault();
                openBangumiLink(url);
            });
            row.appendChild(anchor);

            // 直链才值得下载和播放；给的是网页的话下回来是一页 HTML。
            if (isBangumiDirectLink(url)) {
                const play = document.createElement('button');
                play.className = 'btn btn-sm btn-secondary';
                play.type = 'button';
                play.textContent = t('bangumi.play');
                play.addEventListener('click', () => playBangumiUrl(url, link.name || link.id || ''));
                row.appendChild(play);

                const save = document.createElement('button');
                save.className = 'btn btn-sm btn-secondary';
                save.type = 'button';
                save.textContent = t('bangumi.download');
                save.addEventListener('click', () => downloadBangumiUrl(url, save));
                row.appendChild(save);
            }
        }
        list.appendChild(row);
    });
}

/** 用系统浏览器打开一个链接。 */
function openBangumiLink(url) {
    if (!url) return;
    if (state.api && typeof state.api.open_external === 'function') {
        state.api.open_external(url);
        return;
    }
    window.open(url, '_blank');
}

/** 在页面里直接播一个直链。

 * 资源站大多只把文件发给带着自己 Referer 的请求，网页里的 video 元素
 * 没法设 Referer，所以走本地媒体代理转发。
 */
function playBangumiUrl(url, label) {
    if (!url) return;
    const box = document.getElementById('bangumi-player');
    const video = document.getElementById('bangumi-player-video');
    const name = document.getElementById('bangumi-player-name');
    if (!box || !video) return;
    const base = state.mediaBaseUrl;
    if (!base) {
        showToast('error', t('bangumi.play_failed'));
        return;
    }
    video.src = base + '/bgm-media?u=' + encodeURIComponent(url);
    if (name) name.textContent = label || url;
    box.hidden = false;
    try {
        const started = video.play();
        if (started && typeof started.catch === 'function') {
            started.catch(() => {
                // 自动播放被拦了：控件还在，用户点一下就能播。
                showToast('error', t('bangumi.play_failed'));
            });
        }
    } catch (err) {
        showToast('error', t('bangumi.play_failed'));
    }
}

/** 关掉内联播放器。 */
function closeBangumiPlayer() {
    const box = document.getElementById('bangumi-player');
    const video = document.getElementById('bangumi-player-video');
    if (video) {
        try { video.pause(); } catch (err) { /* 忽略 */ }
        video.removeAttribute('src');
        video.load();
    }
    if (box) box.hidden = true;
}

/** 某一集的下载链接。 */
async function showBangumiEpisodeLinks(episode) {
    const subject = bangumiState.subject;
    if (!subject) return;
    try {
        const result = await state.api.get_bangumi_resource_links(
            subject.id, episode.id);
        if (result && result.success) {
            bangumiState.resources = result.links || [];
            renderBangumiResources();
        }
    } catch (err) {
        /* 拿不到就留着原来的 */
    }
}

/** 把一条资源链接存成文件。 */
async function downloadBangumiUrl(url, button) {
    if (!url) return;
    const label = button ? button.textContent : '';
    if (button) {
        button.disabled = true;
        button.textContent = t('common.loading');
    }
    try {
        const result = await state.api.download_bangumi_url(url);
        if (result && result.success) {
            showToast('success', t('bangumi.download_done'));
        } else {
            showToast('error', t('bangumi.download_failed'));
        }
    } catch (err) {
        showToast('error', t('bangumi.download_failed'));
    }
    if (button) {
        button.disabled = false;
        button.textContent = label;
    }
}

/** 下载队列的状态。 */
const bangumiQueueState = {
    tasks: [],
    running: false,
    timer: null
};

/** 直链才值得下载；网页链接下回来是一页 HTML。 */
function isBangumiDirectLink(url) {
    return /\.(mp4|mkv|avi|webm|flv|m4v|mp3|flac|m4a|zip|7z|rar|txt|ass|srt)(\?|$)/i
        .test(String(url || ''));
}

/** 把当前资源的直链全放进队列。 */
async function downloadAllBangumiResources() {
    const links = (bangumiState.resources || []).filter((link) => {
        const url = link.search_url || link.detail_url;
        return url && isBangumiDirectLink(url);
    });
    if (!links.length) {
        showToast('error', t('bangumi.queue_no_links'));
        return;
    }
    const subject = bangumiState.subject;
    const items = links.map((link) => {
        const url = link.search_url || link.detail_url;
        let filename = '';
        if (subject) {
            const name = subject.name_cn || subject.name || '';
            filename = name + ' - ' + (link.label || link.name || '');
        }
        return {url: url, filename: filename, label: link.name || link.id || url};
    });
    try {
        const result = await state.api.enqueue_bangumi_downloads(items);
        if (result && result.success) {
            showToast('success', t('bangumi.queue_added').replace('{n}', result.added));
            await refreshBangumiQueue();
        } else {
            showToast('error', t('bangumi.download_failed'));
        }
    } catch (err) {
        showToast('error', t('bangumi.download_failed'));
    }
}

/** 读一次队列状态并画出来。 */
async function refreshBangumiQueue() {
    if (!bridgeHas('get_bangumi_download_queue')) return;
    try {
        const result = await state.api.get_bangumi_download_queue();
        if (!result || !result.success) return;
        bangumiQueueState.tasks = result.tasks || [];
        renderBangumiQueue();
        // 还有活着的就继续轮询。
        const busy = result.pending > 0 || result.running > 0;
        if (busy && !bangumiQueueState.timer) {
            bangumiQueueState.timer = setInterval(refreshBangumiQueue, 1000);
        } else if (!busy && bangumiQueueState.timer) {
            clearInterval(bangumiQueueState.timer);
            bangumiQueueState.timer = null;
        }
    } catch (err) {
        /* 读不到就留着原来的 */
    }
}

/** 队列里的每一行。 */
function renderBangumiQueue() {
    const list = document.getElementById('bangumi-queue-list');
    if (!list) return;
    list.innerHTML = '';
    const tasks = bangumiQueueState.tasks || [];
    if (!tasks.length) {
        const empty = document.createElement('div');
        empty.className = 'bangumi-queue-empty';
        empty.textContent = t('bangumi.queue_empty');
        list.appendChild(empty);
        return;
    }
    tasks.forEach((task) => {
        const row = document.createElement('div');
        row.className = 'bangumi-queue-row';
        const label = document.createElement('span');
        label.className = 'bangumi-queue-label';
        label.textContent = task.label || task.url || '';
        row.appendChild(label);
        const status = document.createElement('span');
        status.className = 'bangumi-queue-status is-' + (task.status || 'pending');
        status.textContent = t('bangumi.queue_status_' + (task.status || 'pending'));
        row.appendChild(status);
        list.appendChild(row);
    });
}

/** 停掉整个队列。 */
async function cancelBangumiQueue() {
    if (!bridgeHas('cancel_bangumi_download_queue')) return;
    try {
        await state.api.cancel_bangumi_download_queue();
        showToast('success', t('bangumi.queue_cancelled'));
        await refreshBangumiQueue();
    } catch (err) {
        showToast('error', t('bangumi.download_failed'));
    }
}

/** 清掉已经结束的任务。 */
async function clearBangumiQueue() {
    if (!bridgeHas('clear_bangumi_download_queue')) return;
    try {
        await state.api.clear_bangumi_download_queue();
        await refreshBangumiQueue();
    } catch (err) {
        /* 忽略 */
    }
}

/** 把条目资料和章节表导出成 txt。 */
async function exportBangumiSubject() {
    const subject = bangumiState.subject;
    if (!subject) return;
    try {
        const result = await state.api.export_bangumi_subject(subject.id);
        if (result && result.success) {
            showToast('success', t('bangumi.export_done'));
        } else {
            showToast('error', t('bangumi.download_failed'));
        }
    } catch (err) {
        showToast('error', t('bangumi.download_failed'));
    }
}

/** 收藏 / 取消收藏。 */
async function toggleBangumiFavorite() {
    const subject = bangumiState.subject;
    if (!subject) return;
    try {
        const result = await state.api.toggle_bangumi_favorite(subject.id);
        if (result && result.success) {
            subject.favorite = !!result.favorite;
            renderBangumiDetail();
            // 收藏列表变了，回去的时候要重新读。
            bangumiState.favorites = [];
        }
    } catch (err) {
        /* 忽略 */
    }
}

/** 清空观看历史。 */
async function clearBangumiHistory() {
    try {
        await state.api.clear_bangumi_history();
        bangumiState.history = [];
        renderBangumiHistory();
    } catch (err) {
        /* 忽略 */
    }
}


/* ------------------------------------------- Bangumi: the settings page */

/** The mirrors and the resource sites, as the user arranged them. */
const bangumiSettingsState = {
    mirrors: [],
    sites: []
};

/** Put the saved Bangumi settings into the form. */
function fillBangumiSettings(cfg) {
    const data = cfg || {};
    bangumiSettingsState.mirrors = Array.isArray(data.mirrors)
        ? data.mirrors.map((item) => Object.assign({}, item)) : [];
    bangumiSettingsState.sites = Array.isArray(data.resource_sites)
        ? data.resource_sites.map((item) => Object.assign({}, item)) : [];
    const host = document.getElementById('bangumi-api-host');
    if (host) host.value = data.api_host || '';
    const web = document.getElementById('bangumi-web-host');
    if (web) web.value = data.web_host || '';
    const interval = document.getElementById('bangumi-request-interval');
    if (interval) interval.value = String(data.request_interval || 1);
    const skip = document.getElementById('bangumi-skip-ssl');
    if (skip) skip.checked = !!data.skip_ssl_verify;
    renderBangumiMirrors();
    renderBangumiSites();
}

/** One row per mirror, with the buttons that move it up and down. */
function renderBangumiMirrors() {
    const list = document.getElementById('bangumi-mirror-list');
    if (!list) return;
    list.innerHTML = '';
    bangumiSettingsState.mirrors.forEach((mirror, index) => {
        const row = document.createElement('div');
        row.className = 'bangumi-setting-row';
        const name = document.createElement('span');
        name.className = 'bangumi-setting-name';
        name.textContent = mirror.name || mirror.id || '';
        row.appendChild(name);
        const host = document.createElement('span');
        host.className = 'bangumi-setting-host';
        host.textContent = (mirror.api_hosts || []).join(', ');
        row.appendChild(host);

        const up = document.createElement('button');
        up.className = 'btn btn-sm btn-ghost';
        up.type = 'button';
        up.textContent = '↑';
        up.title = t('bangumi.mirrors_up');
        up.addEventListener('click', () => moveBangumiMirror(index, -1));
        row.appendChild(up);

        const down = document.createElement('button');
        down.className = 'btn btn-sm btn-ghost';
        down.type = 'button';
        down.textContent = '↓';
        down.title = t('bangumi.mirrors_down');
        down.addEventListener('click', () => moveBangumiMirror(index, 1));
        row.appendChild(down);

        const remove = document.createElement('button');
        remove.className = 'btn btn-sm btn-ghost';
        remove.type = 'button';
        remove.textContent = '✕';
        remove.title = t('bangumi.mirrors_remove');
        remove.addEventListener('click', () => {
            bangumiSettingsState.mirrors.splice(index, 1);
            renderBangumiMirrors();
        });
        row.appendChild(remove);
        list.appendChild(row);
    });
}

function moveBangumiMirror(index, step) {
    const target = index + step;
    if (target < 0 || target >= bangumiSettingsState.mirrors.length) return;
    const item = bangumiSettingsState.mirrors.splice(index, 1)[0];
    bangumiSettingsState.mirrors.splice(target, 0, item);
    renderBangumiMirrors();
}

/** One row per resource site. */
function renderBangumiSites() {
    const list = document.getElementById('bangumi-site-list');
    if (!list) return;
    list.innerHTML = '';
    bangumiSettingsState.sites.forEach((site, index) => {
        const row = document.createElement('div');
        row.className = 'bangumi-setting-row';
        const name = document.createElement('span');
        name.className = 'bangumi-setting-name';
        name.textContent = site.name || site.id || '';
        row.appendChild(name);
        const url = document.createElement('span');
        url.className = 'bangumi-setting-host';
        url.textContent = site.detail_url || site.search_url || '';
        row.appendChild(url);

        const remove = document.createElement('button');
        remove.className = 'btn btn-sm btn-ghost';
        remove.type = 'button';
        remove.textContent = '✕';
        remove.title = t('bangumi.mirrors_remove');
        remove.addEventListener('click', () => {
            bangumiSettingsState.sites.splice(index, 1);
            renderBangumiSites();
        });
        row.appendChild(remove);
        list.appendChild(row);
    });
}

async function chooseBangumiDownloadDir() {
    try {
        const result = await state.api.choose_directory();
        if (result && result.success && result.path) {
            const input = document.getElementById('bangumi-download-dir');
            if (input) input.value = result.path;
        }
    } catch (err) {
        /* 忽略：没有桌面对话框的环境里会失败 */
    }
}

/** Add a mirror the user typed in. */
function addBangumiMirror() {
    const idInput = document.getElementById('bangumi-mirror-id');
    const hostInput = document.getElementById('bangumi-mirror-host');
    const id = idInput ? idInput.value.trim() : '';
    const host = hostInput ? hostInput.value.trim() : '';
    if (!host) return;
    const mirrorId = id || host;
    if (bangumiSettingsState.mirrors.some((item) => item.id === mirrorId)) return;
    bangumiSettingsState.mirrors.push({
        id: mirrorId,
        name: mirrorId,
        api_hosts: [host],
        main_hosts: [],
        recommended: false,
    });
    if (idInput) idInput.value = '';
    if (hostInput) hostInput.value = '';
    renderBangumiMirrors();
}

/** Add a resource site the user typed in. */
function addBangumiSite() {
    const nameInput = document.getElementById('bangumi-site-name');
    const urlInput = document.getElementById('bangumi-site-url');
    const name = nameInput ? nameInput.value.trim() : '';
    const url = urlInput ? urlInput.value.trim() : '';
    if (!url) return;
    bangumiSettingsState.sites.push({
        id: name || url,
        name: name || url,
        kind: 'custom',
        enabled: true,
        detail_url: url,
        search_url: '',
        note: '',
    });
    if (nameInput) nameInput.value = '';
    if (urlInput) urlInput.value = '';
    renderBangumiSites();
}

/** Pull the official mirror list. */
async function refreshBangumiMirrors() {
    try {
        const result = await state.api.get_bangumi_mirrors(true);
        const mirrors = (result && result.mirrors) || [];
        if (!mirrors.length) return;
        // The official list replaces the built-in ones but keeps the user's
        // own additions at the end.
        const own = bangumiSettingsState.mirrors.filter((item) => !item.recommended);
        bangumiSettingsState.mirrors = mirrors.concat(own);
        renderBangumiMirrors();
    } catch (err) {
        /* 拉不到就留着现在的 */
    }
}

/** Back to the mirrors that ship with the app. */
async function resetBangumiMirrors() {
    try {
        const result = await state.api.get_bangumi_built_in_mirrors();
        const mirrors = (result && result.mirrors) || [];
        if (mirrors.length) {
            bangumiSettingsState.mirrors = mirrors;
            renderBangumiMirrors();
        }
    } catch (err) {
        /* 忽略 */
    }
}

function bindBangumiSettings() {
    const add = document.getElementById('btn-bangumi-mirror-add');
    if (add) add.addEventListener('click', addBangumiMirror);
    const refresh = document.getElementById('btn-bangumi-mirror-refresh');
    if (refresh) refresh.addEventListener('click', refreshBangumiMirrors);
    const reset = document.getElementById('btn-bangumi-mirror-reset');
    if (reset) reset.addEventListener('click', resetBangumiMirrors);
    const addSite = document.getElementById('btn-bangumi-site-add');
    if (addSite) addSite.addEventListener('click', addBangumiSite);
    const chooseDir = document.getElementById('btn-bangumi-choose-dir');
    if (chooseDir) chooseDir.addEventListener('click', () => chooseBangumiDownloadDir());
}

/** 绑定 Bangumi 的按钮。 */
function bindBangumiFeatures() {
    const tabs = document.getElementById('bangumi-tabs');
    if (tabs) {
        tabs.addEventListener('click', (event) => {
            const button = event.target.closest('.top-tab');
            if (!button) return;
            setBangumiTab(button.dataset.bangumiTab);
        });
    }
    const searchButton = document.getElementById('btn-bangumi-search');
    if (searchButton) {
        searchButton.addEventListener('click', () => {
            const input = document.getElementById('bangumi-search-input');
            bangumiState.keyword = input ? input.value : '';
            searchBangumi(true);
        });
    }
    const input = document.getElementById('bangumi-search-input');
    if (input) {
        input.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter') return;
            bangumiState.keyword = input.value || '';
            searchBangumi(true);
        });
    }
    const refresh = document.getElementById('btn-bangumi-refresh');
    if (refresh) {
        refresh.addEventListener('click', () => {
            if (bangumiState.tab === 'favorites') loadBangumiFavorites(true);
            else if (bangumiState.tab === 'history') loadBangumiHistory(true);
            else searchBangumi(true);
        });
    }
    const back = document.getElementById('btn-bangumi-back');
    if (back) back.addEventListener('click', () => showView('home'));
    const detailBack = document.getElementById('btn-bangumi-detail-back');
    if (detailBack) detailBack.addEventListener('click', closeBangumiSubject);
    const favButton = document.getElementById('btn-bangumi-favorite');
    if (favButton) favButton.addEventListener('click', toggleBangumiFavorite);
    const exportButton = document.getElementById('btn-bangumi-export');
    if (exportButton) exportButton.addEventListener('click', exportBangumiSubject);
    const queueAll = document.getElementById('btn-bangumi-queue-download-all');
    if (queueAll) queueAll.addEventListener('click', downloadAllBangumiResources);
    const queueCancel = document.getElementById('btn-bangumi-queue-cancel');
    if (queueCancel) queueCancel.addEventListener('click', cancelBangumiQueue);
    const queueClear = document.getElementById('btn-bangumi-queue-clear');
    if (queueClear) queueClear.addEventListener('click', clearBangumiQueue);
    const playerClose = document.getElementById('btn-bangumi-player-close');
    if (playerClose) playerClose.addEventListener('click', closeBangumiPlayer);
    const clearButton = document.getElementById('btn-bangumi-clear-history');
    if (clearButton) clearButton.addEventListener('click', clearBangumiHistory);
    bindBangumiSettings();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bindNewBiliFeatures);
    document.addEventListener('DOMContentLoaded', bindBangumiFeatures);
} else {
    bindNewBiliFeatures();
    bindBangumiFeatures();
}


/* -------------------------------------------- 播放源（Kazumi 规则）

规则负责「搜到哪部番、它有哪些集」，播放地址是从剧集页里找出来的。
规则本身不含视频地址（Kazumi 是靠 webview 拦请求拿的），所以有些站点
找得到集数、找不到流 —— 那时如实说，不假装能播。
*/

const bangumiSource = {
    settings: null,
    rules: [],
    active: '',
    hits: [],
    chapters: [],
    wired: false,
    message: ''
};

function bgmSrc(id) {
    return document.getElementById(id);
}

function bgmSay(text) {
    const box = bgmSrc('bgm-source-msg');
    if (box) box.textContent = text || '';
    bangumiSource.message = text || '';
}

function wireBangumiSource() {
    if (bangumiSource.wired) return;
    bangumiSource.wired = true;

    const refresh = bgmSrc('btn-bgm-source-refresh');
    if (refresh) refresh.addEventListener('click', () => loadBangumiRules(true));
    const find = bgmSrc('btn-bgm-source-search');
    if (find) find.addEventListener('click', () => searchBangumiSource());

    const toggles = [
        ['bgm-source-enabled', 'source_enabled'],
        ['bgm-source-auto-mirror', 'auto_mirror'],
        ['bgm-source-skip-ssl', 'skip_ssl_verify']
    ];
    toggles.forEach(([id, key]) => {
        const box = bgmSrc(id);
        if (box) box.addEventListener('change', () => {
            saveBangumiSourceSettings({ [key]: !!box.checked });
        });
    });

    const repo = bgmSrc('bgm-source-repo');
    if (repo) repo.addEventListener('change', () => {
        const value = String(repo.value || '').trim();
        if (!value) return;
        saveBangumiSourceSettings({ rule_repos: [value] });
    });
}

function renderBangumiSourceSettings() {
    const settings = bangumiSource.settings || {};
    const repo = bgmSrc('bgm-source-repo');
    if (repo) repo.value = (settings.repos && settings.repos[0]) || '';
    const pairs = [
        ['bgm-source-enabled', 'enabled'],
        ['bgm-source-auto-mirror', 'auto_mirror'],
        ['bgm-source-skip-ssl', 'skip_ssl_verify']
    ];
    pairs.forEach(([id, key]) => {
        const box = bgmSrc(id);
        if (box) box.checked = !!settings[key];
    });
}

async function loadBangumiSource() {
    wireBangumiSource();
    if (!bridgeHas('get_bangumi_source_settings')) {
        bgmSay(t('error.unknown'));
        return;
    }
    try {
        const result = await state.api.get_bangumi_source_settings();
        if (result && result.success) {
            bangumiSource.settings = result;
            renderBangumiSourceSettings();
        }
    } catch (err) {
        bgmSay(t('error.network'));
        return;
    }
    if (!bangumiSource.rules.length) loadBangumiRules(false);
}

async function saveBangumiSourceSettings(values) {
    try {
        const result = await state.api.save_bangumi_source_settings(values);
        if (result && result.success) {
            bangumiSource.settings = result;
            renderBangumiSourceSettings();
            // 换仓库或开自动镜像之后规则列表要重新拉
            if ('rule_repos' in values || 'auto_mirror' in values) loadBangumiRules(true);
        } else if (result) {
            bgmSay(t(result.error_key || 'error.unknown'));
        }
    } catch (err) {
        bgmSay(t('error.network'));
    }
}

async function loadBangumiRules(refresh) {
    wireBangumiSource();
    const status = bgmSrc('bgm-source-status');
    if (status) status.textContent = t('common.loading');
    try {
        const result = await state.api.get_bangumi_rules('', !!refresh);
        if (!result || !result.success) {
            if (status) status.textContent = t((result && result.error_key) || 'error.network');
            return;
        }
        bangumiSource.rules = result.items || [];
        const settings = bangumiSource.settings || {};
        bangumiSource.active = settings.active_rule || bangumiSource.active;
        if (status) {
            status.textContent = t('bangumi.source_rule_count', {
                count: result.count,
                source: result.source || ''
            });
        }
        renderBangumiRules();
    } catch (err) {
        if (status) status.textContent = t('error.network');
    }
}

function renderBangumiRules() {
    const box = bgmSrc('bgm-source-rules');
    if (!box) return;
    box.innerHTML = '';
    if (!bangumiSource.rules.length) {
        const empty = document.createElement('div');
        empty.className = 'page-hint';
        empty.textContent = t('bangumi.source_no_rules');
        box.appendChild(empty);
        return;
    }
    bangumiSource.rules.forEach((rule) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'chip' + (rule.name === bangumiSource.active ? ' is-active' : '');
        chip.textContent = rule.deprecated
            ? t('bangumi.source_rule_deprecated', { name: rule.name })
            : rule.name;
        chip.addEventListener('click', () => {
            bangumiSource.active = rule.name;
            renderBangumiRules();
            saveBangumiSourceSettings({ active_rule: rule.name });
        });
        box.appendChild(chip);
    });
}

async function searchBangumiSource() {
    const input = bgmSrc('bgm-source-keyword');
    const keyword = String((input && input.value) || '').trim();
    if (!keyword) {
        bgmSay(t('bangumi.rule_keyword_empty'));
        return;
    }
    if (!bangumiSource.active) {
        bgmSay(t('bangumi.source_pick_rule_first'));
        return;
    }
    bgmSay(t('common.loading'));
    try {
        const result = await state.api.search_bangumi_by_rule(keyword, bangumiSource.active, '');
        if (!result || !result.success) {
            bgmSay(t((result && result.error_key) || 'error.network'));
            return;
        }
        bangumiSource.hits = result.items || [];
        bangumiSource.chapters = [];
        bgmSay(bangumiSource.hits.length ? '' : t('bangumi.source_no_hits'));
        renderBangumiSourceHits();
        renderBangumiSourceChapters();
    } catch (err) {
        bgmSay(t('error.network'));
    }
}

function renderBangumiSourceHits() {
    const box = bgmSrc('bgm-source-hits');
    if (!box) return;
    box.innerHTML = '';
    bangumiSource.hits.forEach((hit) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'bgm-rule-row';
        row.textContent = hit.name || hit.url;
        row.title = hit.url;
        row.addEventListener('click', () => openBangumiSourceHit(hit));
        box.appendChild(row);
    });
}

async function openBangumiSourceHit(hit) {
    bgmSay(t('common.loading'));
    try {
        const result = await state.api.get_bangumi_rule_chapters(
            hit.url, bangumiSource.active, '');
        if (!result || !result.success) {
            bgmSay(t((result && result.error_key) || 'error.network'));
            return;
        }
        bangumiSource.chapters = result.items || [];
        bgmSay(bangumiSource.chapters.length ? '' : t('bangumi.source_no_chapters'));
        renderBangumiSourceChapters();
    } catch (err) {
        bgmSay(t('error.network'));
    }
}

function renderBangumiSourceChapters() {
    const box = bgmSrc('bgm-source-chapters');
    if (!box) return;
    box.innerHTML = '';
    bangumiSource.chapters.forEach((chapter) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'chip';
        row.textContent = chapter.title || chapter.url;
        row.title = chapter.group ? `${chapter.group} · ${chapter.url}` : chapter.url;
        row.addEventListener('click', () => playBangumiSourceChapter(chapter, row));
        box.appendChild(row);
    });
}

async function playBangumiSourceChapter(chapter, row) {
    if (row) row.classList.add('is-active');
    bgmSay(t('bangumi.source_looking_stream'));
    try {
        const result = await state.api.get_bangumi_rule_streams(
            chapter.url, bangumiSource.active, '');
        if (!result || !result.success) {
            bgmSay(t((result && result.error_key) || 'error.network'));
            return;
        }
        const streams = result.items || [];
        if (!streams.length) {
            // 这是最常见的结果：地址在页面的 JS 里拼出来，静态拿不到。
            bgmSay(t('bangumi.source_no_stream'));
            return;
        }
        const player = bgmSrc('bgm-source-player');
        const video = bgmSrc('bgm-source-video');
        const now = bgmSrc('bgm-source-now');
        if (!player || !video) return;
        player.hidden = false;
        video.src = streams[0].play_url || streams[0].url;
        const play = video.play();
        if (play && play.catch) play.catch(() => { /* 自动播放被拒是正常的 */ });
        if (now) {
            now.textContent = streams.length > 1
                ? t('bangumi.source_stream_count', { count: streams.length })
                : '';
        }
        bgmSay('');
    } catch (err) {
        bgmSay(t('error.network'));
    }
}
