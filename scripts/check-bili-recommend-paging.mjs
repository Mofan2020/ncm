#!/usr/bin/env node
/**
 * Checks for the "load more" button of the Bilibili recommendation feed of
 * src/gui/assets/app.js.
 *
 * Why this is not a pytest test: the logic lives in the frontend, so the
 * only honest way to check it is to load app.js and drive it.
 *
 * What went wrong before: the button existed for the partition list only.
 * The recommendation tab had no button at all, and the loader overwrote the
 * list on every call instead of adding to it, so even a button would have
 * shown the same rows again.  Five things are checked here:
 *
 *   1. 第一页          - a fresh start asks for batch 1 with scroll off.
 *   2. 下一页          - "more" asks for batch 2 with scroll on.
 *   3. 追加而不是覆盖   - the second batch is added to the list.
 *   4. 按钮在推荐页可见  - the button is shown on the recommendation tab.
 *   5. 加载完就说加载完了 - an empty batch ends the list, not the first one.
 *
 * Run:  node scripts/check-bili-recommend-paging.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const appPath = path.join(root, 'src', 'gui', 'assets', 'app.js');

/* app.js touches the DOM while booting, which never happens here. */
function makeElement() {
    const store = {
        dataset: {}, style: { setProperty() {}, removeProperty() {} },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        addEventListener() {}, append() {},
        appendChild(c) { this.children.push(c); return c; },
        removeChild() {}, removeAttribute() {}, setAttribute() {},
        getAttribute: () => null,
        querySelector: () => null, querySelectorAll: () => [],
        closest: () => null, scrollTo() {}, replaceChildren() {},
        textContent: '', innerHTML: '', hidden: false, disabled: false,
        children: [], tagName: 'SPAN', value: '',
    };
    return new Proxy(store, {
        get: (target, prop) => (prop in target ? target[prop] : undefined),
        set: (target, prop, value) => { target[prop] = value; return true; },
    });
}

const elementsById = new Map();
function getElementById(id) {
    if (!elementsById.has(id)) elementsById.set(id, makeElement());
    return elementsById.get(id);
}

const sandbox = {
    console,
    // No Promise/Error here on purpose: a context brings its own, and a
    // mix of the two realms makes an await inside the page never settle.
    setTimeout, clearTimeout, setInterval, clearInterval,
    document: {
        getElementById,
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: (tag) => {
            const el = makeElement();
            el.tagName = String(tag || 'DIV').toUpperCase();
            return el;
        },
        // setIcon() builds an SVG node through the namespaced factory.
        createElementNS: (ns, tag) => {
            const el = makeElement();
            el.tagName = String(tag || 'svg').toUpperCase();
            el.namespaceURI = ns;
            return el;
        },
        createDocumentFragment: () => makeElement(),
        addEventListener() {},
        documentElement: {
            dataset: {}, style: { setProperty() {}, removeProperty() {} },
            setAttribute() {}, lang: '',
        },
        body: { classList: { add() {}, remove() {}, toggle() {} } },
    },
    window: {
        addEventListener() {},
        matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    },
    // renderBiliResults() asks whether the machine is online.
    navigator: { onLine: true, language: 'zh-CN', userAgent: 'node' },
    localStorage: {
        getItem: () => null, setItem() {}, removeItem() {}, clear() {},
    },
    Image: class { constructor() { this.crossOrigin = null; } },
};

const failures = [];
function check(label, condition, detail) {
    const status = condition ? 'PASS' : 'FAIL';
    console.log(`[${status}] ${label}${detail ? ' -- ' + detail : ''}`);
    if (!condition) failures.push(label);
}

/* The harness runs inside the same context as app.js, so it can call the
   functions that are not on window.  It is async because the loader is. */
const harness = `
    globalThis.__done = (async () => {
        const results = {calls: [], batches: []};

        // A bridge that hands out two fresh videos per batch.
        state.api = {
            get_bilibili_recommend: (pageSize, freshIndex, scroll) => {
                results.calls.push({pageSize, freshIndex, scroll});
                const rows = [
                    {bvid: 'BV' + freshIndex + 'a', name: 'video a', artists: ''},
                    {bvid: 'BV' + freshIndex + 'b', name: 'video b', artists: ''},
                ];
                results.batches.push(rows.length);
                return Promise.resolve({success: true, tracks: rows});
            },
        };

        // ---- 1 and 2: the two calls -------------------------------------
        biliSection.tab = 'recommend';
        biliSection.items = [];
        await loadBiliRecommend(true);
        results.afterFirst = biliSection.items.length;
        await loadBiliRecommend(false);
        results.afterSecond = biliSection.items.length;
        results.bvids = biliSection.items.map((item) => item.bvid);
        results.exhaustedAfterFull = biliSection.exhausted;

        // ---- 4: the button on every tab ---------------------------------
        const more = document.getElementById('btn-bili-more');
        biliSection.tab = 'recommend';
        biliSection.loading = false;
        biliSection.exhausted = false;
        renderBiliResults();
        results.recommendHidden = more.hidden;
        biliSection.tab = 'hot';
        renderBiliResults();
        results.hotHidden = more.hidden;
        biliSection.tab = 'favorites';
        renderBiliResults();
        results.favouritesHidden = more.hidden;

        // ---- 5: an empty batch ends the list ----------------------------
        biliSection.tab = 'recommend';
        biliSection.items = [];
        state.api = {
            get_bilibili_recommend: () => Promise.resolve({success: true, tracks: []}),
        };
        await loadBiliRecommend(true);
        results.exhaustedOnEmpty = biliSection.exhausted;
        results.itemsOnEmpty = biliSection.items.length;

        // ---- 3 again: "more" on the recommendation tab appends ----------
        // Reset and walk the real path the button takes.
        biliSection.items = [];
        biliSection.exhausted = false;
        biliSection.loading = false;
        biliSection.recommendPage = 0;
        state.api = {
            get_bilibili_recommend: (pageSize, freshIndex, scroll) => {
                results.moreCalls.push({freshIndex, scroll});
                return Promise.resolve({success: true, tracks: [
                    {bvid: 'BV' + freshIndex + 'a', name: 'x', artists: ''},
                    {bvid: 'BV' + freshIndex + 'b', name: 'x', artists: ''},
                ]});
            },
        };
        results.moreCalls = [];
        biliSection.tab = 'recommend';
        await loadBiliRecommend(true);
        await loadMoreBili();
        results.afterMore = biliSection.items.length;
        results.morePage = biliSection.page;

        globalThis.__results = results;
    })();
`;

// The page loads icons.js before app.js; renderBiliResults() calls setIcon,
// so the check needs the same two files in the same order.
const iconsPath = path.join(root, 'src', 'gui', 'assets', 'icons.js');

vm.createContext(sandbox);
vm.runInContext(
    fs.readFileSync(iconsPath, 'utf8') + '\n'
    + fs.readFileSync(appPath, 'utf8') + '\n' + harness,
    sandbox,
    { filename: 'app.js' },
);

// Wait for the work, then read what it wrote: the async function itself
// resolves to nothing, its answers are on the context.
try {
    await sandbox.__done;
} catch (err) {
    console.error('the harness threw:', err && err.message);
    console.error(err && err.stack ? err.stack.split('\n').slice(0, 8).join('\n') : '');
    process.exit(1);
}
const r = sandbox.__results;
if (!r) {
    console.error('the harness did not leave any results');
    process.exit(1);
}

// ---- 1: a fresh start asks for the first batch -------------------------
const first = r.calls[0] || {};
check('a fresh start asks for batch 1', first.freshIndex === 1, JSON.stringify(first));
check('a fresh start asks for the home feed, not a scroll',
    first.scroll === false, JSON.stringify(first));
check('the batch size is passed on', first.pageSize === 24, JSON.stringify(first));

// ---- 2: more asks for the next batch -----------------------------------
const second = r.calls[1] || {};
check('"more" asks for batch 2', second.freshIndex === 2, JSON.stringify(second));
check('"more" asks for a scroll', second.scroll === true, JSON.stringify(second));

// ---- 3: it appends -----------------------------------------------------
check('the first batch fills the list', r.afterFirst === 2, `${r.afterFirst} rows`);
check('the second batch is added, not swapped in',
    r.afterSecond === 4, `${r.afterSecond} rows`);
check('both batches are on screen',
    JSON.stringify(r.bvids) === JSON.stringify(['BV1a', 'BV1b', 'BV2a', 'BV2b']),
    JSON.stringify(r.bvids));
check('a full batch does not end the list', r.exhaustedAfterFull === false,
    `exhausted=${r.exhaustedAfterFull}`);
check('"more" appends through the real button path', r.afterMore === 4, `${r.afterMore} rows`);
check('"more" does not touch the partition page', r.morePage === 1, `page=${r.morePage}`);

// ---- 4: the button lives on the recommendation tab ---------------------
check('the button is shown on the recommendation tab', r.recommendHidden === false,
    `hidden=${r.recommendHidden}`);
check('the button is still shown on the partition tab', r.hotHidden === false,
    `hidden=${r.hotHidden}`);
check('the button is hidden on the favourites tab', r.favouritesHidden === true,
    `hidden=${r.favouritesHidden}`);

// ---- 5: an empty batch is the end --------------------------------------
check('an empty batch ends the list', r.exhaustedOnEmpty === true,
    `exhausted=${r.exhaustedOnEmpty}`);
check('an empty batch leaves the list empty', r.itemsOnEmpty === 0, `${r.itemsOnEmpty} rows`);

console.log('');
if (failures.length) {
    console.error(`FAILED: ${failures.length} check(s)`);
    process.exit(1);
}
console.log('all recommendation paging checks passed');
