/* ==========================================================================
   The phone interface.

   Same Python behind it as the desktop (the same bridge, the same media
   server), different front end: one column, a bottom tab bar, a mini
   player that grows into a full screen one, and every target big enough
   for a thumb.

   The bridge is reached through window.pywebview.api, exactly like the
   desktop does.
   ========================================================================== */

const TOAST_MS = 4800;
const TOAST_UNDO_MS = 8000;
const TOAST_UNDONE_MS = 2400;

const state = {
    api: null,
    tab: 'home',
    queue: [],
    index: -1,
    playing: false,
    detail: null,
    libraryTab: 'mine',
    searchType: 'songs',
    login: null,
    coverCache: new Map(),
    coverPending: new Set(),
    translations: {},
    downloadStats: {},
    /* The lyrics of the current track and the line that is playing. */
    lyrics: [],
    lyricIndex: -1,
    lyricsTrack: null,
};

/* ------------------------------------------------------------------ helpers */
const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
}

function showSpinner(host) {
    clear(host).appendChild(el('div', 'spinner'));
}

function showEmpty(host, title, hint) {
    const box = el('div', 'empty');
    if (title) box.appendChild(el('strong', null, title));
    if (hint) box.appendChild(el('span', null, hint));
    clear(host).appendChild(box);
}

/* ------------------------------------------------------------------- i18n */
/**
 * The translation for a key, e.g. "login.code_send_failed".
 *
 * The bridge reports what went wrong as an ``error_key``; this is what
 * turns that into a sentence the user can read.  A key that is not in
 * the catalogue comes back unchanged, so a missing translation shows the
 * key rather than an empty bubble.
 */
function t(key, params) {
    let text = state.translations[key];
    if (text === undefined || text === null) text = key;
    if (params) {
        text = text.replace(/\{(\w+)\}/g, (match, name) =>
            (params[name] !== undefined && params[name] !== null) ? params[name] : match);
    }
    return text;
}

/**
 * The text for a failed call.
 *
 * The bridge reports failures with an ``error_key`` and, for some calls,
 * a ``message``.  The key is the one that is always there, so it comes
 * first; the message is the raw text from the server and only reads
 * well when it is not a translation key itself.
 */
function errorText(result, fallback) {
    if (!result) return fallback;
    if (result.error_key) {
        const text = t(result.error_key, { message: result.message || '' });
        if (text !== result.error_key) return text;
    }
    if (result.message && result.message !== result.error_key) return result.message;
    return fallback;
}

async function api(name, ...args) {
    if (!state.api) return { success: false, error_key: 'bridge.unavailable' };
    try {
        return await state.api[name](...args);
    } catch (err) {
        /* The bridge call itself failed -- it timed out, or the WebView
           never handed the result back.  ``error_key`` is what the pages
           look at to tell "there is nothing here" from "this could not be
           read", so it has to be set even on the failure path.  Without
           it the page renders an empty list and claims the playlist has
           no songs, which is a lie. */
        return {
            success: false,
            error_key: 'bridge.call_failed',
            message: String((err && err.message) || err),
        };
    }
}

function formatTime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function trackKey(track) {
    if (!track) return '';
    if (track.key) return String(track.key);
    if (track.source === 'local') return `local:${track.path || ''}`;
    return `online:${track.id || ''}`;
}

/* ------------------------------------------------------------------ toasts */
function showToast(message, options) {
    if (!message) return;
    const host = $('toast-host');
    if (!host) return;
    const undo = options && options.undoLabel && typeof options.onUndo === 'function'
        ? options : null;
    const toast = el('div', 'toast');
    toast.appendChild(el('span', 'toast-text', message));
    let timer = null;
    const dismiss = () => {
        if (timer) { clearTimeout(timer); timer = null; }
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 200);
    };
    if (undo) {
        const button = el('button', 'toast-undo', undo.undoLabel);
        button.type = 'button';
        button.addEventListener('click', () => {
            if (button.disabled) return;
            button.disabled = true;
            dismiss();
            try { Promise.resolve(undo.onUndo()).catch(() => {}); } catch (err) { /* handled there */ }
        });
        toast.appendChild(button);
    }
    host.appendChild(toast);
    timer = setTimeout(dismiss, undo ? Math.max(TOAST_MS, TOAST_UNDO_MS) : TOAST_MS);
}

/* -------------------------------------------------------------- sheet/modal */
function openSheet(title, body) {
    $('sheet-title').textContent = title || '';
    const host = $('sheet-body');
    clear(host);
    if (body) host.appendChild(body);
    $('sheet-host').hidden = false;
}

function closeSheet() { $('sheet-host').hidden = true; }

function openModal({ title, body, actions }) {
    $('modal-title').textContent = title || '';
    const host = $('modal-body');
    clear(host);
    if (body) host.appendChild(body);
    const buttons = $('modal-actions');
    clear(buttons);
    (actions || []).forEach((action) => {
        const button = el('button', `btn ${action.class || 'btn-ghost'}`, action.label);
        button.type = 'button';
        button.addEventListener('click', () => {
            if (action.keepOpen !== true) $('modal-host').hidden = true;
            if (action.onClick) action.onClick();
        });
        buttons.appendChild(button);
    });
    $('modal-host').hidden = false;
}

function closeModal() { $('modal-host').hidden = true; }

/* ------------------------------------------------------------------ router */
const TITLES = { home: '首页', search: '搜索', library: '歌单', downloads: '下载', me: '我的' };

function go(tab) {
    if (!TITLES[tab]) return;
    state.tab = tab;
    document.querySelectorAll('#tabbar .tab').forEach((button) => {
        button.classList.toggle('is-on', button.dataset.tab === tab);
    });
    document.querySelectorAll('#views > .view').forEach((view) => {
        view.classList.toggle('is-active', view.id === `view-${tab}`);
    });
    $('appbar-title').textContent = TITLES[tab];
    $('btn-back').hidden = true;
    // Each page scrolls inside itself (the pages are absolutely positioned
    // in the content area), so the scroll position to reset is the one of
    // the page being shown, not of the container.
    const active = document.getElementById(`view-${tab}`);
    if (active) active.scrollTop = 0;
    if (tab === 'home') loadHome();
    if (tab === 'search') loadSuggest();
    if (tab === 'library') loadLibrary();
    if (tab === 'downloads') loadDownloads();
    if (tab === 'me') loadMe();
}

/** The detail page and the player are pushed on top; the hardware back
 *  button has to close them, so every push leaves a history entry. */
function pushView(name) {
    try { history.pushState({ view: name }, ''); } catch (err) { /* no history */ }
}

function popView() {
    try { history.back(); } catch (err) { /* no history */ }
}

window.addEventListener('popstate', () => {
    if ($('view-player').classList.contains('is-active')) {
        closePlayer();
        return;
    }
    if ($('view-detail').classList.contains('is-active')) {
        closeDetail();
        return;
    }
});

/* -------------------------------------------------------------- cover art */
async function coverFor(track) {
    const raw = track && track.cover_url ? String(track.cover_url) : '';
    if (!raw) return '';
    if (state.coverCache.has(raw)) return state.coverCache.get(raw);
    if (state.coverPending.has(raw)) return '';
    state.coverPending.add(raw);
    const result = await api('resolve_cover_url', { cover_url: raw });
    state.coverPending.delete(raw);
    const url = result && result.success && result.url ? result.url : '';
    state.coverCache.set(raw, url);
    return url;
}

/** Paint a cover into a box; the url arrives a moment later. */
function paintCover(box, track) {
    if (!box) return;
    box.classList.toggle('is-empty', !(track && track.cover_url));
    if (!track || !track.cover_url) { box.style.backgroundImage = ''; return; }
    coverFor(track).then((url) => {
        if (url) box.style.backgroundImage = `url("${url}")`;
    });
}

/* ------------------------------------------------------------- track rows */
function trackRow(track, index, list) {
    const row = el('div', 'track');
    row.setAttribute('role', 'button');
    row.setAttribute('tabindex', '0');

    const cover = el('div', 'track-cover');
    paintCover(cover, track);
    row.appendChild(cover);

    const main = el('div', 'track-main');
    main.appendChild(el('div', 'track-name', track.name || '未知歌曲'));
    const sub = el('div', 'track-sub', track.artists || track.album || '');
    main.appendChild(sub);
    row.appendChild(main);

    const actions = el('div', 'track-actions');
    const more = el('button', 'iconbtn');
    more.type = 'button';
    more.setAttribute('aria-label', '更多');
    more.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="1.6"/>'
        + '<circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>';
    more.addEventListener('click', (event) => {
        event.stopPropagation();
        openTrackMenu(track, list, index);
    });
    actions.appendChild(more);
    row.appendChild(actions);

    row.addEventListener('click', () => playTracks(list, index));
    return row;
}

function renderTracks(host, tracks, emptyTitle) {
    clear(host);
    if (!tracks || !tracks.length) {
        showEmpty(host, emptyTitle || '这里还没有歌');
        return;
    }
    tracks.forEach((track, index) => host.appendChild(trackRow(track, index, tracks)));
}

function cardGrid(host, items, onOpen, subtitle) {
    clear(host);
    if (!items || !items.length) {
        showEmpty(host, '暂时没有内容');
        return;
    }
    items.forEach((item) => {
        const card = el('div', 'card');
        const cover = el('div', 'card-cover');
        paintCover(cover, item);
        card.appendChild(cover);
        card.appendChild(el('div', 'card-name', item.name || ''));
        if (subtitle) {
            const sub = el('div', 'card-sub', subtitle(item));
            card.appendChild(sub);
        }
        card.addEventListener('click', () => onOpen(item));
        host.appendChild(card);
    });
}

/* ------------------------------------------------------------------ player */
const audio = $('audio');

function currentTrack() {
    return state.index >= 0 ? state.queue[state.index] : null;
}

async function playTracks(tracks, index) {
    if (!tracks || !tracks.length) return;
    state.queue = tracks.slice();
    state.index = index;
    await loadCurrent();
}

async function loadCurrent(autoPlay = true) {
    const track = currentTrack();
    if (!track) return;
    const resolved = await api('resolve_track', track);
    if (!resolved || !resolved.success || !resolved.url) {
        showToast('这首歌暂时放不了');
        updatePlayerUi();
        return;
    }
    audio.src = resolved.url;
    if (autoPlay) {
        try {
            await audio.play();
        } catch (err) {
            /* the browser wants a gesture first; the button still works */
        }
    }
    state.playing = !audio.paused;
    updatePlayerUi();
    /* A new song means new lyrics. */
    if (state.lyricsTrack !== trackKey(track)) loadLyrics(track);
}

function advance(step) {
    if (!state.queue.length) return;
    let next = state.index + step;
    if (next < 0) next = state.queue.length - 1;
    if (next >= state.queue.length) next = 0;
    state.index = next;
    loadCurrent();
}

function togglePlay() {
    if (!currentTrack()) {
        showToast('还没有放歌');
        return;
    }
    if (audio.paused) {
        if (!audio.src) { loadCurrent(); return; }
        audio.play().catch(() => {});
    } else {
        audio.pause();
    }
    state.playing = !audio.paused;
    updatePlayerUi();
}

audio.addEventListener('play', () => { state.playing = true; updatePlayerUi(); });
audio.addEventListener('pause', () => { state.playing = false; updatePlayerUi(); });
audio.addEventListener('ended', () => advance(1));
audio.addEventListener('timeupdate', () => {
    if (!audio.duration) return;
    const ratio = Math.max(0, Math.min(1, audio.currentTime / audio.duration));
    $('player-bar-fill').style.width = `${ratio * 100}%`;
    $('miniplayer-progress-fill').style.width = `${ratio * 100}%`;
    $('player-time-now').textContent = formatTime(audio.currentTime);
    $('player-time-end').textContent = formatTime(audio.duration);
    updateLyricHighlight();
});

function updatePlayerUi() {
    const track = currentTrack();
    $('miniplayer').hidden = !track;
    if (!track) return;
    $('miniplayer-name').textContent = track.name || '';
    $('miniplayer-artist').textContent = track.artists || '';
    $('player-name').textContent = track.name || '';
    $('player-artist').textContent = track.artists || track.album || '';
    paintCover($('miniplayer-cover'), track);
    paintCover($('player-cover'), track);
    document.querySelectorAll('.icon-play, .icon-pause').forEach((node) => {
        if (node.classList.contains('icon-play')) node.hidden = state.playing;
        else node.hidden = !state.playing;
    });
}

function openPlayer() {
    if (!currentTrack()) return;
    $('view-player').classList.add('is-active');
    pushView('player');
}

/* ---------------------------------------------------------------- lyrics */
/**
 * The lyrics of the current track, and which line is playing.
 *
 * The phone has no desktop lyrics window, so the lyrics live in the player
 * itself: the big cover shows them, and tapping it goes back to the cover.
 * The lines arrive from the bridge with a timestamp each, the same shape
 * the desktop uses, so the two front ends read the same data.
 */
/** Load the lyrics of a track (or clear them when it has none). */
async function loadLyrics(track) {
    state.lyrics = [];
    state.lyricIndex = -1;
    state.lyricsTrack = track ? trackKey(track) : null;
    const box = $('lyrics-box');
    if (!box) return;
    clear(box);
    if (!track) return;
    box.appendChild(el('div', 'small text-3', '正在取歌词…'));
    const result = await api('get_track_lyrics', track, false);
    clear(box);
    if (!result || !result.has_lyrics || !(result.lines || []).length) {
        state.lyrics = [];
        box.appendChild(el('div', 'small text-3', '这首歌没有歌词'));
        renderLyrics();
        return;
    }
    state.lyrics = result.lines || [];
    renderLyrics();
}

/** Draw the lyric lines and mark the one that is playing. */
function renderLyrics() {
    const box = $('lyrics-box');
    if (!box) return;
    clear(box);
    if (!state.lyrics.length) return;
    state.lyrics.forEach((line, index) => {
        const element = el('div', 'lyric-line', line.text || '');
        element.dataset.time = String(line.time);
        element.dataset.index = String(index);
        if (line.translation) {
            const translation = el('span', 'lyric-translation', line.translation);
            element.appendChild(translation);
        }
        box.appendChild(element);
    });
    updateLyricHighlight();
}

/** Mark the line that belongs to the current playback position. */
function updateLyricHighlight() {
    const box = $('lyrics-box');
    if (!box || !state.lyrics.length) return;
    const ms = (audio.currentTime || 0) * 1000;
    let index = -1;
    for (let i = 0; i < state.lyrics.length; i += 1) {
        if ((state.lyrics[i].time || 0) <= ms) index = i;
        else break;
    }
    if (index === state.lyricIndex) return;
    state.lyricIndex = index;
    box.querySelectorAll('.lyric-line').forEach((node) => {
        const at = Number(node.dataset.index);
        node.classList.toggle('is-active', at === index);
    });
    if (index >= 0) {
        const line = box.querySelector(`.lyric-line[data-index="${index}"]`);
        if (line && line.scrollIntoView) {
            line.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
    }
}

/** Show the lyrics instead of the cover, or the cover instead of them. */
function toggleLyrics() {
    const cover = $('player-cover');
    const lyrics = $('player-lyrics');
    if (!cover || !lyrics) return;
    const showLyrics = lyrics.hidden;
    lyrics.hidden = !showLyrics;
    cover.hidden = showLyrics;
    if (showLyrics && !state.lyrics.length) loadLyrics(currentTrack());
    if (showLyrics) updateLyricHighlight();
}

function closePlayer() {
    $('view-player').classList.remove('is-active');
    /* Leaving the player puts the cover back, so the next song starts
       from the cover again. */
    const cover = $('player-cover');
    const lyrics = $('player-lyrics');
    if (cover && lyrics) {
        lyrics.hidden = true;
        cover.hidden = false;
    }
}

/* ------------------------------------------------------------------- home */
let homeLoaded = false;

async function loadHome(force) {
    if (homeLoaded && !force) return;
    homeLoaded = true;

    const fmHost = $('home-fm');
    const newHost = $('home-new');
    const playlistHost = $('home-playlists');
    const topHost = $('home-tops');
    const mvHost = $('home-mvs');

    showSpinner(fmHost);
    const fm = await api('get_personal_fm');
    if (fm && fm.success && fm.tracks && fm.tracks.length) {
        renderTracks(fmHost, fm.tracks.slice(0, 10));
    } else {
        $('home-fm-block').hidden = true;
    }

    showSpinner(newHost);
    const fresh = await api('get_new_songs', 20, 0);
    if (fresh && fresh.success && fresh.tracks && fresh.tracks.length) {
        renderTracks(newHost, fresh.tracks.slice(0, 10));
    } else {
        $('home-new-block').hidden = true;
    }

    showSpinner(playlistHost);
    const playlists = await api('get_recommend_playlists');
    cardGrid(playlistHost, (playlists && playlists.playlists) || [], (item) => {
        openDetail('playlist', item);
    }, (item) => (item.play_count ? `${item.play_count}次播放` : ''));

    showSpinner(topHost);
    const tops = await api('get_top_lists');
    clear(topHost);
    ((tops && tops.toplists) || []).forEach((top) => {
        const chip = el('button', 'chip', top.name || '');
        chip.type = 'button';
        chip.addEventListener('click', () => openDetail('toplist', top));
        topHost.appendChild(chip);
    });
    if (!topHost.children.length) topHost.appendChild(el('span', 'small text-3', '暂时没有榜单'));

    showSpinner(mvHost);
    const mvs = await api('get_recommend_mvs', 10, 0);
    cardGrid(mvHost, (mvs && mvs.mvs) || [], (item) => {
        showToast(`《${item.name || ''}》暂时只能在网页版播放`);
    }, () => 'MV');
}

/* ----------------------------------------------------------------- search */
async function loadSuggest() {
    const host = $('search-suggest');
    if (host.children.length) return;
    const result = await api('search_suggest', '');
    clear(host);
    ((result && result.suggestions) || []).slice(0, 8).forEach((word) => {
        const chip = el('button', 'chip', word);
        chip.type = 'button';
        chip.addEventListener('click', () => {
            $('search-input').value = word;
            runSearch();
        });
        host.appendChild(chip);
    });
}

async function runSearch() {
    const keyword = $('search-input').value.trim();
    const host = $('search-results');
    if (!keyword) { clear(host); return; }
    showSpinner(host);
    const result = await api('search_multi', keyword, 30, 0);
    if (!result || !result.success) {
        showEmpty(host, t('player.search_failed'), errorText(result, ''));
        return;
    }
    const groups = (result.results) || {};
    clear(host);
    const type = state.searchType;
    if (type === 'songs') {
        renderTracks(host, groups.songs || [], '没有找到单曲');
    } else if (type === 'artists') {
        cardGrid(host, groups.artists || [], (item) => openDetail('artist', item), () => '歌手');
    } else if (type === 'albums') {
        cardGrid(host, groups.albums || [], (item) => openDetail('album', item), () => '专辑');
    } else if (type === 'playlists') {
        cardGrid(host, groups.playlists || [], (item) => openDetail('playlist', item), () => '歌单');
    } else if (type === 'mvs') {
        cardGrid(host, groups.mvs || [], (item) => {
            showToast(`《${item.name || ''}》暂时不能在手机上播放`);
        }, () => 'MV');
    }
}

/* ---------------------------------------------------------------- library */
async function loadLibrary() {
    const host = $('library-body');
    showSpinner(host);
    if (state.libraryTab === 'mine') {
        const result = await api('get_playlists');
        cardGrid(host, (result && result.playlists) || [], (item) => openDetail('playlist', item),
            (item) => (item.track_count !== undefined ? `${item.track_count} 首` : ''));
    } else if (state.libraryTab === 'liked') {
        const result = await api('get_liked_tracks');
        renderTracks(host, (result && result.tracks) || [], '还没有收藏的歌');
    } else {
        await renderLocalMusic(host);
    }
}

/**
 * The songs on the phone.
 *
 * The phone keeps its music in the system media library, so the app has
 * to ask for the audio permission before it can see anything.  Without
 * that permission the list is empty -- and the user is told why, with a
 * button that asks for it.
 */
async function renderLocalMusic(host) {
    const result = await api('get_local_tracks', '', 200, 0);
    const tracks = (result && result.tracks) || [];

    if (!tracks.length) {
        const box = el('div', 'stack');
        showEmpty(box, '手机里没有找到歌曲',
            '先允许读取音频文件，再点下面的按钮重新找一次');
        const ask = el('button', 'btn btn-primary', '允许读取音频文件');
        ask.type = 'button';
        ask.addEventListener('click', async () => {
            const granted = await api('request_audio_permission');
            if (!granted || granted.success === false) {
                showToast('没有权限就读不到歌曲');
            }
            await api('scan_local_music', true);
            showSpinner(host);
            await renderLocalMusic(host);
        });
        box.appendChild(ask);

        const again = el('button', 'btn btn-ghost', '重新找一次');
        again.type = 'button';
        again.addEventListener('click', async () => {
            showSpinner(host);
            await api('scan_local_music', true);
            await renderLocalMusic(host);
        });
        box.appendChild(again);
        clear(host).appendChild(box);
        return;
    }

    renderTracks(host, tracks, '手机里没有找到歌曲');
}

/* -------------------------------------------------------------- downloads */
async function loadDownloads() {
    const statsHost = $('download-stats');
    const queueHost = $('download-queue');
    const localHost = $('download-local');
    /* The queue used to get a spinner that nothing ever replaced, so the
       page spun forever.  The download progress arrives pushed from the
       bridge, so the queue is drawn from that state: no task means an
       empty state, not a spinner that never stops. */
    renderDownloadQueue(queueHost, state.downloadStats);
    showSpinner(localHost);
    const local = await api('get_local_tracks', '', 50, 0);
    renderTracks(localHost, (local && local.tracks) || [], '还没有下载好的歌');
}

/** The download queue, drawn from the pushed progress state. */
function renderDownloadQueue(host, stats) {
    clear(host);
    const data = stats || {};
    const total = Number(data.total) || 0;
    if (!total) {
        showEmpty(host, '还没有下载任务', '在歌单里点「下载全部」就能开始');
        return;
    }
    const row = el('div', 'list-row');
    row.appendChild(el('span', 'grow', `正在下载 ${data.current || 0}/${total}`));
    host.appendChild(row);
    if (data.failed) {
        const failed = el('div', 'list-row');
        failed.appendChild(el('span', 'grow', `失败 ${data.failed} 首`));
        host.appendChild(failed);
    }
}

/* The bridge pushes the download progress instead of being polled. */
function renderDownloadStats(host, stats) {
    clear(host);
    const data = stats || {};
    /* The bridge counts with "finished" and "total"; there is no
     * "completed" and no "downloading", so both of those used to read
     * 0 forever. */
    [['已完成', data.finished || 0], ['总数', data.total || 0],
     ['失败', data.failed || 0]].forEach(([label, value]) => {
        const box = el('div', 'stat');
        box.appendChild(el('div', 'stat-value', String(value)));
        box.appendChild(el('div', 'stat-label', label));
        host.appendChild(box);
    });
    if (data.total) {
        const box = el('div', 'stat');
        box.appendChild(el('div', 'stat-value', `${data.current || 0}/${data.total}`));
        box.appendChild(el('div', 'stat-label', '进度'));
        host.appendChild(box);
    }
}

window.onDownloadProgress = (payload) => {
    state.downloadStats = Object.assign({}, state.downloadStats, payload || {});
    if (state.tab === 'downloads') renderDownloadStats($('download-stats'), state.downloadStats);
};

/* --------------------------------------------------------------------- me */
async function loadMe() {
    const host = $('me-body');
    showSpinner(host);
    const status = await api('get_login_status');
    state.login = status || {};
    clear(host);

    if (status && status.is_logged_in) {
        const profile = el('div', 'profile');
        const avatar = el('div', 'profile-avatar');
        // The user lives under ``status.user``: reading it off the top
        // level gave an empty nickname and a UID of 0, so the page said
        // "已登录" for an account that was signed in properly.
        const user = status.user || {};
        if (user.avatar_url) {
            coverFor({ cover_url: user.avatar_url }).then((url) => {
                if (url) avatar.style.backgroundImage = `url("${url}")`;
            });
        }
        profile.appendChild(avatar);
        const info = el('div', 'grow');
        info.appendChild(el('div', 'profile-name', user.nickname || '已登录'));
        info.appendChild(el('div', 'profile-sub', user.user_id ? `UID ${user.user_id}` : ''));
        profile.appendChild(info);
        host.appendChild(profile);
    } else {
        const box = el('div', 'list');
        const row = el('button', 'list-row');
        row.type = 'button';
        row.appendChild(el('span', 'grow', '登录网易云账号'));
        row.appendChild(el('span', 'chev', '›'));
        row.addEventListener('click', openLogin);
        box.appendChild(row);
        host.appendChild(box);
    }

    const list = el('div', 'list');
    const addRow = (label, value, onClick) => {
        const row = el('button', 'list-row');
        row.type = 'button';
        row.appendChild(el('span', 'grow', label));
        if (value) row.appendChild(el('span', 'value', value));
        row.appendChild(el('span', 'chev', '›'));
        row.addEventListener('click', onClick);
        list.appendChild(row);
    };
    addRow('我关注的歌手', '', () => openFollowedArtists());
    addRow('设置', '', () => openSettings());
    if (status && status.is_logged_in) {
        addRow('退出登录', '', async () => {
            await api('logout');
            homeLoaded = false;
            loadMe();
        });
    }
    host.appendChild(list);

    const tools = el('div', 'list');
    const toolRow = (label, onClick) => {
        const row = el('button', 'list-row');
        row.type = 'button';
        row.appendChild(el('span', 'grow', label));
        row.appendChild(el('span', 'chev', '›'));
        row.addEventListener('click', onClick);
        tools.appendChild(row);
    };
    toolRow('导出账号数据', exportBackup);
    toolRow('复制登录 Cookie', copyCookie);
    toolRow('清理缓存', async () => {
        const result = await api('clear_cache', 'all');
        showToast(result && result.success ? '缓存已清理' : '清理失败');
    });
    host.appendChild(tools);
}

/* ------------------------------------------------------------------ detail */
async function openDetail(kind, item) {
    const head = $('detail-head');
    const body = $('detail-body');
    clear(head);
    clear(body);
    $('view-detail').classList.add('is-active');
    $('btn-back').hidden = false;
    pushView('detail');

    const cover = el('div', 'detail-cover');
    paintCover(cover, item);
    head.appendChild(cover);
    const info = el('div', 'grow');
    info.appendChild(el('div', 'detail-title', item.name || ''));
    info.appendChild(el('div', 'detail-sub', item.subtitle || ''));
    head.appendChild(info);

    const actions = el('div', 'detail-actions');
    head.appendChild(actions);

    const playAll = el('button', 'btn btn-primary', '播放全部');
    playAll.type = 'button';
    actions.appendChild(playAll);

    if (kind === 'artist') {
        const follow = el('button', 'btn btn-ghost', item.followed ? '已关注' : '关注');
        follow.type = 'button';
        actions.appendChild(follow);
        follow.addEventListener('click', async () => {
            const result = await api('follow_artist', String(item.id || ''), !item.followed);
            if (!result || !result.success) { showToast('操作失败'); return; }
            item.followed = result.followed;
            follow.textContent = result.followed ? '已关注' : '关注';
            showToast(result.followed ? '已关注' : '已取消关注', {
                undoLabel: '撤销',
                onUndo: async () => {
                    const undo = await api('follow_artist', String(item.id || ''), !result.followed);
                    if (undo && undo.success) {
                        item.followed = undo.followed;
                        follow.textContent = undo.followed ? '已关注' : '关注';
                        showToast('已撤销', undefined);
                    }
                },
            });
        });
    }

    showSpinner(body);
    if (kind === 'playlist' || kind === 'toplist') {
        const id = kind === 'playlist' ? item.id : item.id;
        /* A big playlist is fetched in batches and can take a while, so
           the page says what it is doing instead of showing a bare
           spinner. */
        const note = el('div', 'small text-3', '正在读取歌曲…');
        body.appendChild(note);
        const result = await api('get_playlist_tracks', String(id || ''));
        const tracks = (result && result.playlist && result.playlist.tracks)
            || (result && result.tracks) || [];
        if (!tracks.length && result && result.error_key) {
            clear(body);
            showEmpty(body, '读不出来', errorText(result, '这个歌单暂时读不到'));
            return;
        }
        renderTracks(body, tracks, '这个歌单还没有歌');
        playAll.addEventListener('click', () => playTracks(tracks, 0));
        const download = el('button', 'btn btn-ghost', '下载全部');
        download.type = 'button';
        actions.appendChild(download);
        download.addEventListener('click', () => downloadTracks(tracks));
    } else if (kind === 'artist') {
        const result = await api('get_artist_detail', String(item.id || ''));
        const artist = (result && result.artist) || {};
        /* The bridge puts the hot songs next to the artist, not inside
           it, so they are read from the top level. */
        const tracks = result.hot_songs || artist.hot_songs || [];
        if (!tracks.length && result && result.error_key) {
            clear(body);
            showEmpty(body, '读不出来', errorText(result, '这个歌手暂时读不到'));
            return;
        }
        renderTracks(body, tracks, '这个歌手还没有热门歌曲');
        playAll.addEventListener('click', () => playTracks(tracks, 0));
    } else if (kind === 'album') {
        const result = await api('get_album_detail', String(item.id || ''));
        const album = (result && result.album) || {};
        const tracks = result.tracks || album.tracks || [];
        if (!tracks.length && result && result.error_key) {
            clear(body);
            showEmpty(body, '读不出来', errorText(result, '这张专辑暂时读不到'));
            return;
        }
        renderTracks(body, tracks, '这张专辑还没有歌');
        playAll.addEventListener('click', () => playTracks(tracks, 0));
    }
}

function closeDetail() {
    $('view-detail').classList.remove('is-active');
    $('btn-back').hidden = true;
}

/* ------------------------------------------------------- followed artists */
/** One followed artist as a row: a small avatar and the name next to it. */
function artistRow(artist) {
    const row = el('button', 'list-row artist-row');
    row.type = 'button';
    const avatar = el('div', 'artist-avatar');
    paintCover(avatar, artist);
    row.appendChild(avatar);
    row.appendChild(el('span', 'grow', artist.name || '未知歌手'));
    row.addEventListener('click', () => {
        closeSheet();
        openDetail('artist', artist);
    });
    return row;
}

async function openFollowedArtists() {
    const body = el('div', 'stack');
    openSheet('我关注的歌手', body);
    showSpinner(body);
    const result = await api('get_followed_artists', 50, 0);
    clear(body);
    const artists = (result && result.artists) || [];
    if (!artists.length) {
        const hint = result && result.error_key
            ? errorText(result, '取不到关注的歌手')
            : '还没有关注任何歌手';
        showEmpty(body, '没有关注的人', hint);
        return;
    }
    const list = el('div', 'list');
    artists.forEach((artist) => list.appendChild(artistRow(artist)));
    body.appendChild(list);
    const count = el('div', 'small text-3', `共 ${artists.length} 位`);
    body.appendChild(count);
}

/* --------------------------------------------------------------- settings */
/**
 * The settings page.
 *
 * Everything here comes from the bridge: the values it reports and the
 * values it accepts.  Nothing is invented on the phone side.
 */

/** The quality levels the API understands, in the order they are shown. */
const QUALITY_LEVELS = [
    ['standard', '标准'],
    ['higher', '较高'],
    ['exhigh', '极高'],
    ['lossless', '无损'],
    ['hires', 'Hi-Res'],
];

/** The theme modes, with the label the phone shows for each. */
const THEME_MODES = [
    ['system', '跟随系统'],
    ['light', '浅色'],
    ['dark', '深色'],
];

/**
 * Show the theme the user picked.
 *
 * "Follow the system" is not a colour of its own: the stylesheet asks
 * the system through ``prefers-color-scheme``, so the attribute is left
 * off and the phone's own setting decides.  The other two set it, and
 * the stylesheet's explicit rules win over the media query.
 */
function applyTheme(mode) {
    const theme = mode === 'light' || mode === 'dark' ? mode : 'system';
    if (theme === 'system') {
        delete document.documentElement.dataset.theme;
    } else {
        document.documentElement.dataset.theme = theme;
    }
}

/**
 * Save the theme and show it.
 *
 * The bridge is told as well, so the choice survives a restart and the
 * native window chrome (the status bar) matches the page.
 */
async function saveTheme(mode) {
    applyTheme(mode);
    if (!state.api || typeof state.api.set_theme !== 'function') return true;
    const result = await state.api.set_theme(mode);
    return !!(result && result.success !== false);
}

/**
 * A row that opens a list of choices and writes the picked one back.
 *
 * A chip row rather than a dropdown: on a phone a dropdown is a small
 * target and the system picker looks foreign, while the chips are the
 * same control the rest of the interface already uses.
 *
 * @param {object} host    where the row is appended
 * @param {string} label   what the row is about
 * @param {string} group   the settings group the bridge wants
 * @param {string} key     the field inside that group
 * @param {Array}  options [[value, label], ...]
 * @param {string} current the value the bridge reported
 * @param {string} hint    the small line under the label
 * @param {Function} save  optional custom writer; returns true when the
 *                         value stuck
 */
function choiceRow(host, label, group, key, options, current, hint, save) {
    const row = el('div', 'list-row');
    const text = el('div', 'grow');
    text.appendChild(el('div', '', label));
    if (hint) text.appendChild(el('div', 'small text-3', hint));
    row.appendChild(text);

    const chips = el('div', 'chiprow');
    chips.style.marginTop = '8px';
    chips.appendChild(el('div', 'small text-3', '当前'));
    for (const [value, text_label] of options) {
        const chip = el('button', 'chip', text_label);
        chip.type = 'button';
        if (value === current) chip.classList.add('is-on');
        chip.addEventListener('click', async () => {
            if (value === current) return;
            for (const other of chips.querySelectorAll('.chip')) {
                other.classList.remove('is-on');
            }
            chip.classList.add('is-on');
            let ok = true;
            if (save) {
                ok = await save(value);
            } else {
                const payload = {};
                payload[key] = value;
                const result = await api('update_settings', group, payload);
                ok = !!(result && result.success !== false);
            }
            if (!ok) {
                // Put the old value back: the change did not stick.
                for (const other of chips.querySelectorAll('.chip')) {
                    other.classList.remove('is-on');
                }
                for (const [v, l] of options) {
                    if (v === current) {
                        const back = Array.from(chips.querySelectorAll('.chip'))
                            .find((c) => c.textContent === l);
                        if (back) back.classList.add('is-on');
                    }
                }
                showToast('保存不了');
                return;
            }
            showToast('已保存');
        });
        chips.appendChild(chip);
    }
    row.appendChild(chips);
    host.appendChild(row);
}

/**
 * A row with a number field: the value is typed, not picked.
 *
 * @param {object} host    where the row is appended
 * @param {string} label   what the row is about
 * @param {string} group   the settings group the bridge wants
 * @param {string} key     the field inside that group
 * @param {number} current the value the bridge reported
 * @param {string} unit    the unit shown after the number
 * @param {string} hint    the small line under the label
 * @param {Function} save  optional custom writer; returns true when the
 *                         value stuck
 */
function numberRow(host, label, group, key, current, unit, hint, save) {
    const row = el('div', 'list-row');
    const text = el('div', 'grow');
    text.appendChild(el('div', '', label));
    if (hint) text.appendChild(el('div', 'small text-3', hint));
    row.appendChild(text);

    const input = el('input', 'text-input');
    input.type = 'number';
    input.min = '0';
    input.step = '1';
    input.value = String(Number(current) || 0);
    input.style.width = '96px';
    input.addEventListener('change', async () => {
        const value = Math.max(0, Number(input.value) || 0);
        input.value = String(value);
        let ok = true;
        if (save) {
            ok = await save(value);
        } else {
            const payload = {};
            payload[key] = value;
            const result = await api('update_settings', group, payload);
            ok = !!(result && result.success !== false);
        }
        if (!ok) {
            input.value = String(Number(current) || 0);
            showToast('保存不了');
            return;
        }
        showToast('已保存');
    });
    row.appendChild(input);
    if (unit) row.appendChild(el('span', 'value', unit));
    host.appendChild(row);
}

async function openSettings() {
    const body = el('div', 'stack');
    openSheet('设置', body);
    showSpinner(body);

    const [settings, cache] = await Promise.all([
        api('get_settings'),
        api('get_cache_stats'),
    ]);
    clear(body);
    if (!settings || settings.success === false) {
        showEmpty(body, '读不到设置', errorText(settings, '请稍后再试'));
        return;
    }

    const playback = settings.playback || {};
    const download = settings.download || {};
    const ui = settings.ui || {};
    const auth = settings.auth || {};

    const list = el('div', 'list');

    /* The bridge groups the settings, and takes the group name plus a
       dict of the fields to change, so a switch here writes into its
       own group. */
    const switchRow = (label, group, key, hint) => {
        const row = el('div', 'list-row');
        const text = el('div', 'grow');
        text.appendChild(el('div', '', label));
        if (hint) text.appendChild(el('div', 'small text-3', hint));
        row.appendChild(text);
        const toggle = el('button', 'switch');
        toggle.type = 'button';
        const value = Boolean((settings && settings[group] && settings[group][key]));
        toggle.classList.toggle('is-on', value);
        toggle.setAttribute('aria-checked', value ? 'true' : 'false');
        toggle.setAttribute('aria-label', label);
        toggle.addEventListener('click', async () => {
            const next = !toggle.classList.contains('is-on');
            toggle.classList.toggle('is-on', next);
            toggle.setAttribute('aria-checked', next ? 'true' : 'false');
            const payload = {};
            payload[key] = next;
            const result = await api('update_settings', group, payload);
            if (!result || result.success === false) {
                toggle.classList.toggle('is-on', !next);
                toggle.setAttribute('aria-checked', !next ? 'true' : 'false');
                showToast('保存不了');
                return;
            }
            showToast(next ? '已打开' : '已关闭');
        });
        row.appendChild(toggle);
        list.appendChild(row);
    };

    switchRow('记住登录', 'auth', 'remember_login', '下次打开不用重新登录');
    switchRow('显示歌词翻译', 'playback', 'show_translation', '有翻译时一起显示');
    switchRow('优先在线播放', 'playback', 'prefer_online', '没有下载的歌走在线');
    switchRow('播放次数同步到网易云', 'playback', 'report_play_count',
        '每首歌听完会上报一次');
    switchRow('下载歌词', 'download', 'download_lyrics', '下载时把歌词一起存下来');
    switchRow('下载时覆盖已有文件', 'download', 'overwrite', '同名文件直接盖掉');
    body.appendChild(list);

    body.appendChild(el('div', 'section-title', '播放'));
    const playbackBox = el('div', 'list');
    choiceRow(playbackBox, '在线播放音质', 'playback', 'online_quality',
        QUALITY_LEVELS, playback.online_quality || 'standard',
        '网络不够时自动降档');
    choiceRow(playbackBox, '下载音质', 'download', 'quality',
        QUALITY_LEVELS, download.quality || 'standard',
        '下载下来的文件用这一档');
    choiceRow(playbackBox, '播放模式', 'playback', 'play_mode',
        [['sequence', '顺序'], ['loop', '循环'], ['shuffle', '随机']],
        playback.play_mode || 'sequence');
    numberRow(playbackBox, '淡入淡出时长', 'playback', 'crossfade_duration',
        Number(playback.crossfade_duration) || 0, '秒', '两首歌之间交叉淡化的秒数',
        async (value) => {
            // The crossfade is not only a stored number: the media server
            // has to know it too, so this goes through the bridge method
            // that does both.
            const result = await api('set_crossfade_duration', value);
            if (!result || result.success === false) {
                showToast('保存不了');
                return false;
            }
            return true;
        });
    body.appendChild(playbackBox);

    body.appendChild(el('div', 'section-title', '外观'));
    const uiBox = el('div', 'list');
    choiceRow(uiBox, '主题', 'ui', 'theme', THEME_MODES, ui.theme || 'system',
        '跟随系统会跟着手机的深浅色走', async (value) => {
            const ok = await saveTheme(value);
            if (!ok) showToast('保存不了');
            return ok;
        });
    body.appendChild(uiBox);

    await renderBackgroundSetting(uiBox, ui);

    body.appendChild(el('div', 'section-title', '账号'));
    const accountBox = el('div', 'list');
    await renderAccountSettings(accountBox);
    body.appendChild(accountBox);

    body.appendChild(el('div', 'section-title', '缓存'));
    body.appendChild(renderCacheStats(cache));
}

/**
 * The wallpaper row: the current one, a button to pick a new one and a
 * button to take the current one away.
 *
 * The phone has no native file dialog that the bridge can open, so the
 * picture is picked with a plain ``<input type="file">``: the WebView
 * hands the picked file to the page, and the page hands it to the bridge
 * as a data url.
 */
async function renderBackgroundSetting(host, ui) {
    const row = el('div', 'list-row');
    const text = el('div', 'grow');
    text.appendChild(el('div', '', '背景图'));
    text.appendChild(el('div', 'small text-3',
        ui.background_image ? '已设置一张背景图' : '没有背景图'));
    row.appendChild(text);

    const picker = el('input');
    picker.type = 'file';
    picker.accept = 'image/*';
    picker.style.display = 'none';
    picker.addEventListener('change', async () => {
        const file = picker.files && picker.files[0];
        if (!file) return;
        const dataUrl = await readFileAsDataUrl(file);
        const result = await api('set_background_image_data', dataUrl);
        if (!result || result.success === false) {
            showToast(errorText(result, '背景图换不了'));
            return;
        }
        showToast('背景图已换');
        openSettings();
    });
    host.appendChild(picker);

    const pick = el('button', 'btn btn-ghost', '换一张');
    pick.type = 'button';
    pick.addEventListener('click', () => picker.click());
    row.appendChild(pick);

    if (ui.background_image) {
        const clear = el('button', 'btn btn-ghost', '去掉');
        clear.type = 'button';
        clear.addEventListener('click', async () => {
            const result = await api('clear_background_image');
            if (!result || result.success === false) {
                showToast(errorText(result, '去不掉'));
                return;
            }
            showToast('背景图已去掉');
            openSettings();
        });
        row.appendChild(clear);
    }
    host.appendChild(row);
}

/** A picked image as a data url, so it can be handed to the bridge. */
function readFileAsDataUrl(file) {
    return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => resolve('');
        reader.readAsDataURL(file);
    });
}

/**
 * The account rows: paste a cookie to sign in, and import a saved
 * account file.
 */
async function renderAccountSettings(host) {
    const cookieRow = el('div', 'list-row');
    cookieRow.appendChild(el('span', 'grow', '用 cookie 登录'));
    const cookieInput = el('input', 'text-input');
    cookieInput.type = 'text';
    cookieInput.placeholder = 'MUSIC_U=...';
    cookieInput.style.flex = '1 1 160px';
    cookieRow.appendChild(cookieInput);
    const cookieButton = el('button', 'btn btn-primary', '登录');
    cookieButton.type = 'button';
    cookieButton.addEventListener('click', async () => {
        const value = cookieInput.value.trim();
        if (!value) {
            showToast('先把 cookie 贴进来');
            return;
        }
        cookieButton.disabled = true;
        const result = await api('login_cookie', value);
        cookieButton.disabled = false;
        if (!result || result.success === false) {
            showToast(errorText(result, '登录失败'));
            return;
        }
        showToast('登录成功');
        cookieInput.value = '';
    });
    cookieRow.appendChild(cookieButton);
    host.appendChild(cookieRow);

    const importRow = el('div', 'list-row');
    importRow.appendChild(el('span', 'grow', '导入账号数据'));
    const picker = el('input');
    picker.type = 'file';
    picker.accept = '.ncmbk,.json,application/json';
    picker.style.display = 'none';
    picker.addEventListener('change', async () => {
        const file = picker.files && picker.files[0];
        if (!file) return;
        const dataUrl = await readFileAsDataUrl(file);
        const plan = await api('import_account_data', dataUrl);
        if (!plan || plan.success === false) {
            showToast(errorText(plan, '导入失败'));
            return;
        }
        await resolveBackupConflicts(plan);
    });
    host.appendChild(picker);
    const importButton = el('button', 'btn btn-ghost', '选文件');
    importButton.type = 'button';
    importButton.addEventListener('click', () => picker.click());
    importRow.appendChild(importButton);
    host.appendChild(importRow);
}

/**
 * The playlists that already exist on both sides, and what to do with
 * them.
 *
 * Nothing is written until the user decides: the bridge holds the plan
 * until ``apply_account_backup`` is called with the decisions.
 */
async function resolveBackupConflicts(plan) {
    const conflicts = (plan && plan.conflicts) || [];
    if (!conflicts.length) {
        const result = await api('apply_account_backup', {});
        if (!result || result.success === false) {
            showToast(errorText(result, '导入失败'));
            return;
        }
        showToast('账号数据已导入');
        return;
    }

    const body = el('div', 'stack');
    openSheet('导入账号数据', body);
    const summary = (plan && plan.summary) || {};
    body.appendChild(el('div', 'small text-3',
        `要新建 ${summary.playlists_to_create || 0} 个歌单，`
        + `有 ${conflicts.length} 个歌单两边都有，需要你选一下`));

    const decisions = { playlists: {} };
    const list = el('div', 'list');
    for (const item of conflicts) {
        const row = el('div', 'list-row');
        const text = el('div', 'grow');
        text.appendChild(el('div', '', item.name || item.id || ''));
        row.appendChild(text);

        const chips = el('div', 'chiprow');
        for (const [action, label] of [['skip', '跳过'], ['merge', '合并']]) {
            const chip = el('button', 'chip', label);
            chip.type = 'button';
            chip.addEventListener('click', () => {
                for (const other of chips.querySelectorAll('.chip')) {
                    other.classList.remove('is-on');
                }
                chip.classList.add('is-on');
                decisions.playlists[String(item.id)] = { action };
            });
            chips.appendChild(chip);
        }
        row.appendChild(chips);
        list.appendChild(row);
    }
    body.appendChild(list);

    const actions = el('div', 'chiprow');
    const go = el('button', 'btn btn-primary', '导入');
    go.type = 'button';
    go.addEventListener('click', async () => {
        go.disabled = true;
        const result = await api('apply_account_backup', decisions);
        go.disabled = false;
        closeSheet();
        if (!result || result.success === false) {
            showToast(errorText(result, '导入失败'));
            return;
        }
        showToast('账号数据已导入');
    });
    const cancel = el('button', 'btn btn-ghost', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', () => {
        closeSheet();
    });
    actions.appendChild(go);
    actions.appendChild(cancel);
    body.appendChild(actions);
}

/** The cache size and a button to clear it. */
function renderCacheStats(cache) {
    const box = el('div', 'list');
    const data = cache || {};
    const audio = data.audio || {};
    const cover = data.cover || {};
    const audioBytes = Number(audio.bytes) || 0;
    const coverBytes = Number(cover.bytes) || 0;
    const metaBytes = Number(data.meta_bytes) || 0;
    const total = audioBytes + coverBytes + metaBytes;

    const row = el('div', 'list-row');
    row.appendChild(el('span', 'grow', '缓存大小'));
    row.appendChild(el('span', 'value', formatCacheSize(total)));
    box.appendChild(row);

    const audioRow = el('div', 'list-row');
    audioRow.appendChild(el('span', 'grow', '歌曲'));
    audioRow.appendChild(el('span', 'value',
        `${formatCacheSize(audioBytes)}（${audio.count || 0} 个）`));
    box.appendChild(audioRow);

    const coverRow = el('div', 'list-row');
    coverRow.appendChild(el('span', 'grow', '封面'));
    coverRow.appendChild(el('span', 'value',
        `${formatCacheSize(coverBytes)}（${cover.count || 0} 个）`));
    box.appendChild(coverRow);

    const metaRow = el('div', 'list-row');
    metaRow.appendChild(el('span', 'grow', '信息与歌词'));
    metaRow.appendChild(el('span', 'value', formatCacheSize(metaBytes)));
    box.appendChild(metaRow);

    if (data.available === false) {
        const note = el('div', 'small text-3', '缓存功能没有打开，所以这里一直是空的。');
        box.appendChild(note);
    }

    const clear = el('button', 'list-row');
    clear.type = 'button';
    clear.appendChild(el('span', 'grow', '清除缓存'));
    clear.appendChild(el('span', 'chev', '›'));
    clear.addEventListener('click', async () => {
        clear.disabled = true;
        const result = await api('clear_cache', 'all');
        clear.disabled = false;
        if (!result || result.success === false) {
            showToast('清除失败');
            return;
        }
        showToast('缓存已清除');
        const fresh = await api('get_cache_stats');
        const holder = $('sheet-body');
        if (holder) {
            clear(holder);
            holder.appendChild(renderCacheStats(fresh));
        }
    });
    box.appendChild(clear);
    return box;
}

/** A byte count as something a person can read. */
function formatCacheSize(bytes) {
    const value = Number(bytes) || 0;
    if (value <= 0) return '0 B';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
    return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/* ------------------------------------------------------------------ login */
async function openLogin() {
    const body = el('div', 'stack');
    const tabs = el('div', 'chiprow');
    const qrChip = el('button', 'chip is-on', '扫码登录');
    const phoneChip = el('button', 'chip', '手机号登录');
    tabs.appendChild(qrChip);
    tabs.appendChild(phoneChip);
    body.appendChild(tabs);

    const qrBox = el('div', 'qrbox');
    const phoneBox = el('div');
    phoneBox.hidden = true;
    body.appendChild(qrBox);
    body.appendChild(phoneBox);

    qrChip.addEventListener('click', () => {
        qrChip.classList.add('is-on'); phoneChip.classList.remove('is-on');
        qrBox.hidden = false; phoneBox.hidden = true;
    });
    phoneChip.addEventListener('click', () => {
        phoneChip.classList.add('is-on'); qrChip.classList.remove('is-on');
        qrBox.hidden = true; phoneBox.hidden = false;
    });

    openSheet('登录网易云', body);

    // --- QR ---------------------------------------------------------------
    const image = el('img');
    image.alt = '登录二维码';
    qrBox.appendChild(image);
    const hint = el('div', 'hint', '正在取二维码…');
    qrBox.appendChild(hint);
    let timer = null;

    const paintQr = async () => {
        const result = await api('login_qrcode');
        if (result && result.success && result.qr_code) {
            image.src = result.qr_code.startsWith('data:')
                ? result.qr_code
                : `data:image/png;base64,${result.qr_code}`;
            hint.textContent = '用网易云 App 扫这个码';
            const status = await api('get_login_status');
            if (status && status.is_logged_in) {
                clearInterval(timer);
                closeSheet();
                showToast('登录成功');
                homeLoaded = false;
                loadMe();
            }
        } else {
            hint.textContent = errorText(result, '二维码取不到，稍后重试');
        }
    };
    paintQr();
    timer = setInterval(paintQr, 3000);

    // --- phone ------------------------------------------------------------
    const phoneField = el('div', 'field');
    phoneField.appendChild(el('label', null, '手机号'));
    const phoneInput = el('input');
    phoneInput.type = 'tel';
    phoneInput.inputMode = 'numeric';
    phoneInput.placeholder = '11 位手机号';
    phoneField.appendChild(phoneInput);
    phoneBox.appendChild(phoneField);

    const codeField = el('div', 'field');
    codeField.appendChild(el('label', null, '验证码'));
    const codeInput = el('input');
    codeInput.type = 'text';
    codeInput.inputMode = 'numeric';
    codeField.appendChild(codeInput);
    phoneBox.appendChild(codeField);

    const sendRow = el('div', 'row');
    const sendButton = el('button', 'btn btn-ghost', '发验证码');
    sendButton.type = 'button';

    // NetEase refuses a second code for the same number for a while, and
    // every extra request extends that wait.  The button therefore counts
    // down and stays disabled, exactly like the desktop does -- otherwise
    // the second tap comes back with "操作过于频繁" and the user has no
    // idea why.
    let codeTimer = null;
    const startCountdown = (seconds = 60) => {
        if (codeTimer) clearInterval(codeTimer);
        sendButton.disabled = true;
        let remaining = seconds;
        const tick = () => {
            if (remaining <= 0) {
                clearInterval(codeTimer);
                codeTimer = null;
                sendButton.disabled = false;
                sendButton.textContent = '发验证码';
                return;
            }
            sendButton.textContent = `${remaining} 秒后重发`;
            remaining -= 1;
        };
        tick();
        codeTimer = setInterval(tick, 1000);
    };

    sendButton.addEventListener('click', async () => {
        if (sendButton.disabled) return;
        const phone = phoneInput.value.trim();
        if (!/^\d{6,15}$/.test(phone)) { showToast('手机号填对了吗'); return; }
        sendButton.disabled = true;
        const result = await api('send_phone_code', phone, '86');
        if (result && result.success) {
            showToast(t('login.code_sent'));
            startCountdown(60);
        } else if (result && (result.throttled || result.error_key === 'login.code_too_frequent')) {
            showToast(t('login.code_too_frequent', { message: result.message || '' }));
            startCountdown(180);
        } else {
            showToast(errorText(result, t('login.code_send_failed', { message: '' })));
            startCountdown(60);
        }
    });
    const loginButton = el('button', 'btn btn-primary grow', '登录');
    loginButton.type = 'button';
    loginButton.addEventListener('click', async () => {
        const phone = phoneInput.value.trim();
        const code = codeInput.value.trim();
        if (!phone || !code) { showToast('手机号和验证码都要填'); return; }
        const result = await api('login_phone', phone, code, '86');
        if (result && result.success) {
            clearInterval(timer);
            closeSheet();
            showToast(t('login.login_success'));
            homeLoaded = false;
            loadMe();
        } else {
            showToast(errorText(result, t('login.login_failed')));
        }
    });
    sendRow.appendChild(sendButton);
    sendRow.appendChild(loginButton);
    phoneBox.appendChild(sendRow);
}

/* --------------------------------------------------------------- downloads */
/**
 * Download the given tracks.
 *
 * The options (lyrics, overwriting) are asked for first: they are the two
 * choices that change what lands on the disk, and the user should not
 * have to dig them out of the settings to get a download they expected.
 */
async function downloadTracks(tracks) {
    if (!tracks || !tracks.length) { showToast('没有要下载的歌'); return; }
    const options = await askDownloadOptions();
    if (!options) return;
    const result = await api('download_tracks', tracks, options);
    if (result && result.success) {
        showToast(`开始下载 ${tracks.length} 首`);
    } else {
        showToast(errorText(result, '下载没能开始'));
    }
}

/**
 * The download options, asked for in a sheet.
 *
 * Returns the options, or ``null`` when the user backed out.
 */
function askDownloadOptions() {
    return new Promise((resolve) => {
        const body = el('div', 'stack');
        openSheet('下载选项', body);

        const settings = { download_lyrics: true, overwrite: false };
        const list = el('div', 'list');

        const toggleRow = (label, key, hint) => {
            const row = el('div', 'list-row');
            const text = el('div', 'grow');
            text.appendChild(el('div', '', label));
            if (hint) text.appendChild(el('div', 'small text-3', hint));
            row.appendChild(text);
            const toggle = el('button', 'switch');
            toggle.type = 'button';
            toggle.classList.toggle('is-on', settings[key]);
            toggle.setAttribute('aria-checked', settings[key] ? 'true' : 'false');
            toggle.setAttribute('aria-label', label);
            toggle.addEventListener('click', () => {
                settings[key] = !settings[key];
                toggle.classList.toggle('is-on', settings[key]);
                toggle.setAttribute('aria-checked', settings[key] ? 'true' : 'false');
            });
            row.appendChild(toggle);
            list.appendChild(row);
        };

        toggleRow('下载歌词', 'download_lyrics', '把歌词一起存下来');
        toggleRow('覆盖已有文件', 'overwrite', '同名文件直接盖掉，不盖就跳过');
        body.appendChild(list);

        const actions = el('div', 'chiprow');
        const go = el('button', 'btn btn-primary', '开始下载');
        go.type = 'button';
        go.addEventListener('click', () => {
            closeSheet();
            resolve(settings);
        });
        const cancel = el('button', 'btn btn-ghost', '取消');
        cancel.type = 'button';
        cancel.addEventListener('click', () => {
            closeSheet();
            resolve(null);
        });
        actions.appendChild(go);
        actions.appendChild(cancel);
        body.appendChild(actions);
    });
}

/* ------------------------------------------------------------ track menu */
/**
 * The artists of a track, each one opening that artist's page.
 *
 * The ids come from the bridge as ``artist_list``.  A track without them --
 * a local file -- falls back to plain text.
 */
function renderArtistLinks(host, track) {
    clear(host);
    const list = (track && track.artist_list) || [];
    const usable = list.filter((item) => item && item.id && item.name);
    if (!usable.length) {
        host.appendChild(el('span', '', (track && track.artists) || ''));
        return;
    }
    usable.forEach((item, index) => {
        if (index) host.appendChild(el('span', '', ', '));
        const button = el('button', 'artist-link', item.name);
        button.type = 'button';
        button.addEventListener('click', () => {
            closeSheet();
            openDetail('artist', { id: item.id, name: item.name });
        });
        host.appendChild(button);
    });
}

/** The details of one track: everything the bridge knows about it. */
function openTrackDetails(track) {
    const body = el('div', 'stack');
    const list = el('div', 'list');

    const addRow = (label, value, onClick) => {
        const row = el(onClick ? 'button' : 'div', 'list-row');
        if (onClick) row.type = 'button';
        row.appendChild(el('span', 'grow', label));
        if (value) row.appendChild(el('span', 'value', value));
        if (onClick) {
            row.appendChild(el('span', 'chev', '›'));
            row.addEventListener('click', onClick);
        }
        list.appendChild(row);
    };

    addRow('歌曲', track.name || '未知歌曲');
    const artistRow = el('div', 'list-row');
    artistRow.appendChild(el('span', 'grow', '歌手'));
    const artistNames = el('span', 'value artist-names');
    renderArtistLinks(artistNames, track);
    artistRow.appendChild(artistNames);
    list.appendChild(artistRow);
    addRow('专辑', track.album || '');
    /* The bridge reports the length in milliseconds (the field the API
       sends), while formatTime works in seconds. */
    addRow('时长', track.duration ? formatTime(track.duration / 1000) : '');
    addRow('音质', track.vip ? 'VIP / 无损' : '标准');
    body.appendChild(list);

    const hint = el('div', 'small text-3',
        '歌手的名字可以点进去看这个歌手的页面和热门歌曲。');
    body.appendChild(hint);
    openSheet('歌曲信息', body);
}

function openTrackMenu(track, list, index) {
    const body = el('div', 'list');
    const row = (label, onClick) => {
        const button = el('button', 'list-row');
        button.type = 'button';
        button.appendChild(el('span', 'grow', label));
        button.addEventListener('click', () => { closeSheet(); onClick(); });
        body.appendChild(button);
    };
    row('下一首播放', () => {
        state.queue = list.slice();
        state.index = index;
        advance(1);
    });
    row('下载这首', () => downloadTracks([track]));
    row('歌曲信息', () => openTrackDetails(track));
    row('复制歌曲信息', async () => {
        const text = `${track.name || ''} - ${track.artists || ''}`.trim();
        try {
            await navigator.clipboard.writeText(text);
            showToast('已复制');
        } catch (err) {
            showToast('复制不了');
        }
    });
    openSheet(track.name || '', body);
}

/* -------------------------------------------------------------- backup etc */
async function exportBackup() {
    const result = await api('export_account_backup');
    if (result && result.success) {
        showToast(`已导出：${result.path || ''}`);
    } else if (result && result.cancelled) {
        /* the user closed the dialog */
    } else {
        showToast(errorText(result, '导出失败'));
    }
}

async function copyCookie() {
    const result = await api('copy_login_cookie');
    if (!result || !result.success) { showToast('复制失败'); return; }
    const text = result.cookie || '';
    try {
        await navigator.clipboard.writeText(text);
        showToast('Cookie 已复制');
    } catch (err) {
        openModal({
            title: '登录 Cookie',
            body: el('div', 'small', '剪贴板用不了，手动复制下面的内容：'),
            actions: [{ label: '关闭', class: 'btn-ghost' }],
        });
        const body = $('modal-body');
        const box = el('textarea', 'small', text);
        box.style.width = '100%';
        box.style.height = '120px';
        body.appendChild(box);
    }
}

/* ------------------------------------------------------------------- wire */
function wire() {
    document.querySelectorAll('#tabbar .tab').forEach((button) => {
        button.addEventListener('click', () => go(button.dataset.tab));
    });
    $('btn-back').addEventListener('click', popView);
    $('sheet-backdrop').addEventListener('click', closeSheet);
    $('modal-backdrop').addEventListener('click', closeModal);

    $('search-form').addEventListener('submit', (event) => {
        event.preventDefault();
        runSearch();
    });
    $('btn-search-clear').addEventListener('click', () => {
        $('search-input').value = '';
        clear($('search-results'));
    });
    $('search-input').addEventListener('input', (event) => {
        $('btn-search-clear').hidden = !event.target.value;
    });
    document.querySelectorAll('#search-types .chip').forEach((chip) => {
        chip.addEventListener('click', () => {
            document.querySelectorAll('#search-types .chip').forEach((other) => {
                other.classList.toggle('is-on', other === chip);
            });
            state.searchType = chip.dataset.type;
            if ($('search-input').value.trim()) runSearch();
        });
    });
    document.querySelectorAll('#library-tabs .chip').forEach((chip) => {
        chip.addEventListener('click', () => {
            document.querySelectorAll('#library-tabs .chip').forEach((other) => {
                other.classList.toggle('is-on', other === chip);
            });
            state.libraryTab = chip.dataset.tab;
            loadLibrary();
        });
    });

    $('btn-mini-play').addEventListener('click', togglePlay);
    $('btn-mini-next').addEventListener('click', () => advance(1));
    $('miniplayer').addEventListener('click', (event) => {
        if (event.target.closest('.iconbtn')) return;
        openPlayer();
    });
    $('btn-player-close').addEventListener('click', closePlayer);
    /* Tap the big cover to read the lyrics, tap the lyrics to go back. */
    $('player-cover').addEventListener('click', toggleLyrics);
    $('player-lyrics').addEventListener('click', toggleLyrics);
    $('btn-player-play').addEventListener('click', togglePlay);
    $('btn-player-next').addEventListener('click', () => advance(1));
    $('btn-player-prev').addEventListener('click', () => advance(-1));
    $('btn-player-list').addEventListener('click', () => {
        const body = el('div', 'stack');
        openSheet('正在播放', body);
        renderTracks(body, state.queue, '队列是空的');
    });
    $('btn-player-mode').addEventListener('click', () => showToast('播放模式循环'));
    $('btn-player-more').addEventListener('click', () => {
        const track = currentTrack();
        if (track) openTrackMenu(track, state.queue, state.index);
    });

    $('btn-download-pause').addEventListener('click', async () => {
        await api('pause_download'); loadDownloads();
    });
    $('btn-download-resume').addEventListener('click', async () => {
        await api('resume_download'); loadDownloads();
    });
    $('btn-download-cancel').addEventListener('click', async () => {
        await api('cancel_download'); loadDownloads();
    });
    $('btn-scan-local').addEventListener('click', async () => {
        showToast('正在扫描…');
        await api('scan_local_music', true);
        loadDownloads();
    });
    $('btn-more-playlists').addEventListener('click', () => {
        go('library');
    });

    // The pages scroll inside themselves, so the listener goes on each of
    // them.  A scroll event does not bubble, and the container has
    // overflow: hidden -- watching it would never fire.
    document.querySelectorAll('#views > .view').forEach((view) => {
        view.addEventListener('scroll', () => {
            $('appbar').classList.toggle('is-scrolled', view.scrollTop > 4);
        });
    });
}

/* ------------------------------------------------------------------- boot */
async function waitForApi(timeoutMs = 8000) {
    const has = () => !!(window.pywebview && window.pywebview.api
        && typeof window.pywebview.api.get_settings === 'function');
    if (has()) return window.pywebview.api;
    return new Promise((resolve) => {
        const started = Date.now();
        const finish = () => {
            if (has()) resolve(window.pywebview.api);
            else if (Date.now() - started > timeoutMs) resolve(null);
            else setTimeout(finish, 120);
        };
        window.addEventListener('pywebviewready', finish);
        finish();
    });
}

async function boot() {
    state.api = await waitForApi();
    if (!state.api) {
        showEmpty($('views'), '连不上后台', 'pywebview 桥没有就绪');
        return;
    }
    wire();

    /* The translations come first: every message below is looked up by
     * key, and without the catalogue the user would read "login.
     * code_send_failed" instead of a sentence. */
    try {
        state.translations = (await api('get_translations')) || {};
    } catch (err) {
        state.translations = {};
    }

    const settings = await api('get_settings');
    applyTheme(settings && settings.ui ? settings.ui.theme : 'system');
    const status = await api('get_login_status');
    state.login = status || {};

    go('home');
}

document.addEventListener('DOMContentLoaded', boot);
