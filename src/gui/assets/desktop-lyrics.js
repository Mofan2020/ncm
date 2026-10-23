/**
 * Desktop Lyrics Window - Independent borderless window for lyrics display.
 * Communicates with main window via localStorage events (pywebview multi-window) or opener.postMessage.
 */
(function () {
    'use strict';

    const state = {
        lines: [],
        currentIndex: -1,
        settings: {
            fontSize: 18,
            color: '#ffffff',
            opacity: 0.9,
            showTranslation: true,
            // How the bubble behind the text is drawn:
            //   'dark'   - the default dark pill
            //   'translucent' - a see-through pill (desktop shows through)
            //   'none'   - no bubble at all, just the text
            background: 'dark',
            // How the glyphs themselves are drawn:
            //   'transparent' - see-through, outline only
            //   'translucent' - semi-transparent
            //   'white'       - pure white, fully opaque
            textStyle: 'white'
        },
        drag: { active: false, startX: 0, startY: 0, windowX: 0, windowY: 0 },
        hidden: false,
        reportedHeight: 0
    };

    const elements = {
        window: document.getElementById('dl-window'),
        dragHandle: document.getElementById('dl-drag-handle'),
        content: document.getElementById('dl-content'),
        current: null,
        translation: null,
    };

    /**
     * Resolve the rows that may exist at any time.  Built lazily because the
     * initial markup only ships the current row, and the rest are appended
     * when the first lyrics payload arrives.
     */
    function rows() {
        const root = elements.content;
        if (!root) return null;
        return {
            prev: root.querySelector('.lyric-prev'),
            current: root.querySelector('.lyric-current'),
            next: root.querySelector('.lyric-next'),
            translation: root.querySelector('.lyric-translation'),
        };
    }

    // Initialize from URL params or localStorage
    function init() {
        applySettingsFromStorage();
        bindDragEvents();
        listenForMessages();
        announceReady();
    }

    function applySettingsFromStorage() {
        try {
            const stored = localStorage.getItem('ncm:desktop-lyrics:settings');
            if (stored) {
                const parsed = JSON.parse(stored);
                state.settings = { ...state.settings, ...parsed };
            }
        } catch (e) { /* ignore */ }
        applyStyles();
    }

    function applyStyles() {
        const s = state.settings;
        const content = elements.content;
        if (!content) return;
        content.style.fontSize = s.fontSize + 'px';
        content.style.color = s.color;
        content.style.opacity = s.opacity;
        // The three rows (prev/current/next) and translation inherit the
        // container's color; the only thing that needs an explicit style
        // assignment is the visual differentiation by opacity/size.
        const r = rows();
        if (r) {
            ['prev', 'current', 'next', 'translation'].forEach((k) => {
                if (r[k]) r[k].style.color = s.color;
            });
        }
        applyBackgroundMode(s.background);
        applyTextStyle(s.textStyle);
        reportContentHeight();
    }

    /**
     * Draw the bubble behind the lyric text.
     *
     *  'dark'        - the default dark pill
     *  'translucent' - a see-through pill; the desktop shows through
     *  'none'        - no bubble, just the text
     *
     * The setting is a *mode*, not a colour, so the user picks a look rather
     * than a hex value.  'translucent' keeps a hint of the pill so the text
     * stays readable over a bright wallpaper; 'none' is the "pure text" mode.
     */
    /**
     * Tell the backend how tall the window has to be for this content.
     *
     * The window is created at a fixed 120 px, but the content grows with
     * the font size (112 px at 18 px, 159 px at 28 px).  A window that is
     * too short clips the text -- and because the bubble was anchored at the
     * bottom, the clipped part was the *top* line.
     *
     * The height is measured from the DOM and pushed to the backend, which
     * resizes the native window.  Failure is ignored: the window still
     * works, it is just a bit tight.
     */
    function reportContentHeight() {
        const content = elements.content;
        if (!content) return;
        // Measure after the browser has laid the new text out: reading the
        // box in the same tick as the DOM change gives the *old* height,
        // which is how the window ended up a frame behind the lyrics.
        requestAnimationFrame(() => {
            const height = Math.ceil(content.getBoundingClientRect().height) + 12;
            if (!height || height === state.reportedHeight) return;
            state.reportedHeight = height;
            try {
                if (window.pywebview && window.pywebview.api &&
                    window.pywebview.api.resize_desktop_lyrics) {
                    window.pywebview.api.resize_desktop_lyrics(height);
                }
            } catch (e) { /* ignore */ }
        });
    }

    /**
     * Draw the lyric text itself.
     *
     *  'solid'       - fully opaque glyphs (the default)
     *  'translucent' - semi-transparent: the bubble shows through the
     *                  letters, which reads well over a busy wallpaper
     *  'transparent' - the glyphs are see-through and only the outline
     *                  and shadow remain, so the desktop is clearly visible
     *
     * The style is applied to the container; the rows inherit it.
     */
    function applyTextStyle(style) {
        const content = elements.content;
        if (!content) return;
        const mode = style || 'white';
        content.dataset.textStyle = mode;
        const css = content.style;
        let rowOpacity = 1;
        if (mode === 'transparent') {
            css.webkitTextStroke = '1px rgba(255, 255, 255, 0.9)';
            css.textShadow = '0 1px 3px rgba(0, 0, 0, 0.9), 0 0 10px rgba(0, 0, 0, 0.7)';
            rowOpacity = 0.3;
        } else if (mode === 'translucent') {
            css.webkitTextStroke = '';
            css.textShadow = '0 1px 3px rgba(0, 0, 0, 0.85)';
            rowOpacity = 0.55;
        } else {
            css.webkitTextStroke = '';
            css.textShadow = '';
            rowOpacity = 1;
        }
        // The container's own opacity is the *window* opacity; the text style
        // is applied to the rows, so the two do not multiply into something
        // unreadable.
        const r = rows();
        if (r) {
            ['prev', 'current', 'next', 'translation'].forEach((k) => {
                if (r[k]) r[k].style.opacity = rowOpacity;
            });
        }
    }

    function applyBackgroundMode(mode) {
        const content = elements.content;
        if (!content) return;
        content.dataset.background = mode || 'dark';
        const style = content.style;
        if (mode === 'none') {
            style.background = 'transparent';
            style.boxShadow = 'none';
            style.backdropFilter = 'none';
            style.webkitBackdropFilter = 'none';
        } else if (mode === 'translucent') {
            style.background = 'rgba(0, 0, 0, 0.28)';
            style.boxShadow = '0 4px 20px rgba(0, 0, 0, 0.25)';
            style.backdropFilter = 'blur(3px)';
            style.webkitBackdropFilter = 'blur(3px)';
        } else {
            style.background = '';
            style.boxShadow = '';
            style.backdropFilter = '';
            style.webkitBackdropFilter = '';
        }
    }

    function bindDragEvents() {
        const handle = elements.dragHandle;
        const win = elements.window;

        handle.addEventListener('mousedown', (e) => {
            state.drag.active = true;
            state.drag.startX = e.clientX;
            state.drag.startY = e.clientY;
            const rect = win.getBoundingClientRect();
            state.drag.windowX = rect.left;
            state.drag.windowY = rect.top;
            win.classList.add('draggable');
            e.preventDefault();
        });

        document.addEventListener('mousemove', (e) => {
            if (!state.drag.active) return;
            const dx = e.clientX - state.drag.startX;
            const dy = e.clientY - state.drag.startY;
            const newX = state.drag.windowX + dx;
            const newY = state.drag.windowY + dy;
            // Keep within viewport
            const maxX = window.innerWidth - win.offsetWidth;
            const maxY = window.innerHeight - win.offsetHeight - 80; // Keep above player bar
            win.style.left = Math.max(0, Math.min(newX, maxX)) + 'px';
            win.style.top = Math.max(0, Math.min(newY, maxY)) + 'px';
            win.style.transform = 'none';
            win.style.bottom = 'auto';
        });

        document.addEventListener('mouseup', () => {
            if (state.drag.active) {
                state.drag.active = false;
                win.classList.remove('draggable');
                // Save position
                localStorage.setItem('ncm:desktop-lyrics:position', JSON.stringify({
                    left: win.style.left,
                    top: win.style.top
                }));
            }
        });

        // Touch support
        handle.addEventListener('touchstart', (e) => {
            const touch = e.touches[0];
            state.drag.active = true;
            state.drag.startX = touch.clientX;
            state.drag.startY = touch.clientY;
            const rect = win.getBoundingClientRect();
            state.drag.windowX = rect.left;
            state.drag.windowY = rect.top;
            win.classList.add('draggable');
        }, { passive: false });

        document.addEventListener('touchmove', (e) => {
            if (!state.drag.active) return;
            const touch = e.touches[0];
            const dx = touch.clientX - state.drag.startX;
            const dy = touch.clientY - state.drag.startY;
            const newX = state.drag.windowX + dx;
            const newY = state.drag.windowY + dy;
            const maxX = window.innerWidth - win.offsetWidth;
            const maxY = window.innerHeight - win.offsetHeight - 80;
            win.style.left = Math.max(0, Math.min(newX, maxX)) + 'px';
            win.style.top = Math.max(0, Math.min(newY, maxY)) + 'px';
            win.style.transform = 'none';
            win.style.bottom = 'auto';
            e.preventDefault();
        }, { passive: false });

        document.addEventListener('touchend', () => {
            if (state.drag.active) {
                state.drag.active = false;
                win.classList.remove('draggable');
                localStorage.setItem('ncm:desktop-lyrics:position', JSON.stringify({
                    left: win.style.left,
                    top: win.style.top
                }));
            }
        });

        // Double-click to toggle visibility
        elements.content.addEventListener('dblclick', toggleVisibility);

        // Right-click context menu for settings
        elements.content.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            showContextMenu(e.clientX, e.clientY);
        });
    }

    function listenForMessages() {
        // Listen for messages from main window via opener
        window.addEventListener('message', (event) => {
            // Verify origin (same origin policy for file:// is tricky, but pywebview uses same origin)
            if (event.data && event.data.type === 'ncm:desktop-lyrics') {
                handleMessage(event.data.payload);
            }
        });

        // Also listen for localStorage changes (fallback for cross-window communication)
        window.addEventListener('storage', (event) => {
            if (event.key === 'ncm:desktop-lyrics:update') {
                try {
                    const payload = JSON.parse(event.newValue);
                    handleMessage(payload);
                } catch (e) { /* ignore */ }
            } else if (event.key === 'ncm:desktop-lyrics:settings') {
                applySettingsFromStorage();
            } else if (event.key === 'ncm:desktop-lyrics:close') {
                closeWindow();
            }
        });

        // Load saved position
        try {
            const pos = localStorage.getItem('ncm:desktop-lyrics:position');
            if (pos) {
                const parsed = JSON.parse(pos);
                if (parsed.left) elements.window.style.left = parsed.left;
                if (parsed.top) elements.window.style.top = parsed.top;
                elements.window.style.transform = 'none';
                elements.window.style.bottom = 'auto';
            }
        } catch (e) { /* ignore */ }
    }

    function announceReady() {
        // Tell main window we're ready
        sendToMain({ type: 'ready' });
    }

    function handleMessage(payload) {
        switch (payload.action) {
            case 'update':
                // Every update from the bridge now carries the full lines
                // array (see src/gui/bridge.py::update_desktop_lyrics), so a
                // truthiness check on `payload.lines` is wrong: an empty
                // array must clear the overlay, not be ignored.
                if (payload.full) {
                    renderLyricLines(payload.lines || []);
                }
                if (typeof payload.index === 'number') {
                    updateLyricsIndex(payload.index);
                }
                break;
            case 'settings':
                state.settings = { ...state.settings, ...payload.settings };
                applyStyles();
                break;
            case 'hide':
                setVisibility(false);
                break;
            case 'show':
                setVisibility(true);
                break;
            case 'close':
                closeWindow();
                break;
        }
    }

    function updateLyrics(lines, index) {
        renderLyricLines(lines || []);
        updateLyricsIndex(index);
    }

    /**
     * Build (or rebuild) the prev/current/next lyric lines DOM from a fresh
     * lyrics array.  Only three rows are needed because the desktop window is
     * narrow and high-DPI -- the full scrolling list lives in the main
     * player panel.  We rebuild only when the line count actually changes,
     * so subsequent index-only updates stay free.
     */
    function renderLyricLines(lines) {
        state.lines = lines || [];
        const container = elements.content;
        if (!container) return;

        const prev = container.querySelector('.lyric-prev');
        const current = container.querySelector('.lyric-current');
        const next = container.querySelector('.lyric-next');
        const translation = container.querySelector('.lyric-translation');

        if (!prev || !current || !next) {
            // First build -- the static markup is missing the new rows.
            container.innerHTML = `
                <div class="lyric-prev"></div>
                <div class="lyric-current"></div>
                <div class="lyric-next"></div>
                <div class="lyric-translation" hidden></div>
            `;
        }

        const newCurrent = container.querySelector('.lyric-current');
        const newTranslation = container.querySelector('.lyric-translation');

        const line = state.lines[state.currentIndex >= 0 ? state.currentIndex : 0];
        if (line) {
            newCurrent.textContent = line.text || '';
            newTranslation.hidden = !state.settings.showTranslation || !line.translation;
            newTranslation.textContent = line.translation || '';
        } else {
            newCurrent.textContent = '';
            newTranslation.hidden = true;
        }

        updateLyricsIndex(state.currentIndex);

        // The content just grew (or shrank): the native window has to
        // follow.  Without this the overlay kept its initial height and
        // the lyrics were cut off -- the window is 120 px, but the content
        // is 112 px at 18 px and 196 px at 36 px.
        reportContentHeight();
    }

    function updateLyricsIndex(index) {
        // Skip if index hasn't changed
        if (state.currentIndex === index) {
            return;
        }
        state.currentIndex = index;

        const currentLine = state.lines[index];
        const container = elements.content;
        if (!container) return;
        const currentEl = container.querySelector('.lyric-current');
        const prevEl = container.querySelector('.lyric-prev');
        const nextEl = container.querySelector('.lyric-next');
        const translationEl = container.querySelector('.lyric-translation');

        if (currentEl) {
            currentEl.textContent = (currentLine && currentLine.text) || '';
        }
        if (prevEl) {
            const prevLine = state.lines[index - 1];
            prevEl.textContent = (prevLine && prevLine.text) || '';
        }
        if (nextEl) {
            const nextLine = state.lines[index + 1];
            nextEl.textContent = (nextLine && nextLine.text) || '';
        }

        if (translationEl) {
            if (state.settings.showTranslation && currentLine && currentLine.translation) {
                translationEl.hidden = false;
                translationEl.textContent = currentLine.translation;
            } else {
                translationEl.hidden = true;
            }
        }
    }

    function setVisibility(visible) {
        state.hidden = !visible;
        elements.window.classList.toggle('desktop-lyrics-hidden', !visible);
    }

    function toggleVisibility() {
        setVisibility(state.hidden);
        sendToMain({ type: 'visibility', hidden: state.hidden });
    }

    function closeWindow() {
        // This window is created by the Python bridge with `js_api` pointing
        // at it, and `window.close()` is a no-op under pywebview (the window
        // stayed alive and the bridge kept its reference).  Ask the backend
        // to destroy it; the browser fallback keeps the old behaviour.
        try {
            if (window.pywebview && window.pywebview.api &&
                window.pywebview.api.close_desktop_lyrics) {
                window.pywebview.api.close_desktop_lyrics();
                return;
            }
        } catch (e) { /* fall through */ }
        sendToMain({ type: 'closed' });
        window.close();
    }

    function sendToMain(message) {
        // Try opener.postMessage first
        if (window.opener && !window.opener.closed) {
            try {
                window.opener.postMessage({ type: 'ncm:desktop-lyrics', payload: message }, '*');
            } catch (e) { /* ignore */ }
        }
        // Fallback to localStorage
        try {
            localStorage.setItem('ncm:desktop-lyrics:from-desktop', JSON.stringify({ ...message, ts: Date.now() }));
        } catch (e) { /* ignore */ }
    }

    function showContextMenu(x, y) {
        // Remove existing menu
        const existing = document.getElementById('dl-context-menu');
        if (existing) existing.remove();

        const menu = document.createElement('div');
        menu.id = 'dl-context-menu';
        menu.style.cssText = `
            position: fixed;
            left: ${x}px;
            top: ${y}px;
            background: rgba(30,30,30,0.95);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            padding: 4px;
            z-index: 1000;
            min-width: 160px;
            backdrop-filter: blur(10px);
        `;
        const items = [
            { label: '隐藏歌词', action: () => setVisibility(false) },
            { label: '关闭窗口', action: closeWindow },
            { label: '设置...', action: () => sendToMain({ type: 'openSettings' }) }
        ];
        items.forEach(item => {
            const btn = document.createElement('button');
            btn.textContent = item.label;
            btn.style.cssText = `
                width: 100%;
                padding: 8px 12px;
                background: none;
                border: none;
                color: #fff;
                font: inherit;
                text-align: left;
                border-radius: 4px;
                cursor: pointer;
            `;
            btn.onmouseover = () => btn.style.background = 'rgba(255,255,255,0.1)';
            btn.onmouseout = () => btn.style.background = 'none';
            btn.onclick = () => { item.action(); menu.remove(); };
            menu.appendChild(btn);
        });
        document.body.appendChild(menu);
        document.addEventListener('click', () => menu.remove(), { once: true });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu.remove(); }, { once: true });
    }

    // Handle window close
    window.addEventListener('beforeunload', () => {
        sendToMain({ type: 'closed' });
    });

    // Initialize
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Expose a tiny bridge for the Python host.  The desktop window is
    // created by pywebview's `create_window` (see src/gui/bridge.py), which
    // has no `window.opener` and does not share localStorage with the main
    // window.  `GuiBridge.update_desktop_lyrics` therefore calls
    // `_desktop_lyrics_window.evaluate_js("handleLyricsUpdate(<json>)")`
    // directly; the IIFE keeps the rest of the module private.
    window.handleLyricsUpdate = function (payload) {
        handleMessage(payload);
    };
})();