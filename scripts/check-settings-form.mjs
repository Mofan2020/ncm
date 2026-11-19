#!/usr/bin/env node
/**
 * The settings form has to show what the server sent.
 *
 * This is the half of "the setting does not save" that only shows up in
 * the browser: the backend answers with the real values, the form fills
 * itself, and then the user changes something and saves.  If the form
 * fills itself with the HTML defaults instead, the save writes the
 * defaults back and the user's change is lost -- and nothing complains.
 *
 * The functions are taken **out of the real app.js**, not re-written
 * here: a mock of the form filling would prove nothing about the form
 * filling.
 *
 * Run:  node scripts/check-settings-form.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
// The mutation check runs this against a deliberately broken copy of
// app.js, so the source can be pointed at another file.
const appPath = process.env.NCM_APP_JS
    ? path.resolve(process.env.NCM_APP_JS)
    : path.join(root, 'src', 'gui', 'assets', 'app.js');
const app = fs.readFileSync(appPath, 'utf8');

/** Pull a function out of the source, braces balanced. */
function extract(name) {
    // `async function foo(` and `function foo(` are both possible.
    const asyncStart = app.indexOf(`async function ${name}(`);
    const plainStart = app.indexOf(`function ${name}(`);
    const start = asyncStart >= 0 ? asyncStart : plainStart;
    if (start < 0) throw new Error(`${name} is not in app.js`);
    let depth = 0;
    let seen = false;
    for (let i = app.indexOf('{', start); i < app.length; i += 1) {
        const ch = app[i];
        if (ch === '{') { depth += 1; seen = true; }
        else if (ch === '}') {
            depth -= 1;
            if (seen && depth === 0) return app.slice(start, i + 1);
        }
    }
    throw new Error(`the end of ${name} was not found`);
}

/* ------------------------------------------------------------------- DOM */

/** The controls the form fills.  Only the ones we check are needed. */
const elements = new Map();

function element(id, kind) {
    const el = {
        id,
        value: kind === 'select' ? '' : '',
        checked: false,
        textContent: '',
        innerHTML: '',
        options: [],
        addEventListener() {},
        appendChild() {},
        setAttribute() {},
        removeAttribute() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    };
    elements.set(id, el);
    return el;
}

// The controls the check cares about.  Their HTML defaults are what the
// user would wrongly get if the form did not fill itself.
element('update-use-sdm-check', 'check');
element('update-threads-select', 'select');
element('pixiv-nsfw-mode', 'select');
element('pixiv-include-r18', 'check');
element('pixiv-hide-ai', 'check');
element('pixiv-quality', 'select');
element('pixiv-bm-private', 'check');
element('pixiv-page-size', 'select');
element('pixiv-template', 'input');
element('pixiv-download-dir', 'input');
// These two are read straight off the element, not through setValue().
element('download-dir-input', 'input');
element('background-input', 'input');

// The defaults the HTML ships with -- what the form shows before it is
// filled, and what it would save back if it never filled itself.
elements.get('update-use-sdm-check').checked = true;
elements.get('update-threads-select').value = '8';
elements.get('pixiv-nsfw-mode').value = 'hide';
elements.get('pixiv-include-r18').checked = false;
elements.get('pixiv-page-size').value = '48';

const document = {
    activeElement: null,
    getElementById(id) { return elements.get(id) || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    createElement() { return element('created-' + Math.random(), 'input'); },
    addEventListener() {},
    removeEventListener() {},
};

const calls = [];
const stub = (name) => (...args) => { calls.push([name, ...args]); };

const sandbox = {
    document,
    console,
    window: {},
    Math, JSON, Number, String, Boolean, Object, Array,
    setTimeout, clearTimeout,
    state: {
        settings: null,
        player: { lyricsScrollIdleSeconds: 4 },
    },
    // The helpers fillSettingsForm() leans on.  They are recorded, not
    // performed: the check is about the values the form ends up with.
    fillBangumiSettings: stub('fillBangumiSettings'),
    updateCrossfadeLabel: stub('updateCrossfadeLabel'),
    updateLyricsScrollIdleLabel: stub('updateLyricsScrollIdleLabel'),
    updateBiliSettingsLabels: stub('updateBiliSettingsLabels'),
    renderBiliSettingsAccount: stub('renderBiliSettingsAccount'),
    updateDesktopLyricsOpacityLabel: stub('updateDesktopLyricsOpacityLabel'),
    updateRangeLabels: stub('updateRangeLabels'),
    fillLanguageSelect: stub('fillLanguageSelect'),
    renderFolderList: stub('renderFolderList'),
    setTheme: stub('setTheme'),
    applyAppearance: stub('applyAppearance'),
    player: { lyricsScrollIdleSeconds: 4 },
};

vm.createContext(sandbox);
const code = [
    extract('setValue'),
    extract('setChecked'),
    extract('fillSettingsForm'),
    'globalThis.__fillSettingsForm = fillSettingsForm;',
].join('\n');
vm.runInContext(code, sandbox, { filename: appPath });

const fill = sandbox.__fillSettingsForm;

/* ----------------------------------------------------------------- tests */

const results = {};

function check(name, value) {
    results[name] = value === true;
}

/**
 * The answer the backend sends.  The values are deliberately **not** the
 * HTML defaults: if the form showed the defaults, the user would think
 * the setting never saved.
 */
const answer = {
    download: {
        quality: 'standard', hires_enabled: false, max_concurrent: 4,
        overwrite: true, download_lyrics: true, lyrics_translation: false,
        download_dir: '/tmp/music',
    },
    ui: {
        theme: 'dark', check_updates_on_start: true, auto_install_updates: false,
        update_include_prerelease: false, update_skip_ssl_verify: false,
        // The two settings that could not be saved:
        update_use_sdm: false,
        update_threads: 16,
        desktop_lyrics_font_size: 18, desktop_lyrics_color: '#ffffff',
        desktop_lyrics_opacity: 0.9, desktop_lyrics_show_translation: false,
        desktop_lyrics_background: 'dark', desktop_lyrics_text_style: 'white',
        background_image: '', background_blur: 10, background_dim: 20,
        glass: true, accent_from_background: false, language: 'zh_cn',
    },
    auth: { remember_login: true },
    playback: {
        prefer_online: true, online_quality: 'standard',
        resume_playback: true, report_play_count: false, show_translation: false,
        crossfade_duration: 3, lyrics_scroll_idle_seconds: 4,
        exclusive_mode: 'off', sleep_timer_minutes: 0,
    },
    debug: false,
    bili: {
        quality: 0, danmaku_enabled: true, danmaku_font_size: 18,
        danmaku_opacity: 0.9, download_audio: true, merge_audio: false,
        separate_player_window: false, report_playback: false,
        resume_playback: false, download_dir: '', interest_tags: [],
        blocked_tags: [],
    },
    // The Pixiv section.  Without it the form has nothing to fill and
    // saves the defaults back.
    pixiv: {
        nsfw_mode: 'blur',
        include_r18: true,
        hide_ai: true,
        quality: 'original',
        bookmark_private: true,
        template: 'https://example.test/{id}',
        page_size: 30,
        download_dir: '/tmp/pixiv',
    },
    bangumi: {},
};

sandbox.state.settings = answer;
fill();

// ---- the update settings -------------------------------------------------
check('the "borrow SDM" box shows the value the server sent',
    elements.get('update-use-sdm-check').checked === false);
check('the thread count shows the value the server sent',
    elements.get('update-threads-select').value === '16');

// ---- the Pixiv settings ---------------------------------------------------
check('the adult content mode shows the value the server sent',
    elements.get('pixiv-nsfw-mode').value === 'blur');
check('the R-18 switch shows the value the server sent',
    elements.get('pixiv-include-r18').checked === true);
check('the "hide AI" switch shows the value the server sent',
    elements.get('pixiv-hide-ai').checked === true);
check('the bookmark privacy switch shows the value the server sent',
    elements.get('pixiv-bm-private').checked === true);
check('the page size shows the value the server sent',
    elements.get('pixiv-page-size').value === '30');
check('the template shows the value the server sent',
    elements.get('pixiv-template').value === 'https://example.test/{id}');

/* ------------------------------------------------------- the save path */

/// The payload the form sends to the backend.
let sent = null;
sandbox.state.api = {
    async update_settings(category, values) {
        sent = sent || {};
        sent[category] = values;
        return { success: true, settings: null };
    },
};

/// The controls saveSettings() reads that are not in the check above.
// (Their values are set further down, next to the save path.)

// The two settings the user reported, in the state the user left them in.
elements.get('update-use-sdm-check').checked = false;
elements.get('update-threads-select').value = '16';
elements.get('pixiv-include-r18').checked = true;
elements.get('pixiv-nsfw-mode').value = 'blur';

// saveSettings() reads a few more things from the page.
sandbox.splitTagWords = (text) => (text ? text.split(',') : []);
sandbox.bangumiSettingsState = { mirrors: [], sites: [] };
sandbox.showToast = stub('showToast');
sandbox.applyPlaybackSettings = stub('applyPlaybackSettings');
sandbox.applyAppearance = stub('applyAppearance');
sandbox.player = { mode: 'list', volume: 100 };
sandbox.t = (key) => key;
sandbox.closeModal = stub('closeModal');
sandbox.openModal = stub('openModal');

/// Every control saveSettings() reads, by name.  A missing one throws
/// right here -- that is the point: the form and the code have to agree.
const SAVE_READS = [
    'language-select', 'theme-select', 'background-blur', 'background-dim',
    'glass-check', 'accent-check', 'desktop-lyrics-font-size',
    'desktop-lyrics-color', 'desktop-lyrics-opacity',
    'desktop-lyrics-translation-check', 'desktop-lyrics-background',
    'desktop-lyrics-text-style', 'update-on-start-check',
    'update-auto-install-check', 'update-prerelease-check',
    'update-skip-ssl-check', 'remember-login-check', 'debug-check',
    'default-quality-select', 'max-concurrent-select',
    'overwrite-files-check', 'download-lyrics-check',
    'lyrics-translation-check', 'download-dir-input', 'priority-select',
    'online-quality-select', 'resume-playback-check', 'report-play-check',
    'show-translation-check', 'crossfade-duration', 'lyrics-scroll-idle',
    'exclusive-mode-select', 'sleep-timer-select', 'bili-quality-select',
    'bili-danmaku-check', 'bili-danmaku-size', 'bili-danmaku-opacity',
    'bili-download-audio-check', 'bili-merge-audio-check',
    'bili-separate-window-check', 'bili-report-playback-check',
    'bili-resume-playback-check', 'bili-download-dir', 'bili-interest-tags',
    'bili-blocked-tags', 'bili-hires-check',
    'pixiv-nsfw-mode', 'pixiv-include-r18',
    'pixiv-hide-ai', 'pixiv-quality', 'pixiv-bm-private', 'pixiv-template',
    'pixiv-page-size',
];
for (const id of SAVE_READS) {
    if (!elements.has(id)) element(id, id.endsWith('-check') ? 'check' : 'input');
}
elements.get('update-use-sdm-check').checked = false;
elements.get('update-threads-select').value = '16';
elements.get('pixiv-include-r18').checked = true;
elements.get('pixiv-nsfw-mode').value = 'blur';
elements.get('pixiv-quality').value = 'original';
elements.get('pixiv-page-size').value = '30';
elements.get('pixiv-template').value = 'https://example.test/{id}';
elements.get('pixiv-hide-ai').checked = true;
elements.get('pixiv-bm-private').checked = true;
// The Hi-Res switch is a boolean, and a boolean that is never read on the
// way out is a switch that does nothing.
elements.get('bili-hires-check').checked = true;

vm.runInContext(
    extract('splitTagWords') + '\n' + extract('saveSettings') +
    '\nglobalThis.__saveSettings = saveSettings;',
    sandbox, { filename: appPath });

await sandbox.__saveSettings();

check('the update settings are sent to the backend',
    sent && sent.ui && sent.ui.update_use_sdm === false &&
    sent.ui.update_threads === 16);
check('the Pixiv settings are sent to the backend',
    sent && sent.pixiv && sent.pixiv.include_r18 === true &&
    sent.pixiv.nsfw_mode === 'blur');
check('the Pixiv section is sent at all',
    sent && Object.prototype.hasOwnProperty.call(sent, 'pixiv'));
check('the Hi-Res switch is sent to the backend',
    sent && sent.bili && sent.bili.hires_enabled === true);

/* --------------------------------------------- opening the modal again */

/// Every time the modal opens it has to ask the backend again: the
/// controls start from the HTML defaults, and filling them without
/// asking means the user sees the leftovers of the last time and saves
/// them back.
let pulls = 0;
sandbox.state.api.get_settings = async () => {
    pulls += 1;
    return answer;
};
vm.runInContext(
    extract('loadSettings') + '\n' + extract('openSettingsModal') +
    '\nglobalThis.__openSettingsModal = openSettingsModal;',
    sandbox, { filename: appPath });
await sandbox.__openSettingsModal();
check('opening the settings modal asks the backend again', pulls === 1);

/* ------------------------------------------------------------------ report */

let failed = 0;
for (const [name, ok] of Object.entries(results)) {
    console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}`);
    if (!ok) failed += 1;
}
if (failed) {
    console.error(`\n${failed} check(s) failed`);
    process.exit(1);
}
console.log('\nall checks passed');
