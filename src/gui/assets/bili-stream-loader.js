/* SkycMedia - the one way to get a video playing.
 *
 * The inline player in the main window and the standalone window each had
 * their own copy of this: two places to fix, and they drifted.  The
 * standalone one lost the timeout (an AbortController that was never
 * connected to the bridge call, so it never fired), the inline one kept a
 * copy of the resume logic that the standalone one did not have.
 *
 * Everything that stands between "the user asked for a video" and "the
 * picture is on screen" lives here now, and both surfaces call the same
 * functions.
 */
(function () {
    'use strict';

    /* The deadline for the backend call.  Long enough for a slow network,
     * short enough that the user is not left staring at a spinner. */
    const DEFAULT_TIMEOUT = 20000;

    /**
     * Fetch the streams for a video, with a deadline.
     *
     * The bridge call cannot be aborted: pywebview hands the arguments to
     * the backend and waits for the answer, and there is no signal to pass
     * down.  So the deadline is a race -- the call in the background is
     * left to finish on its own, and the caller hears about the timeout
     * right away.
     *
     * @param {object} api the bridge
     * @param {string} bvid the video
     * @param {number} quality the wanted quality
     * @param {number} [timeoutMs]
     * @returns {Promise<Object>} the answer of the backend
     */
    async function fetchStreams(api, bvid, quality, timeoutMs) {
        if (!api || typeof api.get_bilibili_video_streams !== 'function') {
            const err = new Error('no bridge');
            err.name = 'NoBridgeError';
            throw err;
        }
        let timer = null;
        const deadline = new Promise(function (resolve, reject) {
            timer = setTimeout(function () {
                const err = new Error('the backend did not answer in time');
                err.name = 'TimeoutError';
                reject(err);
            }, timeoutMs || DEFAULT_TIMEOUT);
        });
        try {
            return await Promise.race([
                api.get_bilibili_video_streams(bvid, quality),
                deadline
            ]);
        } finally {
            clearTimeout(timer);
        }
    }

    /**
     * Jump to the last place the user was watching this video.
     *
     * @returns {Promise<number>} the position in seconds, 0 when there is
     *   nothing to resume
     */
    async function resume(api, bvid, videoEl) {
        try {
            const prog = await api.get_bilibili_video_progress(bvid);
            if (!prog || !prog.success) return 0;
            if (!(prog.progress > 1) || !(prog.duration > 0)) return 0;
            const target = Math.floor(prog.duration * prog.progress / 100);
            if (!target) return 0;
            videoEl.currentTime = target;
            return target;
        } catch (err) {
            // Resuming is a nicety; if the history cannot be read the video
            // simply starts at the beginning.
            return 0;
        }
    }

    /**
     * Put streams that are already in hand on screen.
     *
     * A bangumi episode does not come from `get_bilibili_video_streams` (it
     * has no bvid), so it cannot go through `load()`; it still has to go
     * through the same playback, which is what this is for.
     *
     * @returns {Promise<boolean>} true when something is playing
     */
    async function play(videoEl, streams, options) {
        const opts = options || {};
        const onStatus = typeof opts.onStatus === 'function'
            ? opts.onStatus : function () {};
        if (!videoEl || !streams) {
            onStatus('error');
            return false;
        }
        try {
            const played = await window.playBiliVideo(videoEl, streams, {onStatus: onStatus});
            if (!played) onStatus('error');
            return !!played;
        } catch (err) {
            onStatus('error');
            return false;
        }
    }

    /**
     * Fetch the streams of a video and put them on screen.
     *
     * @param {object} options
     * @param {object} options.api the bridge
     * @param {string} options.bvid the video
     * @param {number} options.quality the wanted quality
     * @param {HTMLVideoElement} options.videoEl the element that shows it
     * @param {Function} [options.onStatus] the status codes of the player
     * @param {Function} [options.onStreams] called with the streams, before
     *   the playback starts, so the caller can fill its own labels
     * @param {boolean} [options.resumePlayback] jump to the last position
     * @param {number} [options.timeoutMs]
     * @returns {Promise<Object>} {ok, error, result, resumed}
     *   error is one of: no_video, timeout, network, no_stream
     */
    async function load(options) {
        const opts = options || {};
        const api = opts.api;
        const videoEl = opts.videoEl;
        const bvid = opts.bvid;
        const onStatus = typeof opts.onStatus === 'function'
            ? opts.onStatus : function () {};

        if (!videoEl || !bvid) {
            onStatus('error');
            return {ok: false, error: 'no_video', result: null, resumed: 0};
        }

        onStatus('buffering');
        let result = null;
        try {
            result = await fetchStreams(api, bvid, opts.quality, opts.timeoutMs);
        } catch (err) {
            onStatus('error');
            const timeout = err && err.name === 'TimeoutError';
            return {
                ok: false,
                error: timeout ? 'timeout' : 'network',
                result: null,
                resumed: 0
            };
        }

        if (!result || !result.success) {
            onStatus('error');
            return {ok: false, error: 'no_stream', result: result, resumed: 0};
        }

        if (typeof opts.onStreams === 'function') opts.onStreams(result);

        const played = await play(videoEl, result, {onStatus: onStatus});
        if (!played) {
            return {ok: false, error: 'no_stream', result: result, resumed: 0};
        }

        let resumed = 0;
        if (opts.resumePlayback) {
            resumed = await resume(api, bvid, videoEl);
        }
        return {ok: true, error: '', result: result, resumed: resumed};
    }

    window.BiliStreamLoader = {
        fetchStreams: fetchStreams,
        resume: resume,
        play: play,
        load: load
    };
})();
