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
 * The MSE path is **progressive**: the initialisation segment goes in first,
 * then the rest arrives one byte range at a time and is appended as it lands,
 * and the picture starts as soon as a couple of seconds are buffered.
 * Appending the whole file before the first frame -- what this file used to
 * do -- meant staring at a spinner for as long as the whole video took to
 * download, and a large file blew the SourceBuffer quota before it finished.
 *
 * Every session is released before the next one starts: a MediaSource that is
 * left open keeps its buffers (and its decoded audio) alive, which is how the
 * player used to freeze the sound of the previous video.
 */
'use strict';

(function () {
    /** Bytes asked for in one range request. */
    const CHUNK = 1024 * 1024;
    /** Seconds of picture kept in front of the play head before pausing the feed. */
    const BUFFER_TARGET = 20;
    /** Seconds that have to be buffered before playback starts. */
    const START_THRESHOLD = 2;
    /** Seconds of already-played data kept behind the play head. */
    const TRIM_BEHIND = 30;
    /** How long to wait for the first picture before giving up on MSE. */
    const START_TIMEOUT = 20000;

    /** The playback session that runs at the moment (null when nothing plays). */
    let session = null;

    function sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

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
        if (current.seekHandler && current.videoEl) {
            try { current.videoEl.removeEventListener('seeking', current.seekHandler); } catch (err) { /* 元素已经没了 */ }
            current.seekHandler = null;
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

    /**
     * Seconds of continuous picture available after ``time``.
     *
     * A gap ahead of the play head counts as zero: the video would stall at the
     * gap, so "forty seconds buffered somewhere later" is not progress.
     */
    function trackAhead(buffer, time) {
        const ranges = buffer && buffer.buffered;
        if (!ranges || !ranges.length) return 0;
        for (let i = 0; i < ranges.length; i += 1) {
            const start = ranges.start(i);
            const end = ranges.end(i);
            if (time >= start - 0.35 && time <= end) return end - time;
        }
        return 0;
    }

    /**
     * Drop data behind the play head, so a SourceBuffer never fills up.
     *
     * Awaited like every other buffer operation: ``remove()`` is asynchronous,
     * and appending while it is still running throws ``InvalidStateError`` --
     * which poisons the whole MediaSource with a decode error.
     */
    function trimBehind(buffer, time, current) {
        const ranges = buffer && buffer.buffered;
        if (!ranges || !ranges.length || time < TRIM_BEHIND) return Promise.resolve();
        const cut = time - TRIM_BEHIND;
        if (ranges.start(0) >= cut - 1) return Promise.resolve();
        return new Promise((resolve) => {
            const done = () => {
                buffer.removeEventListener('updateend', done);
                resolve();
            };
            buffer.addEventListener('updateend', done);
            try {
                buffer.remove(0, cut);
            } catch (err) {
                buffer.removeEventListener('updateend', done);
                resolve();
            }
        });
    }

    /** Whether ``time`` is inside what the buffer already holds. */
    function isBuffered(buffer, time) {
        const ranges = buffer && buffer.buffered;
        if (!ranges) return false;
        for (let i = 0; i < ranges.length; i += 1) {
            if (ranges.start(i) - 0.5 <= time && time <= ranges.end(i)) return true;
        }
        return false;
    }

    /**
     * Hand the picture over to the merged stream, at ``time``.
     *
     * The merged stream is a plain file with both halves in it: the browser
     * seeks in it by itself, and it is the only way to jump to a place the
     * SourceBuffers do not hold.
     */
    function switchToMuxed(videoEl, streams, time) {
        if (!streams.muxed || !streams.muxed.url) return false;
        const target = Number(time) || 0;
        stopSession();
        try {
            videoEl.addEventListener('loadedmetadata', () => {
                try { videoEl.currentTime = target; } catch (err) { /* 还没准备好 */ }
            }, { once: true });
        } catch (err) { /* 元素已经没了 */ }
        videoEl.src = streams.muxed.url;
        try { videoEl.play(); } catch (err) { /* 自动播放被拦下 */ }
        return true;
    }

    /**
     * Feed one track: the initialisation segment first, then the rest in byte
     * ranges, staying no more than BUFFER_TARGET seconds ahead of the picture.
     */
    async function feedTrack(buffer, track, current) {
        const initRange = parseRange(track.init_range);
        let offset = 0;
        if (initRange && initRange.end >= initRange.start) {
            const response = await fetchRange(track.url, initRange.start, initRange.end);
            if (current.cancelled) return;
            if (!response.ok) throw new Error('http ' + response.status);
            const chunk = await response.arrayBuffer();
            await appendBuffer(buffer, chunk, current);
            if (response.status === 200) return;        // the whole file came back
            offset = initRange.end + 1;
        }
        while (!current.cancelled) {
            if (trackAhead(buffer, current.videoEl.currentTime) > BUFFER_TARGET) {
                await trimBehind(buffer, current.videoEl.currentTime, current);
                await sleep(200);
                continue;
            }
            const response = await fetchRange(track.url, offset, offset + CHUNK - 1);
            if (current.cancelled) return;
            if (!response.ok) throw new Error('http ' + response.status);
            const chunk = await response.arrayBuffer();
            if (!chunk.byteLength) return;
            await appendBuffer(buffer, chunk, current);
            if (response.status === 200) return;        // the server ignored the range
            offset += chunk.byteLength;
            if (chunk.byteLength < CHUNK) return;       // end of the file
        }
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
        const current = {
            mediaSource: mediaSource,
            objectUrl: objectUrl,
            buffers: [],
            cancelled: false,
            videoEl: videoEl,
            duration: Number(streams.duration) || 0
        };
        session = current;
        videoEl.src = objectUrl;

        await new Promise((resolve) => {
            if (mediaSource.readyState === 'open') { resolve(); return; }
            let settled = false;
            const done = () => { if (!settled) { settled = true; resolve(); } };
            mediaSource.addEventListener('sourceopen', done, { once: true });
            // 没有 sourceopen 就是这个 WebView 不支持 MSE（旧内核、或者页面不是
            // http 来源）。以前这里会一直等下去，视频就永远黑着不播。
            setTimeout(done, 3000);
        });
        if (current.cancelled) return false;
        if (mediaSource.readyState !== 'open') return false;

        const videoBuffer = mediaSource.addSourceBuffer(videoMime);
        const audioBuffer = mediaSource.addSourceBuffer(audioMime);
        current.buffers = [videoBuffer, audioBuffer];
        try {
            videoBuffer.mode = 'segments';
            audioBuffer.mode = 'segments';
        } catch (err) { /* 旧内核不认这个模式 */ }

        if (current.duration) {
            try { mediaSource.duration = current.duration; } catch (err) { /* 让浏览器自己算 */ }
        }

        // A seek past what is buffered cannot be served from the two halves:
        // the demuxer would have to cut a fragment in half, and WebKit answers
        // that with a decode error that kills the picture.  The merged stream
        // is an ordinary file, so the browser seeks in it by itself.
        current.seekHandler = () => {
            const time = videoEl.currentTime;
            if (isBuffered(videoBuffer, time) && isBuffered(audioBuffer, time)) return;
            switchToMuxed(videoEl, streams, time);
        };
        videoEl.addEventListener('seeking', current.seekHandler);

        const feeds = [
            feedTrack(videoBuffer, streams.video, current).catch((err) => {
                if (!current.cancelled) console.error('bilibili video track failed:', err);
            }),
            feedTrack(audioBuffer, streams.audio, current).catch((err) => {
                if (!current.cancelled) console.error('bilibili audio track failed:', err);
            })
        ];

        status('buffering');
        const started = await waitForStart(current);
        if (!started || current.cancelled) return false;
        status('ready');
        try { await videoEl.play(); } catch (err) { /* 自动播放被拦下时用户自己点 */ }
        // The feeds keep running in the background; nothing waits for them.
        Promise.all(feeds).catch(() => {});
        return true;
    }

    /** Wait until both tracks hold a little picture ahead of the play head. */
    async function waitForStart(current) {
        const deadline = Date.now() + START_TIMEOUT;
        while (!current.cancelled && Date.now() < deadline) {
            const time = current.videoEl.currentTime || 0;
            const [videoBuffer, audioBuffer] = current.buffers;
            const videoAhead = trackAhead(videoBuffer, time);
            const audioAhead = trackAhead(audioBuffer, time);
            if (videoAhead >= START_THRESHOLD && audioAhead >= START_THRESHOLD) return true;
            await sleep(150);
        }
        return false;
    }

    /**
     * Play a video.
     *
     * @param {HTMLVideoElement} videoEl the element that shows the video
     * @param {Object} streams the answer of get_bilibili_video_streams
     * @param {Object} [options] {onStatus: function (code)}
     * @returns {Promise<boolean>} true when something is playing
     */
    /**
     * Is there actually a picture on the screen?
     *
     * The DOM cannot tell a black frame from a good one: a decoder that
     * produced nothing and a decoder that produced a normal picture both
     * report "readyState 4" and a video size.  Reading the pixels back is
     * the only way to know, and it is what turns "the sound plays but the
     * screen is black" into something the program can act on.
     *
     * @returns {boolean} true when the frame is (nearly) black
     */
    function frameIsBlack(videoEl) {
        if (!videoEl || !videoEl.videoWidth || !videoEl.videoHeight) return true;
        try {
            const canvas = document.createElement('canvas');
            const width = Math.min(videoEl.videoWidth, 160);
            const height = Math.max(1, Math.round(videoEl.videoHeight * width / videoEl.videoWidth));
            canvas.width = width;
            canvas.height = height;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(videoEl, 0, 0, width, height);
            const data = ctx.getImageData(0, 0, width, height).data;
            let sum = 0;
            for (let i = 0; i < data.length; i += 4) {
                sum += 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
            }
            // 12 is well below the darkest real footage: a video that is
            // merely dark still has highlights, an empty screen does not.
            return (sum / (width * height)) < 12;
        } catch (err) {
            // If the pixels cannot be read (a tainted canvas, a browser
            // without canvas), assume the picture is there rather than
            // throwing away a video that may be playing fine.
            return false;
        }
    }

    /**
     * Watch the picture after the playback started.
     *
     * Many videos open on a black frame, so the first second or so is not
     * counted: only frames past that point can prove that nothing is going
     * to show up.  When the window is over and the screen is still black,
     * the caller is told so it can try the other source.
     *
     * @returns {Promise<boolean>} true when a picture showed up
     */
    async function pictureAppeared(videoEl, windowMs) {
        const deadline = Date.now() + (windowMs || 7000);
        while (Date.now() < deadline) {
            await new Promise((resolve) => setTimeout(resolve, 500));
            if (!videoEl || videoEl.error) return false;
            if ((videoEl.currentTime || 0) < 1.2) continue;
            if (!frameIsBlack(videoEl)) return true;
        }
        return false;
    }

    /**
     * Keep an eye on the picture and give the other source a turn when
     * nothing shows up.
     *
     * The two ways of playing the same video fail differently: the merged
     * file is played natively but tops out at 720P, the DASH halves reach
     * 1080P but need the webview to put them together.  When the one that is
     * running shows a black screen, the other one gets a turn; only when
     * both stay black is it called a failure.
     *
     * This runs after the playback has already been handed to the caller, so
     * it never delays the first picture.
     */
    async function watchPicture(videoEl, streams, status, current) {
        const srcAtStart = videoEl.src;
        const showed = await pictureAppeared(videoEl);
        if (showed) return;
        // The user moved on to another video: this verdict is about a
        // picture that is no longer on the screen.
        if (!videoEl || videoEl.error || videoEl.src !== srcAtStart) return;
        if (current === 'muxed' && streams.video && streams.audio) {
            try {
                const played = await playWithMse(videoEl, streams, status);
                if (played && await pictureAppeared(videoEl)) return;
            } catch (err) {
                console.error('bilibili MSE fallback failed:', err);
            }
        } else if (current === 'mse' && streams.muxed && streams.muxed.url) {
            stopSession();
            videoEl.src = streams.muxed.url;
            try { await videoEl.play(); } catch (err) { /* 自动播放被拦下 */ }
            if (await pictureAppeared(videoEl)) return;
        }
        status('error');
    }

    /**
     * The picture of the standalone window and of the inline player.
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
        // The merged file first: the browser plays it natively, with no
        // MSE involved.  This is the path that always works on macOS; the
        // DASH route below needs the webview to composite progressive
        // SourceBuffer data, and feedback from a real window was "sound
        // plays, the picture stays black" even though the decoder had
        // already produced frames (canvas.drawImage read a normal picture).
        // Quality tops out at 720P here, which is the price of a picture.
        if (streams.muxed && streams.muxed.url) {
            videoEl.src = streams.muxed.url;
            try { await videoEl.play(); } catch (err) { /* 自动播放被拦下 */ }
            status('ready');
            watchPicture(videoEl, streams, status, 'muxed').catch(() => {});
            return true;
        }
        if (streams.video && streams.audio) {
            try {
                const played = await playWithMse(videoEl, streams, status);
                if (played) {
                    watchPicture(videoEl, streams, status, 'mse').catch(() => {});
                    return true;
                }
            } catch (err) {
                // The MSE path failed: nothing else left.
                console.error('bilibili MSE failed:', err);
            }
            stopSession();
        }
        if (streams.video && streams.video.url) {
            // The two DASH halves could not be put together (no MSE, or the
            // merged file is missing).  A picture without sound still beats
            // sound without a picture: before this ran only when there was
            // no audio track at all, so a failed MSE fell through to the
            // audio-only branch below and the window played the sound with
            // nothing on screen.
            videoEl.src = streams.video.url;
            try { await videoEl.play(); } catch (err) { /* 自动播放被拦下 */ }
            status('ready');
            return true;
        }
        if (streams.audio && streams.audio.url) {
            // Audio only: nothing else came through.  The video element is
            // emptied, so no dead blob is left behind, and the status says so
            // instead of leaving the user staring at a black frame.
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
