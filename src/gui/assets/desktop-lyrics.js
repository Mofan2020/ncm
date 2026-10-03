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
            showTranslation: true
        },
        drag: { active: false, startX: 0, startY: 0, windowX: 0, windowY: 0 },
        hidden: false
    };

    const elements = {
        window: document.getElementById('dl-window'),
        dragHandle: document.getElementById('dl-drag-handle'),
        content: document.getElementById('dl-content'),
        current: document.getElementById('dl-current'),
        translation: document.getElementById('dl-translation')
    };

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
        content.style.fontSize = s.fontSize + 'px';
        content.style.color = s.color;
        content.style.opacity = s.opacity;
        // Update current line color
        if (elements.current) elements.current.style.color = s.color;
        if (elements.translation) elements.translation.style.color = s.color;
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
                updateLyrics(payload.lines, payload.index);
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
        state.lines = lines || [];
        state.currentIndex = index;

        const currentLine = lines[index];
        if (currentLine) {
            elements.current.textContent = currentLine.text || '♪';
            elements.current.classList.add('current');
        } else {
            elements.current.textContent = state.hidden ? '' : '♪';
            elements.current.classList.remove('current');
        }

        // Handle translation
        if (state.settings.showTranslation && currentLine && currentLine.translation) {
            elements.translation.textContent = currentLine.translation;
            elements.translation.hidden = false;
        } else {
            elements.translation.hidden = true;
        }

        // Update previous/next lines opacity
        const allLines = elements.content.querySelectorAll('.lyric-line');
        allLines.forEach((el, i) => {
            el.classList.toggle('current', i === index);
        });
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
})();