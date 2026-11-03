/**
 * SkycMedia - the local video window.
 *
 * A window of its own, opened by the main interface with
 * ``webview.create_window``.  It has **no bridge**: it cannot ask the backend
 * to scan a folder or to read a subtitle, so everything it plays comes either
 * from the URL it was opened with or from a file the user picks here.
 *
 * The two query parameters the caller passes are:
 *
 *   video       an absolute path or an http(s) URL of the video to play
 *   subtitle    a path or an http(s) URL of a WebVTT/SRT subtitle, or
 *   subtitleText  the WebVTT text itself (Percent-encoded); it is turned into a
 *               blob URL, which is how the backend's ``subtitle_for()`` answer
 *               can travel without a server
 *
 * ``file`` / ``files`` (a "|" or JSON separated list of paths) prefill the
 * list on the left.
 *
 * Why the file pickers: a webview that renders the page over http refuses to
 * load ``file://`` media (the two are different origins), so a path handed in
 * through the URL can fail through no fault of the caller.  The pickers turn
 * the same file into a blob URL, which always plays.
 */
'use strict';

(function () {
    /** Seconds the arrow keys jump. */
    const SEEK_STEP = 10;

    const state = {
        items: [],
        active: '',
        search: '',
        /** The object URLs this window made, so they can be released. */
        blobUrls: []
    };

    function el(id) {
        return document.getElementById(id);
    }

    /* --------------------------------------------------------------- helpers */

    /** ``12345678`` -> ``11.8 MB``. */
    function formatSize(bytes) {
        const size = Number(bytes || 0);
        if (!size) return '';
        if (size >= 1024 * 1024 * 1024) return (size / 1024 / 1024 / 1024).toFixed(1) + ' GB';
        if (size >= 1024 * 1024) return (size / 1024 / 1024).toFixed(1) + ' MB';
        if (size >= 1024) return Math.round(size / 1024) + ' KB';
        return size + ' B';
    }

    /** Seconds as ``h:mm:ss`` (or ``m:ss`` when it is short). */
    function formatTime(seconds) {
        const total = Math.max(0, Math.floor(Number(seconds) || 0));
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const rest = total % 60;
        const tail = (rest < 10 ? '0' : '') + rest;
        if (hours) return hours + ':' + (minutes < 10 ? '0' : '') + minutes + ':' + tail;
        return minutes + ':' + tail;
    }

    /** The name of a path, for the list and the title. */
    function baseName(path) {
        const text = String(path || '');
        const cut = Math.max(text.lastIndexOf('/'), text.lastIndexOf('\\'));
        return cut >= 0 ? text.slice(cut + 1) : text;
    }

    /**
     * An absolute path as a ``file://`` URL.
     *
     * Only a fallback: a page served over http cannot read ``file://``, which
     * is why the window also has the pickers.  The URL is still worth trying:
     * when the window itself was opened from disk, it works.
     */
    function fileUrl(path) {
        const raw = String(path || '').replace(/\\/g, '/');
        if (!raw) return '';
        if (/^(https?|blob|data):/i.test(raw)) return raw;
        if (/^file:/i.test(raw)) return raw;
        const leading = raw.startsWith('/') ? '' : '/';
        return 'file://' + leading + encodeURI(raw).replace(/#/g, '%23');
    }

    /** Whether a path is a plain local path (not a URL). */
    function isLocalPath(value) {
        const text = String(value || '');
        return !!text && !/^(https?|blob|data|file):/i.test(text);
    }

    /** Remember an object URL so it can be released when it is replaced. */
    function trackBlobUrl(url) {
        if (url && url.startsWith('blob:')) state.blobUrls.push(url);
        return url;
    }

    function releaseBlobUrls() {
        state.blobUrls.forEach((url) => {
            try { URL.revokeObjectURL(url); } catch (err) { /* already gone */ }
        });
        state.blobUrls = [];
    }

    /* ------------------------------------------------------------- the query */

    /** The parameters the window was opened with. */
    function readQuery() {
        const params = new URLSearchParams(window.location.search || '');
        return {
            video: (params.get('video') || '').trim(),
            subtitle: (params.get('subtitle') || '').trim(),
            subtitleText: params.get('subtitleText') || '',
            title: (params.get('title') || '').trim(),
            file: params.getAll('file').map((value) => String(value || '').trim()).filter(Boolean),
            files: (params.get('files') || '').trim()
        };
    }

    /** The extra paths of the ``files`` parameter: JSON or "a|b|c". */
    function parseFileList(raw) {
        if (!raw) return [];
        if (raw.startsWith('[')) {
            try {
                const parsed = JSON.parse(raw);
                if (Array.isArray(parsed)) {
                    return parsed
                        .map((entry) => (typeof entry === 'string' ? entry : entry && entry.path))
                        .filter(Boolean)
                        .map(String);
                }
            } catch (err) {
                /* not JSON after all: fall through to the plain split */
            }
        }
        return raw.split('|').map((entry) => entry.trim()).filter(Boolean);
    }

    /* -------------------------------------------------------------- the list */

    /** Add one entry; the same path is only listed once. */
    function addItem(item) {
        if (!item || !item.url) return null;
        const key = item.key || item.url;
        const found = state.items.find((entry) => entry.key === key);
        if (found) {
            if (!found.url) found.url = item.url;
            return found;
        }
        const record = {
            key: key,
            name: item.name || baseName(item.path) || '未命名',
            path: item.path || '',
            url: item.url,
            size: Number(item.size || 0),
            duration: Number(item.duration || 0),
            subtitleUrl: item.subtitleUrl || ''
        };
        state.items.push(record);
        return record;
    }

    /** The entries the search box lets through. */
    function visibleItems() {
        const needle = state.search.trim().toLowerCase();
        if (!needle) return state.items;
        return state.items.filter((item) => item.name.toLowerCase().indexOf(needle) >= 0);
    }

    /** Redraw the list on the left. */
    function renderList() {
        const list = el('lv-list');
        const empty = el('lv-empty');
        if (!list || !empty) return;
        list.innerHTML = '';
        const items = visibleItems();
        empty.hidden = items.length > 0;
        if (!items.length) {
            empty.textContent = state.items.length
                ? '没有匹配的文件。'
                : '还没有视频文件。用上面的「文件夹」或「文件」按钮添加，或者由主窗口传入。';
        }
        items.forEach((item) => {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'lv-item' + (item.key === state.active ? ' is-active' : '');
            row.dataset.key = item.key;

            const name = document.createElement('span');
            name.className = 'lv-item-name';
            name.textContent = item.name;
            name.title = item.path || item.url;
            row.appendChild(name);

            const bits = [];
            if (item.size) bits.push(formatSize(item.size));
            if (item.duration) bits.push(formatTime(item.duration));
            if (item.subtitleUrl) bits.push('有字幕');
            if (bits.length) {
                const meta = document.createElement('span');
                meta.className = 'lv-item-meta';
                meta.textContent = bits.join(' · ');
                row.appendChild(meta);
            }

            row.addEventListener('click', () => playItem(item));
            list.appendChild(row);
        });
        const count = el('lv-sub');
        if (count) {
            const total = state.items.length;
            count.textContent = total ? total + ' 个视频' : '';
        }
    }

    /* ------------------------------------------------------------ the player */

    /** Take every subtitle track off the element. */
    function clearTracks() {
        const videoEl = el('lv-video');
        if (!videoEl) return;
        const tracks = videoEl.querySelectorAll('track');
        tracks.forEach((track) => track.remove());
    }

    /**
     * Attach one WebVTT source as a subtitle track.
     *
     * The element only takes WebVTT; the backend already converts SRT and ASS,
     * so this window never parses a subtitle file itself.
     */
    function attachTrack(src) {
        const videoEl = el('lv-video');
        if (!videoEl || !src) return;
        clearTracks();
        const track = document.createElement('track');
        track.kind = 'subtitles';
        track.label = '字幕';
        track.srclang = 'zh';
        track.src = src;
        track.default = true;
        videoEl.appendChild(track);
        // A track that was just added is disabled until the mode is set; the
        // cues would never appear on screen otherwise.
        const apply = () => {
            const list = videoEl.textTracks;
            for (let index = 0; index < list.length; index += 1) {
                try { list[index].mode = 'showing'; } catch (err) { /* not ready */ }
            }
        };
        track.addEventListener('load', apply);
        setTimeout(apply, 0);
    }

    /** Show or hide the notice over the picture. */
    function showHint(text) {
        const hint = el('lv-hint');
        if (!hint) return;
        if (!text) {
            hint.hidden = true;
            hint.textContent = '';
            return;
        }
        hint.textContent = text;
        hint.hidden = false;
    }

    /** The line under the picture: what is playing, and why it may not. */
    function setStatus(text) {
        const status = el('lv-status');
        if (status) status.textContent = text || '';
    }

    /** Play one entry of the list. */
    function playItem(item) {
        const videoEl = el('lv-video');
        if (!videoEl || !item) return;
        state.active = item.key;
        showHint('');
        const title = el('lv-title');
        if (title) title.textContent = item.name;
        videoEl.src = item.url;
        attachSubtitleSource(item.subtitleUrl, item.key);
        renderList();
        setStatus('正在打开 ' + item.name);
        const started = videoEl.play();
        if (started && typeof started.catch === 'function') {
            // Autoplay is refused without a gesture; the user can press space
            // or the play button, so this is not an error worth a popup.
            started.catch(() => setStatus('已载入，按空格或点播放开始。'));
        }
    }

    /* -------------------------------------------------------------- subtitles */

    /** A WebVTT timestamp (``00:00:01.250``) from a number of seconds. */
    function vttTime(seconds) {
        const total = Math.max(0, Number(seconds) || 0);
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const rest = total % 60;
        return (hours < 10 ? '0' : '') + hours + ':'
            + (minutes < 10 ? '0' : '') + minutes + ':'
            + (rest < 10 ? '0' : '') + rest.toFixed(3);
    }

    /** ``00:00:01,250`` / ``0:00:01.25`` -> seconds. */
    function parseTimestamp(value) {
        const match = String(value || '').match(/(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?/);
        if (!match) return null;
        const hours = Number(match[1] || 0);
        const minutes = Number(match[2] || 0);
        const seconds = Number(match[3] || 0);
        const fraction = (match[4] || '').padEnd(3, '0');
        return hours * 3600 + minutes * 60 + seconds + (fraction ? Number(fraction) / 1000 : 0);
    }

    /** An SRT file as WebVTT; the cue ids and the comma decimals are enough. */
    function srtToVtt(text) {
        const body = String(text || '').replace(/\r\n?/g, '\n')
            .replace(/(\d{2}):(\d{2}):(\d{2}),(\d{3})/g, '$1:$2:$3.$4');
        return 'WEBVTT\n\n' + body.trim() + '\n';
    }

    /** An ASS file as WebVTT: only the timing and the text survive. */
    function assToVtt(text) {
        const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
        let section = '';
        let fields = [];
        const cues = [];
        lines.forEach((line) => {
            const stripped = line.trim();
            if (stripped.startsWith('[') && stripped.endsWith(']')) {
                section = stripped.slice(1, -1).trim().toLowerCase();
                return;
            }
            if (section !== 'events') return;
            if (stripped.toLowerCase().startsWith('format:')) {
                fields = stripped.split(':')[1].split(',').map((name) => name.trim().toLowerCase());
                return;
            }
            if (!stripped.toLowerCase().startsWith('dialogue:') || !fields.length) return;
            const parts = stripped.split(':').slice(1).join(':').split(',');
            if (parts.length < fields.length) return;
            const start = parseTimestamp(parts[fields.indexOf('start')]);
            const end = parseTimestamp(parts[fields.indexOf('end')]);
            if (start === null || end === null) return;
            // The text is the last field and may contain commas itself.
            const body = parts.slice(fields.length - 1).join(',')
                .replace(/\{[^}]*\}/g, '')
                .replace(/\\N|\\n|\\h/gi, (match) => (match.toLowerCase() === '\\h' ? ' ' : '\n'))
                .trim();
            cues.push(vttTime(start) + ' --> ' + vttTime(end) + '\n' + body);
        });
        return 'WEBVTT\n\n' + cues.join('\n\n') + (cues.length ? '\n' : '');
    }

    /** Whatever a subtitle file holds, as WebVTT. */
    function toVtt(text) {
        const body = String(text || '');
        if (body.trim().toUpperCase().startsWith('WEBVTT')) return body;
        if (/\[events\]/i.test(body) && /dialogue:/i.test(body)) return assToVtt(body);
        if (/-->/.test(body)) return srtToVtt(body);
        return 'WEBVTT\n\n' + body;
    }

    /** Turn subtitle text into a blob URL the track tag accepts. */
    function subtitleBlob(text) {
        if (!text) return '';
        try {
            const blob = new Blob([toVtt(text)], { type: 'text/vtt' });
            return trackBlobUrl(URL.createObjectURL(blob));
        } catch (err) {
            return '';
        }
    }

    /** The subtitle source for one video: the text first, then the path. */
    function subtitleFor(query) {
        if (query.subtitleText) {
            const url = subtitleBlob(query.subtitleText);
            if (url) return url;
        }
        const value = query.subtitle;
        if (!value) return '';
        return fileUrl(value);
    }

    /**
     * Attach a subtitle that is given as a URL.
     *
     * A ``<track>`` only takes WebVTT, and only the backend guarantees it --
     * an SRT served straight from a path would be ignored without a word, so
     * the text is read and converted here when the browser lets it (same
     * origin, or a page opened from disk).  When the read is refused the
     * source is attached as it is and the picker below is the way out.
     */
    function attachSubtitleSource(src, key) {
        if (!src) {
            clearTracks();
            return;
        }
        if (!/^(https?|file):/i.test(src)) {
            attachTrack(src);
            return;
        }
        fetch(src)
            .then((response) => (response.ok ? response.text() : ''))
            .then((text) => {
                if (state.active !== key) return;      // the video changed meanwhile
                attachTrack(text ? subtitleBlob(text) : src);
            })
            .catch(() => {
                if (state.active === key) attachTrack(src);
            });
    }

    /* --------------------------------------------------------------- sources */

    /** Prefill the list from the query, then play what was asked for. */
    function bootFromQuery() {
        const query = readQuery();
        const subtitleUrl = subtitleFor(query);
        const paths = [];
        if (query.video && !/^https?:/i.test(query.video)) paths.push(query.video);
        parseFileList(query.files).forEach((path) => paths.push(path));
        query.file.forEach((path) => paths.push(path));

        let wanted = null;
        paths.forEach((path) => {
            const isWanted = path === query.video;
            const item = addItem({
                key: path,
                name: baseName(path),
                path: isLocalPath(path) ? path : '',
                url: fileUrl(path),
                subtitleUrl: isWanted ? subtitleUrl : ''
            });
            if (isWanted) wanted = item;
        });

        // A URL video is not a file in the list until it is played; it still
        // has to be reachable, so it goes in as its own entry.
        if (query.video && !wanted) {
            wanted = addItem({
                key: query.video,
                name: query.title || baseName(query.video) || '视频',
                path: isLocalPath(query.video) ? query.video : '',
                url: fileUrl(query.video),
                subtitleUrl: subtitleUrl
            });
        }

        if (query.title) {
            const title = el('lv-title');
            if (title) title.textContent = query.title;
        } else if (wanted && wanted.name) {
            const title = el('lv-title');
            if (title) title.textContent = wanted.name;
        }

        renderList();
        if (wanted) {
            playItem(wanted);
        } else {
            setStatus('选择左边的文件开始播放。');
        }
    }

    /* ----------------------------------------------------------------- files */

    /** Add the files a picker handed over, as blob URLs. */
    function addPickedFiles(fileList) {
        if (!fileList || !fileList.length) return null;
        let first = null;
        Array.prototype.forEach.call(fileList, (file) => {
            if (!file || !file.name) return;
            if (!/\.(mp4|mkv|mov|avi|webm|m4v|flv|ts)$/i.test(file.name)) return;
            let url = '';
            try {
                url = trackBlobUrl(URL.createObjectURL(file));
            } catch (err) {
                return;
            }
            // The blob URL is the key: two picks of the same name are two
            // different objects, and only the blob can be played.
            const item = addItem({ key: url, name: file.name, url: url, size: file.size });
            if (item && !first) first = item;
        });
        renderList();
        return first;
    }

    /* ------------------------------------------------------------------ keys */

    /** Space plays, the arrows jump; typing in the search box keeps its keys. */
    function bindKeys() {
        document.addEventListener('keydown', (event) => {
            const tag = (event.target && event.target.tagName) || '';
            if (tag === 'INPUT' || tag === 'TEXTAREA') return;
            const videoEl = el('lv-video');
            if (!videoEl) return;
            if (event.key === ' ') {
                event.preventDefault();
                if (videoEl.paused) videoEl.play().catch(() => {});
                else videoEl.pause();
            } else if (event.key === 'ArrowRight') {
                event.preventDefault();
                const end = videoEl.duration || 0;
                videoEl.currentTime = Math.min(end, (videoEl.currentTime || 0) + SEEK_STEP);
            } else if (event.key === 'ArrowLeft') {
                event.preventDefault();
                videoEl.currentTime = Math.max(0, (videoEl.currentTime || 0) - SEEK_STEP);
            } else if (event.key === 'Escape') {
                window.close();
            }
        });
    }

    /* ------------------------------------------------------------------ boot */

    function bindEvents() {
        const videoEl = el('lv-video');
        if (videoEl) {
            videoEl.addEventListener('loadedmetadata', () => {
                const item = state.items.find((entry) => entry.key === state.active);
                if (item && isFinite(videoEl.duration)) {
                    item.duration = Math.round(videoEl.duration);
                    renderList();
                }
                setStatus('');
            });
            videoEl.addEventListener('error', () => {
                // The one failure the user can actually fix: the browser was
                // not allowed to read the path it was given.
                showHint('浏览器不允许直接播放这个本地路径。'
                    + '请用左边的「文件」按钮选中同一个文件，这样就能播放了。');
                setStatus('无法播放');
            });
            videoEl.addEventListener('play', () => {
                showHint('');
                setStatus('正在播放');
            });
            videoEl.addEventListener('pause', () => setStatus('已暂停'));
            videoEl.addEventListener('ended', () => setStatus('播放结束'));
        }

        const search = el('lv-search');
        if (search) {
            search.addEventListener('input', () => {
                state.search = search.value || '';
                renderList();
            });
        }

        const dirInput = el('lv-dir');
        if (dirInput) {
            dirInput.addEventListener('change', () => {
                const first = addPickedFiles(dirInput.files);
                if (first) playItem(first);
                dirInput.value = '';
            });
        }

        const fileInput = el('lv-files');
        if (fileInput) {
            fileInput.addEventListener('change', () => {
                const first = addPickedFiles(fileInput.files);
                if (first) playItem(first);
                fileInput.value = '';
            });
        }

        // SRT and ASS are not WebVTT, so a picked subtitle has to be converted
        // before the track tag will show it.
        const subtitleInput = el('lv-subtitle');
        if (subtitleInput) {
            subtitleInput.addEventListener('change', () => {
                const file = subtitleInput.files && subtitleInput.files[0];
                subtitleInput.value = '';
                if (!file) return;
                file.text().then((text) => {
                    if (state.active === '') {
                        setStatus('先选一个视频，再挂字幕。');
                        return;
                    }
                    const url = subtitleBlob(text);
                    if (!url) {
                        setStatus('这个字幕文件读不出来。');
                        return;
                    }
                    const item = state.items.find((entry) => entry.key === state.active);
                    if (item) item.subtitleUrl = url;
                    attachTrack(url);
                    renderList();
                    setStatus('字幕已挂上：' + file.name);
                }).catch(() => setStatus('这个字幕文件读不出来。'));
            });
        }

        const close = el('lv-close');
        if (close) {
            close.addEventListener('click', () => window.close());
        }

        window.addEventListener('beforeunload', releaseBlobUrls);
    }

    document.addEventListener('DOMContentLoaded', () => {
        bindEvents();
        bindKeys();
        bootFromQuery();
    });
})();
