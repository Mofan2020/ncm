/* SkycMedia - the overlay that sits on top of a player.
 *
 * Both player surfaces (the standalone window and the modal inside the main
 * window) have the same problem: while the backend is being reached, or while
 * the streams are being fetched, there is nothing to look at, and a black
 * rectangle with a one-word status line tells the user nothing.  This is the
 * shared piece that paints a proper state on top of the video: what is
 * happening, and a way out when it goes wrong.
 *
 * The caller supplies the translations, because the standalone window and the
 * main window read them from different places.
 */
(function () {
    'use strict';

    /**
     * Build the overlay controller for one player.
     *
     * @param {object} options
     * @param {Function} options.t  translation lookup: t('key') -> string
     * @param {Function} [options.onRetry]  called when the user asks to retry
     */
    function createOverlay(options) {
        const t = options.t || function (key) { return key; };
        const nodes = {
            root: document.getElementById(options.root || 'bp-overlay'),
            title: document.getElementById(options.title || 'bp-overlay-title'),
            detail: document.getElementById(options.detail || 'bp-overlay-detail'),
            spinner: document.getElementById(options.spinner || 'bp-overlay-spinner'),
            actions: document.getElementById(options.actions || 'bp-overlay-actions'),
            retry: document.getElementById(options.retry || 'bp-overlay-retry')
        };

        if (nodes.retry) {
            nodes.retry.textContent = t('bili.overlay_retry');
            nodes.retry.addEventListener('click', () => {
                if (options.onRetry) options.onRetry();
            });
        }

        /** Show a state on top of the picture. */
        function show(state, detailKey) {
            if (!nodes.root) return;
            nodes.root.hidden = false;
            nodes.root.setAttribute('data-state', state || 'loading');
            if (nodes.spinner) {
                nodes.spinner.hidden = state === 'error';
            }
            const titles = {
                connecting: 'bili.overlay_connecting',
                loading: 'bili.overlay_streams',
                error: 'bili.overlay_failed'
            };
            const title = t(titles[state] || 'bili.overlay_streams');
            if (nodes.title) nodes.title.textContent = title;
            if (nodes.detail) {
                const detail = detailKey ? t(detailKey) : '';
                nodes.detail.textContent = detail || '';
                nodes.detail.hidden = !detail;
            }
            if (nodes.actions) {
                nodes.actions.hidden = state !== 'error';
            }
        }

        /** Take the overlay away; the video is visible again. */
        function hide() {
            if (!nodes.root) return;
            nodes.root.hidden = true;
        }

        return { show: show, hide: hide };
    }

    window.BiliOverlay = { createOverlay: createOverlay };
})();
