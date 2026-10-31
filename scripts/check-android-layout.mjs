#!/usr/bin/env node
/**
 * Layout check for the phone shell (android/web).
 *
 * Why this is not a pytest test: the layout lives in the browser, so the
 * only honest way to test it is to load the page and measure it.  The
 * script loads the real android/web/index.html in Chromium and checks the
 * geometry of the shell:
 *
 *   1. the shell fills the viewport;
 *   2. the content area has a height (the pages need one to be visible);
 *   3. the tab bar sits at the bottom of the screen;
 *   4. the page does not scroll as a whole -- the content scrolls inside
 *      the content area, not the window;
 *   5. every tab really shows its page, and that page scrolls inside
 *      itself.
 *
 * The whole thing is run twice: once as it is, and once against a
 * "legacy engine" stylesheet, in which the units and the properties that
 * only newer browsers know are removed.  The phone has to look the same
 * on both, because the WebView on an older Android is not updated with
 * the system and stays where it is for years:
 *
 *   dvh / svh / lvh  Chrome 108+ (2022)
 *   inset            Chrome 87+  (2020)
 *   aspect-ratio     Chrome 88+  (2021)
 *   min() / max()    Chrome 79+
 *
 * Run:  node scripts/check-android-layout.mjs
 *
 * Needs Playwright:  npm i -g playwright && npx playwright install chromium
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

/* Playwright is not a dependency of this project (the CI has no node at
   all), so it is usually installed globally.  An ESM import does not read
   NODE_PATH, so the global directories are tried by hand. */
let chromium;
try {
    ({ chromium } = await import('playwright'));
} catch (err) {
    const require = createRequire(import.meta.url);
    const roots = [
        process.env.PLAYWRIGHT_ROOT,
        '/opt/homebrew/lib/node_modules',
        '/usr/local/lib/node_modules',
        path.join(os.homedir(), '.npm-global/lib/node_modules'),
    ].filter(Boolean);
    for (const dir of roots) {
        const candidate = path.join(dir, 'playwright');
        if (!fs.existsSync(candidate)) continue;
        try {
            ({ chromium } = require(candidate));
            break;
        } catch (err) { /* try the next one */ }
    }
}
if (!chromium) {
    console.error('playwright is not installed: npm i -g playwright');
    console.error('  and then: npx playwright install chromium');
    process.exit(2);
}

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const web = path.join(root, 'android', 'web');

/* An optional stylesheet lets the mutation check run this script against
   a deliberately broken copy of the stylesheet. */
const cssPath = process.argv[2]
    ? path.resolve(process.argv[2])
    : path.join(web, 'style.css');

/* The phone is a 411x891 screen (a Pixel-ish viewport). */
const VIEWPORT = { width: 411, height: 891 };

/* A stylesheet as an engine that knows none of the modern units sees it.
   The declarations are removed, not renamed: an unknown unit makes the
   browser drop the declaration, which is what happens on the phone.

   Two things are easy to get wrong here:

   - a declaration pattern has to stop at ``{`` and ``}`` as well as at
     ``;``, or it swallows the whole block that follows it and the
     stylesheet stops parsing half way down;
   - the ``@supports not (aspect-ratio: 1 / 1)`` condition is *not* a
     declaration.  It has to be kept, and turned inside out: a modern
     Chromium knows aspect-ratio, so the condition is false and the
     fallback never runs -- while on the phone it is true and the
     fallback is exactly what keeps the cover square. */
function asLegacyEngine(css) {
    return css
        .replace(/(\d*\.?\d+)(dvh|svh|lvh)\b/g, '$1qqh')
        .replace(/@supports not \(aspect-ratio: 1 \/ 1\)/g,
                 '@supports (aspect-ratio: 1 / 1)')
        .replace(/aspect-ratio:\s*[^;{}]+;/g, '')
        .replace(/inset:\s*[^;{}]+;/g, '')
        .replace(/\b(min|max)\(/g, '$1qq(');
}

/* The rewrite must not change the stylesheet apart from the features it
   is simulating.  A mangled stylesheet would silently drop half the
   rules, and the checks below would then pass for the wrong reason. */
function assertIntact(original, rewritten) {
    const braces = (css) => (css.match(/\{/g) || []).length
        - (css.match(/\}/g) || []).length;
    if (braces(original) !== braces(rewritten)) {
        throw new Error('the rewrite unbalanced the braces');
    }
    for (const canary of [
        '.tabbar {',
        'height: calc(var(--tabbar-h) + var(--safe-bottom));',
        'min-height: 0;',
        'position: fixed;',
        'overflow-y: auto;',
    ]) {
        if (original.includes(canary) && !rewritten.includes(canary)) {
            throw new Error(`the rewrite dropped "${canary}"`);
        }
    }
}

/* The bridge has to exist before the page boots.  Without it the page
   waits for it, gives up after eight seconds and never wires its buttons
   -- so the tabs would look dead for reasons that have nothing to do with
   the layout. */
function stubBridge() {
    const rows = Array.from({ length: 40 }, (_, i) => ({
        id: String(9000 + i),
        name: '测试歌曲 ' + (i + 1),
        artist: '测试歌手',
        album: '测试专辑',
        duration: 200 + i,
    }));
    const answer = (value) => async () => value;
    window.pywebview = {
        api: {
            get_settings: answer({ ui: { theme: 'dark', language: 'zh' } }),
            get_translations: answer({}),
            get_login_status: answer({
                success: true, user: { nickname: '测试', user_id: 1 },
            }),
            get_home_fm: answer({ success: true, data: rows.slice(0, 10) }),
            get_new_songs: answer({ success: true, data: rows.slice(10, 20) }),
            get_recommend_playlists: answer({ success: true, data: [] }),
            get_top_lists: answer({ success: true, data: [] }),
            get_mvs: answer({ success: true, data: [] }),
            get_search_suggest: answer({ success: true, data: [] }),
            get_user_playlists: answer({ success: true, data: [] }),
            get_liked_playlists: answer({ success: true, data: [] }),
            get_local_music: answer({ success: true, data: [] }),
            get_download_stats: answer({ success: true, finished: 0, total: 0 }),
            get_download_queue: answer({ success: true, data: [] }),
            get_downloaded: answer({ success: true, data: [] }),
        },
    };
}

async function measure(page, tab) {
    return page.evaluate((name) => {
        const box = (sel) => {
            const node = document.querySelector(sel);
            if (!node) return null;
            const r = node.getBoundingClientRect();
            return {
                top: Math.round(r.top),
                bottom: Math.round(r.bottom),
                height: Math.round(r.height),
            };
        };
        const view = document.querySelector(`#view-${name}`);
        const cs = view ? getComputedStyle(view) : null;
        return {
            vh: window.innerHeight,
            app: box('.app'),
            views: box('.views'),
            tabbar: box('.tabbar'),
            bodyScroll: document.body.scrollHeight,
            view: view ? {
                visible: cs.display !== 'none' && cs.visibility !== 'hidden',
                height: Math.round(view.getBoundingClientRect().height),
                overflowY: cs.overflowY,
            } : null,
        };
    }, tab);
}

const browser = await chromium.launch();
const failures = [];

function check(label, ok, detail) {
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ' -- ' + detail : ''}`);
    if (!ok) failures.push(label);
}

const tabbarHeights = {};
for (const [name, mutate] of [
    ['as it is', (css) => css],
    ['as an old WebView sees it', asLegacyEngine],
]) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ncm-layout-'));
    fs.copyFileSync(path.join(web, 'index.html'), path.join(dir, 'index.html'));
    fs.copyFileSync(path.join(web, 'app.js'), path.join(dir, 'app.js'));
    const original = fs.readFileSync(cssPath, 'utf8');
    const rewritten = mutate(original);
    assertIntact(original, rewritten);
    fs.writeFileSync(path.join(dir, 'style.css'), rewritten);

    console.log(`\n[${name}]`);
    const page = await browser.newPage({ viewport: VIEWPORT });
    await page.addInitScript(stubBridge);
    await page.goto('file://' + path.join(dir, 'index.html'));
    await page.waitForTimeout(600);

    const m = await measure(page, 'home');
    console.log(`  app    ${JSON.stringify(m.app)}`);
    console.log(`  views  ${JSON.stringify(m.views)}`);
    console.log(`  tabbar ${JSON.stringify(m.tabbar)}`);

    check('the shell fills the viewport', m.app && m.app.height >= m.vh - 1,
        `app is ${m.app && m.app.height}px, the viewport is ${m.vh}px`);
    check('the content area has a height', m.views && m.views.height > 100,
        `views is ${m.views && m.views.height}px`);
    check('the tab bar is at the bottom',
        m.tabbar && Math.abs(m.tabbar.bottom - m.vh) <= 1,
        `tab bar ends at ${m.tabbar && m.tabbar.bottom}px`);
    check('the window itself does not scroll', m.bodyScroll <= m.vh + 1,
        `the document is ${m.bodyScroll}px tall`);
    tabbarHeights[name] = m.tabbar && m.tabbar.height;

    // The overlays.  The full-screen player lives inside the shell, which
    // is position: fixed, so the overlay has to be measured rather than
    // assumed to cover the screen.
    for (const [label, open] of [
        ['the full-screen player',
            'state.queue = [{ id: 1, name: "x" }]; state.index = 0; openPlayer()'],
        ['the sheet', "openSheet('测试', document.createElement('div'))"],
        ['the modal', "openModal({ title: '测试', body: document.createElement('div'), actions: [] })"],
    ]) {
        await page.evaluate((code) => { eval(code); }, open);
        await page.waitForTimeout(400);
        const overlay = await page.evaluate((which) => {
            const sel = which === 'the full-screen player' ? '#view-player'
                : which === 'the sheet' ? '.sheet-host' : '.modal-host';
            const node = document.querySelector(sel);
            if (!node) return null;
            const r = node.getBoundingClientRect();
            const cs = getComputedStyle(node);
            return {
                display: cs.display,
                width: Math.round(r.width),
                height: Math.round(r.height),
                top: Math.round(r.top),
                left: Math.round(r.left),
                zIndex: cs.zIndex,
            };
        }, label);
        check(`${label} covers the screen`,
            overlay && overlay.display !== 'none'
            && overlay.height >= VIEWPORT.height - 1
            && overlay.width >= VIEWPORT.width - 1,
            JSON.stringify(overlay));
        await page.evaluate(() => {
            closeSheet(); closeModal(); closePlayer();
        });
        await page.waitForTimeout(100);
    }

    for (const tab of ['search', 'library', 'downloads', 'me', 'home']) {
        await page.evaluate((t) => {
            const button = document.querySelector(`.tab[data-tab="${t}"]`);
            if (button) button.click();
        }, tab);
        await page.waitForTimeout(120);
        const t = await measure(page, tab);
        check(`the ${tab} page is shown`,
            t.view && t.view.visible && t.view.height > 100,
            `height ${t.view && t.view.height}px`);
        check(`the ${tab} page scrolls inside itself`,
            t.view && t.view.overflowY === 'auto');
    }

    await page.close();
    fs.rmSync(dir, { recursive: true, force: true });
}
await browser.close();

/* The two variants have to agree on the size of the tab bar.  A rewrite
   that quietly dropped a rule would still leave the bar at the bottom,
   so only the comparison catches that. */
const heights = Object.values(tabbarHeights);
if (heights.length === 2 && heights[0] !== heights[1]) {
    console.error(`\nFAIL  the tab bar is a different height on the two `
        + `engines: ${JSON.stringify(tabbarHeights)}`);
    failures.push('the tab bar is the same height on both engines');
}

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed: ${failures.join('; ')}`);
    process.exit(1);
}
console.log('\nall checks passed');
