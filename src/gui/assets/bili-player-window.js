/**
 * SkycMedia - the Bilibili player window.
 *
 * The video plays in a window of its own, so the main window stays usable
 * while it runs.  This file is the whole of that window: it asks the backend
 * for the streams, the danmaku and the comments, and it draws them.
 *
 * The backend is reached through the same bridge as the main window
 * (``window.pywebview.api``), so every method used here also exists in the
 * main interface.
 */
'use strict';

(function () {
    /** Everything the window needs to know about the video it shows. */
    const state = {
        api: null,
        translations: {},
        mediaBase: '',
        bvid: '',
        page: 1,
        quality: 0,
        streams: null,
        danmaku: [],
        danmakuCursor: 0,
        danmakuOn: true,
        danmakuSize: 18,
        danmakuOpacity: 0.9,
        stat: {},
        liked: false,
        coinGiven: 0,
        comments: [],
        commentPage: 1,
        commentHasMore: false,
        commentLoading: false,
        live: [],
        rows: []
    };

    /** The danmaku that are on the picture at the moment. */
    const LIVE_SECONDS = 8;

    function el(id) {
        return document.getElementById(id);
    }

    /** The translation of a key; the key itself when it is missing. */
    function t(key, params) {
        let text = state.translations[key];
        if (typeof text !== 'string' || !text) text = key;
        if (params) {
            Object.keys(params).forEach((name) => {
                text = text.split('{' + name + '}').join(String(params[name]));
            });
        }
        return text;
    }

    /**
     * The translation of a key, or nothing when the catalogue does not know it.
     *
     * The window paints its labels before the translations arrive.  Writing
     * the key itself would put "bili.coin" on the button, so an unknown key
     * leaves the label empty until the catalogue shows up.
     */
    function label(key, params) {
        const text = state.translations[key];
        if (typeof text !== 'string' || !text) return '';
        if (!params) return text;
        return Object.keys(params).reduce(
            (out, name) => out.split('{' + name + '}').join(String(params[name])),
            text);
    }

    /**
     * Whether the bridge is not just there but *callable*.
     *
     * pywebview creates ``window.pywebview.api`` as an object first and
     * injects the methods into it a moment later.  Polling for the object
     * alone therefore returns a handle whose every call resolves to
     * ``undefined``: the window then has no translations (buttons keep empty
     * labels), no quality list, an empty comment panel and no streams.  That
     * is exactly the "only sound, no picture" window that was reported.
     * The main window has always checked for a real method; this one did not.
     */
    function apiReady() {
        return !!(window.pywebview && window.pywebview.api
            && typeof window.pywebview.api.get_settings === 'function');
    }

    /** Wait for the pywebview bridge; it is injected after the page loads. */
    function waitForApi(deadline) {
        return new Promise((resolve) => {
            const started = Date.now();
            const check = () => {
                if (apiReady()) {
                    resolve(window.pywebview.api);
                    return;
                }
                if (Date.now() - started > (deadline || 20000)) {
                    resolve(null);
                    return;
                }
                setTimeout(check, 60);
            };
            check();
        });
    }

    /** Put the fixed labels of the window in place. */
    function applyStaticText() {
        const title = label('bili.window_title');
        if (title) document.title = title;
        setLabel('bp-quality-label', 'bili.quality');
        setLabel('bp-danmaku-label', 'bili.settings_danmaku');
        setLabel('bp-size-label', 'bili.settings_danmaku_size');
        setLabel('bp-opacity-label', 'bili.settings_danmaku_opacity');
        setLabel('bp-danmaku-send', 'bili.danmaku_send');
        setLabel('bp-comment-send', 'bili.comment_send');
        setPlaceholder('bp-comment-input', 'bili.comment_placeholder');
        setPlaceholder('bp-danmaku-input', 'bili.danmaku_placeholder');
        setLabel('bp-tab-comments', 'bili.comments');
        setLabel('bp-tab-danmaku', 'bili.danmaku');
        setLabel('bp-like', 'bili.like');
        setLabel('bp-coin', 'bili.coin');
        setLabel('bp-favorite', 'bili.favorite_add');
        setLabel('bp-coin-one', 'bili.coin_one');
        setLabel('bp-coin-two', 'bili.coin_two');
        setLabel('bp-fav-title', 'bili.favorite_pick');
        el('bp-close').textContent = '×';
        const closeTitle = label('bili.close_player');
        if (closeTitle) el('bp-close').setAttribute('title', closeTitle);
    }

    /**
     * Write a label into an element.
     *
     * An unknown key leaves the element alone: the window paints itself
     * before the catalogue arrives, and a button that reads "bili.coin" is
     * worse than a button that is still empty.
     */
    function setLabel(id, key) {
        const node = el(id);
        if (!node) return;
        const text = label(key);
        if (!text) return;
        node.textContent = text;
    }

    /** The same for a placeholder. */
    function setPlaceholder(id, key) {
        const node = el(id);
        if (!node) return;
        const text = label(key);
        if (!text) return;
        node.setAttribute('placeholder', text);
    }

    /** The status of the player, in the words of the interface. */
    function setStatus(code) {
        const node = el('bp-status');
        if (!node) return;
        const keys = {
            buffering: 'bili.buffering',
            ready: 'bili.video_ready',
            streaming: 'bili.streaming',
            error: 'bili.video_error',
            audio_only: 'bili.audio_only'
        };
        node.textContent = t(keys[code] || 'bili.buffering');
        node.className = 'bp-status' + (code === 'error' ? ' is-error' : '');
    }

    /** The URL of a Bilibili picture, through the loopback proxy. */
    function imageSrc(url) {
        const raw = String(url || '').trim();
        if (!raw) return '';
        if (!raw.startsWith('http://') && !raw.startsWith('https://')) return raw;
        if (!state.mediaBase) return '';
        return state.mediaBase + '/bili?u=' + encodeURIComponent(raw);
    }

    /* -------------------------------------------------------------- the video */

    /** The quality list of the video that is playing. */
    function renderQualities(streams) {
        const select = el('bp-quality');
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
        const wanted = String(state.quality || 0);
        select.value = offered.indexOf(wanted) >= 0 ? wanted : '0';
    }

    /** Switch to another quality and keep the place in the video. */
    async function changeQuality(value) {
        const videoEl = el('bp-video');
        if (!videoEl || !state.bvid) return;
        const position = videoEl.currentTime || 0;
        const restore = () => {
            try { videoEl.currentTime = position; } catch (err) { /* not ready yet */ }
        };
        videoEl.addEventListener('loadedmetadata', restore, { once: true });
        await loadStreams(Number(value) || 0);
        if (position) restore();
    }

    /** Fetch the streams and hand them to the player. */
    async function loadStreams(quality) {
        const videoEl = el('bp-video');
        if (!videoEl || !state.api) return;
        setStatus('buffering');
        try {
            const result = await state.api.get_bilibili_video_streams(state.bvid, quality);
            if (!result || !result.success) {
                setStatus('error');
                return;
            }
            state.streams = result;
            state.quality = quality;
            el('bp-title').textContent = result.title || state.bvid;
            el('bp-sub').textContent = result.author || '';
            renderQualities(result);
            const played = await window.playBiliVideo(videoEl, result, {
                onStatus: (code) => setStatus(code)
            });
            if (!played) setStatus('error');
        } catch (err) {
            setStatus('error');
        }
    }

    /* -------------------------------------------------------------- the danmaku */

    /** The size and the transparency of the danmaku, as the settings say. */
    function applyDanmakuStyle() {
        const layer = el('bp-danmaku-layer');
        if (!layer) return;
        layer.style.fontSize = (state.danmakuSize || 18) + 'px';
        layer.style.opacity = String(state.danmakuOpacity ?? 0.9);
    }

    /** Take every danmaku off the picture. */
    function clearLiveDanmaku() {
        state.live.forEach((entry) => {
            if (entry.el && entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
        });
        state.live = [];
        state.rows = [];
    }

    /**
     * The row a new danmaku may use.
     *
     * Two danmaku that scroll over the same line at the same time would
     * sit on top of each other, so every row remembers until when it is
     * busy.
     */
    function pickRow(now) {
        for (let index = 0; index < state.rows.length; index += 1) {
            if (state.rows[index] <= now) {
                state.rows[index] = now + LIVE_SECONDS;
                return index;
            }
        }
        state.rows.push(now + LIVE_SECONDS);
        return state.rows.length - 1;
    }

    /** Show the danmaku that belong to the time the video is at. */
    function updateLiveDanmaku(currentTime) {
        const layer = el('bp-danmaku-layer');
        if (!layer) return;
        if (!state.danmakuOn) {
            clearLiveDanmaku();
            return;
        }
        const now = Number(currentTime || 0);
        for (let index = state.live.length - 1; index >= 0; index -= 1) {
            if (state.live[index].start + LIVE_SECONDS < now) {
                const entry = state.live[index];
                if (entry.el && entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
                state.live.splice(index, 1);
            }
        }
        while (state.danmakuCursor < state.danmaku.length) {
            const item = state.danmaku[state.danmakuCursor];
            state.danmakuCursor += 1;
            if (!item) continue;
            if (Number(item.time || 0) > now) continue;
            const node = document.createElement('span');
            node.className = 'bp-danmaku';
            const mode = Number(item.mode || 1);
            const color = Number(item.color || 16777215);
            node.style.color = '#' + ('000000' + color.toString(16)).slice(-6);
            if (mode === 4 || mode === 5) {
                node.classList.add('is-fixed');
                node.style.top = (mode === 4 ? 68 : 8) + '%';
            } else {
                const row = pickRow(now);
                node.style.top = (8 + row * (state.danmakuSize || 18) * 1.35) + 'px';
                node.style.setProperty('--bp-start', (layer.clientWidth || 640) + 'px');
            }
            node.textContent = item.text || '';
            layer.appendChild(node);
            state.live.push({ el: node, start: now });
        }
    }

    /** The whole danmaku list, with the second each one belongs to. */
    function renderDanmakuList() {
        const list = el('bp-danmaku-list');
        if (!list) return;
        list.innerHTML = '';
        if (!state.danmaku.length) {
            const empty = document.createElement('div');
            empty.className = 'bp-empty';
            empty.textContent = t('bili.danmaku_empty');
            list.appendChild(empty);
            return;
        }
        state.danmaku.forEach((item) => {
            if (!item) return;
            const row = document.createElement('div');
            row.className = 'bp-danmaku-row';
            const time = document.createElement('span');
            time.className = 'bp-danmaku-time';
            time.textContent = fmtTime(item.time || 0);
            row.appendChild(time);
            const text = document.createElement('span');
            text.className = 'bp-danmaku-text';
            text.textContent = item.text || '';
            row.appendChild(text);
            // Clicking a danmaku jumps to the second it belongs to.
            row.addEventListener('click', () => seekTo(Number(item.time || 0)));
            list.appendChild(row);
        });
    }

    /** Seconds as mm:ss. */
    function fmtTime(seconds) {
        const total = Math.max(0, Math.floor(Number(seconds || 0)));
        const minutes = Math.floor(total / 60);
        const rest = total % 60;
        return minutes + ':' + (rest < 10 ? '0' : '') + rest;
    }

    /** Jump to a second of the video. */
    function seekTo(seconds) {
        const videoEl = el('bp-video');
        if (!videoEl) return;
        try {
            videoEl.currentTime = Number(seconds || 0);
        } catch (err) { /* the video is not ready yet */ }
    }

    /** Read the danmaku of the video. */
    async function loadDanmaku() {
        if (!state.api || !state.bvid) return;
        try {
            const result = await state.api.get_bilibili_danmaku(state.bvid, 0);
            if (!result || !result.success) return;
            state.danmaku = result.items || [];
            state.danmakuCursor = 0;
            // The cursor sits at the second the video is at, otherwise a
            // burst of old danmaku lands on the picture when it starts.
            const videoEl = el('bp-video');
            const now = Number(videoEl && videoEl.currentTime || 0);
            while (state.danmakuCursor < state.danmaku.length) {
                if (Number(state.danmaku[state.danmakuCursor].time || 0) >= now) break;
                state.danmakuCursor += 1;
            }
            renderDanmakuList();
        } catch (err) {
            /* no danmaku */
        }
    }

    /** Send one danmaku. */
    async function sendDanmaku() {
        const input = el('bp-danmaku-input');
        const text = (input && input.value ? input.value : '').trim();
        if (!text || !state.api) return;
        const button = el('bp-danmaku-send');
        if (button) button.disabled = true;
        try {
            const result = await state.api.bili_send_danmaku(state.bvid, text, 0, 16777215, 25, 1);
            if (!result || !result.success) {
                el('bp-status').textContent = t('bili.danmaku_failed');
                return;
            }
            if (input) input.value = '';
            await loadDanmaku();
        } catch (err) {
            el('bp-status').textContent = t('bili.danmaku_failed');
        } finally {
            if (button) button.disabled = false;
        }
    }

    /* -------------------------------------------------------------- the comments */

    /** One comment. */
    function commentElement(comment) {
        const row = document.createElement('div');
        row.className = 'bp-comment';
        const head = document.createElement('div');
        head.className = 'bp-comment-head';
        if (comment.face) {
            const face = document.createElement('img');
            face.className = 'bp-comment-face';
            face.alt = '';
            face.loading = 'lazy';
            face.src = imageSrc(comment.face);
            face.addEventListener('error', () => { face.remove(); });
            head.appendChild(face);
        }
        const name = document.createElement('span');
        name.className = 'bp-comment-name';
        name.textContent = comment.uname || '';
        head.appendChild(name);
        if (comment.level) {
            const level = document.createElement('span');
            level.className = 'bp-comment-level';
            level.textContent = t('bili.level', { level: comment.level });
            head.appendChild(level);
        }
        row.appendChild(head);
        const body = document.createElement('div');
        body.className = 'bp-comment-body';
        body.textContent = comment.content || '';
        row.appendChild(body);
        const meta = document.createElement('div');
        meta.className = 'bp-comment-meta';
        if (comment.location) {
            const place = document.createElement('span');
            place.textContent = comment.location;
            meta.appendChild(place);
        }
        if (comment.like) {
            const like = document.createElement('span');
            like.textContent = t('bili.comment_like') + ' ' + comment.like;
            meta.appendChild(like);
        }
        row.appendChild(meta);
        return row;
    }

    /** Draw the comments that are read. */
    function renderComments() {
        const list = el('bp-comment-list');
        if (!list) return;
        list.innerHTML = '';
        if (!state.comments.length) {
            const empty = document.createElement('div');
            empty.className = 'bp-empty';
            empty.textContent = t('bili.comment_empty');
            list.appendChild(empty);
        } else {
            state.comments.forEach((comment) => {
                if (comment) list.appendChild(commentElement(comment));
            });
        }
        const more = el('bp-comment-more');
        if (more) {
            more.hidden = !state.commentHasMore || state.commentLoading;
            more.disabled = state.commentLoading;
            more.textContent = state.commentLoading
                ? t('common.loading') : t('bili.load_more');
        }
    }

    /** Read the comments of the video. */
    async function loadComments(reset) {
        if (!state.api || !state.bvid) return;
        if (state.commentLoading) return;
        state.commentLoading = true;
        renderComments();
        try {
            const page = reset ? 1 : state.commentPage + 1;
            const result = await state.api.get_bilibili_comments(state.bvid, page, 'hot');
            if (!result || !result.success) return;
            if (reset) state.comments = [];
            (result.comments || []).forEach((comment) => {
                if (comment) state.comments.push(comment);
            });
            state.commentPage = page;
            state.commentHasMore = !!result.has_more;
        } catch (err) {
            /* no comments */
        } finally {
            state.commentLoading = false;
            renderComments();
        }
    }

    /** Write a comment. */
    async function sendComment() {
        const input = el('bp-comment-input');
        const text = (input && input.value ? input.value : '').trim();
        if (!text || !state.api) return;
        const button = el('bp-comment-send');
        if (button) button.disabled = true;
        try {
            const result = await state.api.bili_post_comment(state.bvid, text, 0);
            if (!result || !result.success) {
                el('bp-status').textContent = t('bili.comment_failed');
                return;
            }
            if (input) input.value = '';
            await loadComments(true);
        } catch (err) {
            el('bp-status').textContent = t('bili.comment_failed');
        } finally {
            if (button) button.disabled = false;
        }
    }

    /* --------------------------------------------------------- the interactions */

    /** 12345 becomes 1.2万, the way the site writes it. */
    function formatCount(value) {
        const count = Number(value || 0);
        if (count >= 100000000) return (count / 100000000).toFixed(1) + '亿';
        if (count >= 10000) return (count / 10000).toFixed(1) + '万';
        return String(count);
    }

    /** The counters of the video. */
    function renderStats() {
        const row = el('bp-stats');
        if (!row) return;
        const stat = state.stat || {};
        row.innerHTML = '';
        const items = [
            { key: 'bili.plays', value: stat.view },
            { key: 'bili.stat_danmaku', value: stat.danmaku },
            { key: 'bili.stat_reply', value: stat.reply },
            { key: 'bili.stat_like', value: stat.like }
        ];
        items.forEach((item) => {
            const node = document.createElement('span');
            node.textContent = t(item.key, { count: formatCount(item.value) });
            row.appendChild(node);
        });
        const like = el('bp-like');
        if (like) like.textContent = state.liked ? t('bili.liked') : t('bili.like');
    }

    /** Read the counters of the video. */
    async function loadStat() {
        if (!state.api || !state.bvid) return;
        try {
            const result = await state.api.get_bilibili_video_stat(state.bvid);
            if (!result || !result.success) return;
            state.stat = result.stat || {};
            renderStats();
        } catch (err) {
            /* the counters are not important */
        }
    }

    /** Like the video, or take the like back. */
    async function toggleLike() {
        if (!state.api || !state.bvid) return;
        const next = !state.liked;
        const button = el('bp-like');
        if (button) button.disabled = true;
        try {
            const result = await state.api.bili_like_video(state.bvid, next);
            if (!result || !result.success) {
                el('bp-status').textContent = t('bili.like_failed');
                return;
            }
            state.liked = next;
            renderStats();
        } catch (err) {
            el('bp-status').textContent = t('bili.like_failed');
        } finally {
            if (button) button.disabled = false;
        }
    }

    /** The coin panel: the balance, what is already given, and the buttons. */
    async function openCoinPanel() {
        const panel = el('bp-coin-panel');
        if (!panel) return;
        panel.hidden = false;
        const balance = el('bp-coin-balance');
        if (balance) balance.textContent = t('bili.coin_balance', { count: '' });
        try {
            const result = await state.api.get_bilibili_coin_balance();
            if (result && result.success && balance) {
                balance.textContent = t('bili.coin_balance', {
                    count: Number(result.balance || result.coin || 0)
                });
            }
        } catch (err) {
            /* the balance stays empty */
        }
        await refreshCoinState();
    }

    /**
     * How many coins this account already gave the video.
     *
     * B站 allows two coins per submission; throwing more is refused by the
     * server.  The buttons follow the number that is left instead of letting
     * the user find out through an error message.
     */
    async function refreshCoinState() {
        if (!state.api || !state.bvid) return;
        let given = Number(state.coinGiven || 0);
        try {
            const result = await state.api.get_bilibili_coins_given(state.bvid);
            if (result && result.success) given = Number(result.given || 0);
        } catch (err) {
            /* keep the last known number */
        }
        state.coinGiven = given;
        const left = Math.max(0, 2 - given);
        const one = el('bp-coin-one');
        const two = el('bp-coin-two');
        if (one) one.disabled = left < 1;
        if (two) two.disabled = left < 2;
        const panel = el('bp-coin-panel');
        const tip = el('bp-coin-hint');
        if (tip) {
            tip.textContent = left > 0
                ? t('bili.coin_left', { count: left })
                : t('bili.coin_maxed');
        }
        if (panel && left === 0) {
            // Nothing left to throw; the panel stays with the explanation.
            if (one) one.hidden = true;
            if (two) two.hidden = true;
        }
    }

    /** Throw coins at the video. */
    async function throwCoins(count) {
        if (!state.api || !state.bvid) return;
        const panel = el('bp-coin-panel');
        const left = Math.max(0, 2 - Number(state.coinGiven || 0));
        if (count > left) {
            // Asking for more than the server allows is the one thing that must
            // not even be attempted: it answers with an error that reads like a
            // failure of the app.
            el('bp-status').textContent = t('bili.coin_maxed');
            return;
        }
        try {
            const result = await state.api.bili_coin_video(state.bvid, count, false);
            if (!result || !result.success) {
                el('bp-status').textContent = t('bili.coin_failed');
                return;
            }
            if (panel) panel.hidden = true;
            // The balance and the counters both changed: redraw them instead of
            // leaving the old numbers on screen.
            await loadStat();
            await refreshCoinState();
        } catch (err) {
            el('bp-status').textContent = t('bili.coin_failed');
        }
    }

    /** The favourite folders, so the video can be put into one. */
    async function openFavoritePanel() {
        const panel = el('bp-fav-panel');
        const list = el('bp-fav-list');
        if (!panel || !list) return;
        panel.hidden = false;
        list.innerHTML = '';
        const loading = document.createElement('div');
        loading.className = 'bp-empty';
        loading.textContent = t('common.loading');
        list.appendChild(loading);
        try {
            const result = await state.api.get_bilibili_favorites();
            list.innerHTML = '';
            const folders = (result && result.folders) || [];
            if (!folders.length) {
                const empty = document.createElement('div');
                empty.className = 'bp-empty';
                empty.textContent = t('bili.no_folders');
                list.appendChild(empty);
                return;
            }
            folders.forEach((folder) => {
                if (!folder) return;
                const button = document.createElement('button');
                button.className = 'bp-fav-item';
                button.type = 'button';
                button.textContent = folder.title || '';
                button.addEventListener('click', () => addToFolder(folder.id || ''));
                list.appendChild(button);
            });
        } catch (err) {
            list.innerHTML = '';
            const empty = document.createElement('div');
            empty.className = 'bp-empty';
            empty.textContent = t('bili.no_folders');
            list.appendChild(empty);
        }
    }

    /** Put the video into one favourite folder. */
    async function addToFolder(folderId) {
        if (!state.api || !state.bvid) return;
        const panel = el('bp-fav-panel');
        try {
            const result = await state.api.bili_favorite_video(state.bvid, folderId, false);
            if (!result || !result.success) {
                el('bp-status').textContent = t('bili.favorite_failed');
                return;
            }
            if (panel) panel.hidden = true;
            await loadStat();
        } catch (err) {
            el('bp-status').textContent = t('bili.favorite_failed');
        }
    }

    /* ------------------------------------------------------------------ the tabs */
    /** Show one of the two lists of the sidebar. */
    function switchTab(name) {
        const isComments = name !== 'danmaku';
        el('bp-tab-comments').className = 'bp-tab' + (isComments ? ' is-active' : '');
        el('bp-tab-danmaku').className = 'bp-tab' + (isComments ? '' : ' is-active');
        el('bp-panel-comments').hidden = !isComments;
        el('bp-panel-danmaku').hidden = isComments;
    }

    /* ------------------------------------------------------------------- boot */

    /** The video id and the page, taken from the URL of the window. */
    function readQuery() {
        const params = new URLSearchParams(window.location.search || '');
        return {
            bvid: (params.get('bvid') || '').trim(),
            page: parseInt(params.get('page') || '1', 10) || 1
        };
    }

    /** The settings of the Bilibili part. */
    function applySettings(settings) {
        const bili = (settings && settings.bili) || {};
        state.danmakuOn = bili.danmaku_enabled !== false;
        state.danmakuSize = Number(bili.danmaku_font_size || 18);
        state.danmakuOpacity = Number(bili.danmaku_opacity ?? 0.9);
        state.quality = Number(bili.quality || 0);
        const on = el('bp-danmaku-on');
        if (on) on.checked = state.danmakuOn;
        const size = el('bp-danmaku-size');
        if (size) size.value = String(state.danmakuSize);
        const opacity = el('bp-danmaku-opacity');
        if (opacity) opacity.value = String(Math.round(state.danmakuOpacity * 100));
        const sizeValue = el('bp-danmaku-size-value');
        if (sizeValue) sizeValue.textContent = state.danmakuSize + 'px';
        const opacityValue = el('bp-danmaku-opacity-value');
        if (opacityValue) opacityValue.textContent = Math.round(state.danmakuOpacity * 100) + '%';
        applyDanmakuStyle();
    }

    /** The window cannot work without the bridge; say so and stop. */
    function showFatal(message) {
        const fatal = el('bp-fatal');
        if (!fatal) return;
        fatal.hidden = false;
        // An empty text means the translations never arrived either -- which is
        // the same failure.  Say something rather than leave a blank window.
        fatal.textContent = message || 'SkycMedia 窗口没能连上后台，请关掉这个窗口再打开一次。';
    }

    async function boot() {
        applyStaticText();
        const api = await waitForApi();
        if (!api) {
            showFatal(t('bili.window_only_desktop'));
            return;
        }
        state.api = api;
        try {
            state.translations = await api.get_translations() || {};
        } catch (err) {
            state.translations = {};
        }
        applyStaticText();
        try {
            const info = await api.get_app_info();
            state.mediaBase = (info && info.media_base_url) || '';
        } catch (err) {
            state.mediaBase = '';
        }
        try {
            applySettings(await api.get_settings());
        } catch (err) {
            applySettings(null);
        }

        const query = readQuery();
        state.bvid = query.bvid;
        state.page = query.page;
        if (!state.bvid) {
            showFatal(t('bili.window_only_desktop'));
            return;
        }

        bindEvents();
        renderComments();
        renderDanmakuList();
        await loadStreams(state.quality);
        // The danmaku and the comments do not have to wait for the video.
        loadDanmaku();
        loadComments(true);
        loadStat();
    }

    /* -------------------------------------------------------------- 播放数据 */

    /**
     * Tell B站 what is playing, once every 15 seconds.
     *
     * This is what fills the Bilibili watch history and what the recommendation
     * feed learns from.  It writes to the account, so the backend refuses it
     * unless the user turned "report playback" on in the settings -- the player
     * can call it blindly.
     */
    const playbackReport = { timer: null, session: '', startTs: 0 };

    function reportOnce(playType) {
        const videoEl = el('bp-video');
        if (!state.api || !state.bvid || !videoEl) return;
        if (typeof state.api.bili_report_playback !== 'function') return;
        const played = Math.max(0, Math.floor(Number(videoEl.currentTime) || 0));
        const duration = Math.floor(Number(videoEl.duration)
            || Number(state.streams && state.streams.duration) || 0);
        try {
            state.api.bili_report_playback(
                state.bvid,
                Number((state.streams && state.streams.cid) || 0),
                played, duration,
                Number(state.quality || 0), playbackReport.startTs,
                playbackReport.session, Number(playType || 0)
            ).catch(() => {});
        } catch (err) {
            /* the report must never break the picture */
        }
    }

    function startPlaybackReport() {
        if (!state.api || !state.bvid) return;
        if (!playbackReport.session) {
            playbackReport.session = Math.random().toString(16).slice(2)
                + Date.now().toString(16);
            playbackReport.startTs = Math.floor(Date.now() / 1000);
        }
        reportOnce(1);
        if (playbackReport.timer) return;
        playbackReport.timer = setInterval(() => reportOnce(0), 15000);
    }

    function stopPlaybackReport(playType) {
        if (playbackReport.timer) {
            clearInterval(playbackReport.timer);
            playbackReport.timer = null;
        }
        if (playType) reportOnce(playType);
        if (playType === 4) playbackReport.session = '';
    }

    function bindEvents() {
        const videoEl = el('bp-video');
        if (videoEl) {
            videoEl.addEventListener('timeupdate', () => {
                updateLiveDanmaku(videoEl.currentTime);
            });
            videoEl.addEventListener('playing', () => startPlaybackReport());
            videoEl.addEventListener('pause', () => stopPlaybackReport(2));
            videoEl.addEventListener('ended', () => stopPlaybackReport(4));
            videoEl.addEventListener('seeked', () => {
                clearLiveDanmaku();
                const now = Number(videoEl.currentTime || 0);
                state.danmakuCursor = 0;
                while (state.danmakuCursor < state.danmaku.length) {
                    if (Number(state.danmaku[state.danmakuCursor].time || 0) >= now) break;
                    state.danmakuCursor += 1;
                }
            });
        }

        const quality = el('bp-quality');
        if (quality) {
            quality.addEventListener('change', () => changeQuality(quality.value));
        }
        const danmakuOn = el('bp-danmaku-on');
        if (danmakuOn) {
            danmakuOn.addEventListener('change', () => {
                state.danmakuOn = danmakuOn.checked;
                if (!state.danmakuOn) clearLiveDanmaku();
                else saveDanmakuSettings();
            });
        }
        const size = el('bp-danmaku-size');
        if (size) {
            size.addEventListener('input', () => {
                state.danmakuSize = Number(size.value) || 18;
                const value = el('bp-danmaku-size-value');
                if (value) value.textContent = state.danmakuSize + 'px';
                applyDanmakuStyle();
            });
            size.addEventListener('change', () => saveDanmakuSettings());
        }
        const opacity = el('bp-danmaku-opacity');
        if (opacity) {
            opacity.addEventListener('input', () => {
                state.danmakuOpacity = Number(opacity.value) / 100;
                const value = el('bp-danmaku-opacity-value');
                if (value) value.textContent = Math.round(state.danmakuOpacity * 100) + '%';
                applyDanmakuStyle();
            });
            opacity.addEventListener('change', () => saveDanmakuSettings());
        }
        const like = el('bp-like');
        if (like) {
            like.addEventListener('click', toggleLike);
        }
        const coin = el('bp-coin');
        if (coin) {
            coin.addEventListener('click', openCoinPanel);
        }
        const coinOne = el('bp-coin-one');
        if (coinOne) {
            coinOne.addEventListener('click', () => throwCoins(1));
        }
        const coinTwo = el('bp-coin-two');
        if (coinTwo) {
            coinTwo.addEventListener('click', () => throwCoins(2));
        }
        const favorite = el('bp-favorite');
        if (favorite) {
            favorite.addEventListener('click', openFavoritePanel);
        }
        const sendDanmakuButton = el('bp-danmaku-send');
        if (sendDanmakuButton) {
            sendDanmakuButton.addEventListener('click', sendDanmaku);
        }
        const danmakuInput = el('bp-danmaku-input');
        if (danmakuInput) {
            danmakuInput.addEventListener('keydown', (event) => {
                if (event.key === 'Enter') sendDanmaku();
            });
        }
        const sendCommentButton = el('bp-comment-send');
        if (sendCommentButton) {
            sendCommentButton.addEventListener('click', sendComment);
        }
        const commentInput = el('bp-comment-input');
        if (commentInput) {
            commentInput.addEventListener('keydown', (event) => {
                if (event.key === 'Enter') sendComment();
            });
        }
        const more = el('bp-comment-more');
        if (more) {
            more.addEventListener('click', () => loadComments(false));
        }
        const tabComments = el('bp-tab-comments');
        if (tabComments) {
            tabComments.addEventListener('click', () => switchTab('comments'));
        }
        const tabDanmaku = el('bp-tab-danmaku');
        if (tabDanmaku) {
            tabDanmaku.addEventListener('click', () => switchTab('danmaku'));
        }
        const close = el('bp-close');
        if (close) {
            close.addEventListener('click', () => {
                if (window.stopBiliVideo) window.stopBiliVideo();
                window.close();
            });
        }
        window.addEventListener('beforeunload', () => {
            if (window.stopBiliVideo) window.stopBiliVideo();
        });
    }

    /** Keep the danmaku settings of the window in the settings of the app. */
    async function saveDanmakuSettings() {
        if (!state.api) return;
        try {
            const settings = await state.api.get_settings() || {};
            const bili = Object.assign({}, settings.bili || {}, {
                danmaku_enabled: state.danmakuOn,
                danmaku_font_size: state.danmakuSize,
                danmaku_opacity: state.danmakuOpacity
            });
            await state.api.update_settings('bili', bili);
        } catch (err) {
            /* the settings stay as they are */
        }
    }

    document.addEventListener('DOMContentLoaded', boot);
})();
