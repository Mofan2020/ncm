/**
 * SkycMedia - Bilibili video playback.
 *
 * The B站 streams come in two halves (a video track and an audio track), so the
 * <video> element cannot simply be given a URL: the two halves have to be
 * glued together.  Media Source Extensions do that in the browser, which is
 * why this file exists instead of a two-line assignment in app.js.
 *
 * Three ways to play, in this order:
 *   1. MSE: the video and the audio track are appended to two SourceBuffers.
 *   2. The merged stream (`muxed`), played by the browser itself.
 *   3. Nothing playable -> the caller shows an error.
 *
 * Every session is released before the next one starts: a MediaSource that is
 * left open keeps its buffers (and its decoded audio) alive, which is how the
 * player used to freeze the sound of the previous video.
 */
'use strict';

(function () {
    /** The playback session that runs at the moment (null when nothing plays). */
    let session = null;

    /**
     * Release the running session.
     *
     * The SourceBuffers are removed first, then the MediaSource is closed: a
     * MediaSource that is still "open" keeps the last frame and the decoded
     * audio alive, so the next video would start with the sound of the old one.
     */
    function stopSession() {
        const current = session;
        session = null;
        if (!current) return;
        current.cancelled = true;
        if (current.audio) {
            try { current.audio.pause(); } catch (err) { /* 已经停了 */ }
            current.audio = null;
        }
        const mediaSource = current.mediaSource;
        if (mediaSource) {
            (current.buffers || []).forEach((buffer) => {
                try {
                    if (mediaSource.readyState === 'open') mediaSource.removeSourceBuffer(buffer);
                } catch (err) { /* 已经移除过了 */ }
            });
            try {
                if (mediaSource.readyState === 'open') mediaSource.endOfStream();
            } catch (err) { /* 状态已经不是 open */ }
            current.mediaSource = null;
        }
        current.buffers = [];
        if (current.objectUrl) {
            try { URL.revokeObjectURL(current.objectUrl); } catch (err) { /* 无所谓 */ }
            current.objectUrl = '';
        }
    }

    /** The mime type with the codecs, as MediaSource wants it. */
    function buildMime(track) {
        if (!track) return '';
        const mime = track.mime || '';
        const codecs = track.codecs || '';
        if (!codecs) return mime;
        // Some answers already carry the codecs.
        if (mime.indexOf('codecs=') >= 0) return mime;
        return mime + '; codecs="' + codecs + '"';
    }

    /**
     * The byte range of the initialisation segment.
     *
     * The backend may answer "0-1234", [0, 1234] or {start, end}; all three
     * mean the same thing, and none of them may be taken for granted.
     */
    function parseRange(range) {
        if (range === null || range === undefined) return null;
        if (Array.isArray(range)) {
            if (range.length < 2) return null;
            return { start: Number(range[0]), end: Number(range[1]) };
        }
        if (typeof range === 'object') {
            const start = Number(range.start);
            const end = Number(range.end);
            if (!isFinite(start) || !isFinite(end)) return null;
            return { start: start, end: end };
        }
        const match = String(range).match(/(\d+)\s*-\s*(\d+)/);
        if (!match) return null;
        return { start: Number(match[1]), end: Number(match[2]) };
    }

    /** A request for a byte range; without a range the whole file comes back. */
    function fetchRange(url, start, end) {
        const headers = {};
        if (start !== null && start !== undefined && start >= 0) {
            headers.Range = 'bytes=' + start + '-' + (end === null || end === undefined ? '' : end);
        }
        return fetch(url, { headers: headers });
    }

    /**
     * Append one chunk and wait until the SourceBuffer took it.
     *
     * appendBuffer() is asynchronous: appending again before "updateend" throws
     * InvalidStateError, so every append waits for the previous one.
     */
    function appendBuffer(buffer, chunk, current) {
        return new Promise((resolve, reject) => {
            const cleanup = () => {
                buffer.removeEventListener('updateend', onEnd);
                buffer.removeEventListener('error', onError);
            };
            const onEnd = () => { cleanup(); resolve(); };
            const onError = () => { cleanup(); reject(new Error('source buffer failed')); };
            buffer.addEventListener('updateend', onEnd);
            buffer.addEventListener('error', onError);
            if (current && current.cancelled) { cleanup(); resolve(); return; }
            try {
                buffer.appendBuffer(chunk);
            } catch (err) {
                cleanup();
                reject(err);
            }
        });
    }

    /** Append the body of a response piece by piece, so nothing is buffered whole. */
    async function appendStream(buffer, response, current) {
        if (!response.body || !response.body.getReader) {
            const chunk = await response.arrayBuffer();
            await appendBuffer(buffer, chunk, current);
            return;
        }
        const reader = response.body.getReader();
        while (true) {
            const part = await reader.read();
            if (part.done) break;
            if (current && current.cancelled) {
                try { reader.cancel(); } catch (err) { /* 已经断了 */ }
                return;
            }
            await appendBuffer(buffer, part.value, current);
        }
    }

    /**
     * Fill one SourceBuffer.
     *
     * The initialisation segment (the moov box) comes first, otherwise the
     * browser does not know the duration and the codecs until it has seen the
     * whole file.  When the server ignores the Range header the answer is the
     * whole file, and there is nothing left to fetch afterwards.
     */
    async function appendTrack(buffer, track, current) {
        const initRange = parseRange(track.init_range);
        if (initRange && initRange.end >= initRange.start) {
            const response = await fetchRange(track.url, initRange.start, initRange.end);
            if (!response.ok) throw new Error('http ' + response.status);
            if (response.status === 206) {
                const chunk = await response.arrayBuffer();
                await appendBuffer(buffer, chunk, current);
                if (current.cancelled) return;
                const rest = await fetchRange(track.url, initRange.end + 1, null);
                if (!rest.ok) throw new Error('http ' + rest.status);
                // A "200" here would be the whole file again: the bytes would
                // be written twice, so this is a failure and not a fallback.
                if (rest.status !== 206 && rest.status !== 200) {
                    throw new Error('unexpected range answer: ' + rest.status);
                }
                if (rest.status === 206) await appendStream(buffer, rest, current);
                return;
            }
            await appendStream(buffer, response, current);
            return;
        }
        // Without an initialisation segment the file is appended as it comes.
        const response = await fetchRange(track.url, null, null);
        if (!response.ok) throw new Error('http ' + response.status);
        await appendStream(buffer, response, current);
    }

    /** Play the two tracks through Media Source Extensions. */
    async function playWithMse(videoEl, streams, status) {
        if (!window.MediaSource || !window.MediaSource.isTypeSupported) return false;
        const videoMime = buildMime(streams.video);
        const audioMime = buildMime(streams.audio);
        if (!videoMime || !audioMime) return false;
        if (!window.MediaSource.isTypeSupported(videoMime)) return false;
        if (!window.MediaSource.isTypeSupported(audioMime)) return false;

        const mediaSource = new window.MediaSource();
        const objectUrl = URL.createObjectURL(mediaSource);
        const current = { mediaSource: mediaSource, objectUrl: objectUrl, buffers: [], cancelled: false };
        session = current;
        videoEl.src = objectUrl;

        await new Promise((resolve) => {
            if (mediaSource.readyState === 'open') { resolve(); return; }
            mediaSource.addEventListener('sourceopen', () => resolve(), { once: true });
        });
        if (current.cancelled) return false;

        const videoBuffer = mediaSource.addSourceBuffer(videoMime);
        const audioBuffer = mediaSource.addSourceBuffer(audioMime);
        current.buffers = [videoBuffer, audioBuffer];
        try {
            videoBuffer.mode = 'segments';
            audioBuffer.mode = 'segments';
        } catch (err) { /* 旧内核不认这个模式 */ }

        if (streams.duration) {
            try { mediaSource.duration = Number(streams.duration); } catch (err) { /* 让浏览器自己算 */ }
        }

        status('buffering');
        await Promise.all([
            appendTrack(videoBuffer, streams.video, current),
            appendTrack(audioBuffer, streams.audio, current)
        ]);
        if (current.cancelled) return false;
        try { mediaSource.endOfStream(); } catch (err) { /* 已经结束了 */ }
        status('ready');
        try { await videoEl.play(); } catch (err) { /* 自动播放被拦下时用户自己点 */ }
        return true;
    }

    /**
     * Play a video.
     *
     * @param {HTMLVideoElement} videoEl the element that shows the video
     * @param {Object} streams the answer of get_bilibili_video_streams
     * @param {Object} [options] {onStatus: function (code)}
     * @returns {Promise<boolean>} true when something is playing
     */
    async function play(videoEl, streams, options) {
        const opts = options || {};
        const status = typeof opts.onStatus === 'function' ? opts.onStatus : function () {};
        stopSession();
        if (!videoEl || !streams) {
            status('error');
            return false;
        }
        // The picture starts later than the data does: the caller can say
        // "streaming" as soon as the video really runs.
        try {
            videoEl.addEventListener('playing', () => status('streaming'), { once: true });
        } catch (err) { /* 没有播放器的测试环境 */ }
        if (streams.video && streams.audio) {
            try {
                const played = await playWithMse(videoEl, streams, status);
                if (played) return true;
            } catch (err) {
                // The MSE path failed: the merged stream is the fallback.
                console.error('bilibili MSE failed:', err);
            }
            stopSession();
        }
        if (streams.muxed && streams.muxed.url) {
            videoEl.src = streams.muxed.url;
            try { await videoEl.play(); } catch (err) { /* 自动播放被拦下 */ }
            status('ready');
            return true;
        }
        if (streams.video && streams.video.url && !streams.audio) {
            // A video without a sound track: the picture is still worth it.
            videoEl.src = streams.video.url;
            try { await videoEl.play(); } catch (err) { /* 自动播放被拦下 */ }
            status('ready');
            return true;
        }
        if (streams.audio && streams.audio.url) {
            // Audio only: there is no picture, but the track is still worth it.
            // The video element is emptied, so no dead blob is left behind.
            try {
                videoEl.removeAttribute('src');
                videoEl.load();
            } catch (err) { /* 忽略 */ }
            const audio = new Audio();
            audio.src = streams.audio.url;
            try { await audio.play(); } catch (err) { /* 自动播放被拦下 */ }
            session = { mediaSource: null, objectUrl: '', buffers: [], audio: audio, cancelled: false };
            status('audio_only');
            return true;
        }
        status('error');
        return false;
    }

    window.playBiliVideo = play;
    window.stopBiliVideo = stopSession;
})();
