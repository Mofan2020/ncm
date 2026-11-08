#!/usr/bin/env node
/**
 * Checks for the paging of the subscribed collections (订阅的合集) in
 * src/gui/assets/app.js.
 *
 * Why this is not a pytest test: the logic lives in the frontend, so the
 * only honest way to check it is to load app.js and drive it.
 *
 * What was wrong before: the loader asked for one page and stopped.  The
 * route answers 20 collections per page and this account has 65, so three
 * quarters of them were unreachable -- there was no way to see them at all.
 *
 * And then the tab itself was invisible: the segment row of the favourites
 * page borrowed the collapsible style of the search filter row, so it was
 * folded to zero height with opacity 0.  The list behind it was fine, but
 * there was no way to reach it.
 *
 * What is checked here:
 *
 *   1. 第一页        - a fresh start asks for page 1.
 *   2. 下一页        - "more" asks for page 2.
 *   3. 追加而不是覆盖 - the second page is added to the list.
 *   4. 按钮跟着状态走 - the button shows while there is more, hides at the end.
 *   5. 到底就停      - the last page ends the list, the button goes away.
 *   6. 看得见        - the tab row is not folded away, and its id is used once.
 *
 * Run:  node scripts/check-bili-collection-paging.mjs
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
        const results = {calls: []};

        // A bridge that hands out three collections per page and says
        // there is a third page as well.
        const rows = (page) => [1, 2, 3].map((n) => ({
            id: String(page * 10 + n),
            title: '合集 ' + page + '-' + n,
            count: 5,
            author_name: 'UP',
            cover: '',
        }));
        state.api = {
            get_bili_subscribed_collections: (page) => {
                const number = Number(page) || 1;
                results.calls.push(number);
                return Promise.resolve({
                    success: true,
                    collections: rows(number),
                    has_more: number < 3,
                    page: number,
                    total: 9,
                });
            },
        };

        // ---- 1: the first page -------------------------------------------
        biliFavSeg.collections = [];
        biliFavSeg.collectionPage = 1;
        biliFavSeg.collectionHasMore = false;
        await loadBiliCollections(true);
        results.afterFirst = biliFavSeg.collections.length;
        results.firstPage = biliFavSeg.collectionPage;
        results.firstHasMore = biliFavSeg.collectionHasMore;
        results.firstIds = biliFavSeg.collections.map((item) => item.id);

        // ---- 2 and 3: the next page --------------------------------------
        await loadMoreBiliCollections();
        results.afterSecond = biliFavSeg.collections.length;
        results.secondPage = biliFavSeg.collectionPage;
        results.allIds = biliFavSeg.collections.map((item) => item.id);
        results.secondHasMore = biliFavSeg.collectionHasMore;

        // ---- 5: the last page ends the list ------------------------------
        await loadMoreBiliCollections();
        results.afterThird = biliFavSeg.collections.length;
        results.thirdPage = biliFavSeg.collectionPage;
        results.exhausted = biliFavSeg.collectionHasMore;

        // ---- and one more "more" does not ask the backend ----------------
        results.callsBefore = results.calls.length;
        await loadMoreBiliCollections();
        results.callsAfter = results.calls.length;

        // ---- 4: the button follows the state -----------------------------
        // The button is read after the list ran out, so the "more" state
        // has to be set up again on purpose.
        const more = document.getElementById('btn-bili-collection-more');
        biliFavSeg.loading = false;
        biliFavSeg.collectionHasMore = true;
        renderBiliCollections();
        results.buttonShown = more.hidden;
        biliFavSeg.collectionHasMore = false;
        renderBiliCollections();
        results.buttonHidden = more.hidden;

        globalThis.__results = results;
    })();
`;

// The page loads icons.js before app.js; the render helpers call setIcon,
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

// ---- 1: a fresh start asks for the first page ----------------------------
check('a fresh start asks for page 1', r.calls[0] === 1, JSON.stringify(r.calls));
check('the first page fills the list', r.afterFirst === 3, `${r.afterFirst} rows`);
check('the first page says there is more', r.firstHasMore === true,
    `has_more=${r.firstHasMore}`);
check('the first page is remembered', r.firstPage === 1, `page=${r.firstPage}`);

// ---- 2: more asks for the next page --------------------------------------
check('"more" asks for page 2', r.calls[1] === 2, JSON.stringify(r.calls));
check('the page counter moved on', r.secondPage === 2, `page=${r.secondPage}`);

// ---- 3: it appends -------------------------------------------------------
check('the second page is added, not swapped in',
    r.afterSecond === 6, `${r.afterSecond} rows`);
check('both pages are on screen',
    JSON.stringify(r.allIds) === JSON.stringify(['11', '12', '13', '21', '22', '23']),
    JSON.stringify(r.allIds));

// ---- 4: the button follows the state -------------------------------------
check('the button is shown while there is more', r.buttonShown === false,
    `hidden=${r.buttonShown}`);
check('the button goes away at the end', r.buttonHidden === true,
    `hidden=${r.buttonHidden}`);

// ---- 5: the last page ends the list --------------------------------------
check('the third page is loaded', r.afterThird === 9, `${r.afterThird} rows`);
check('the third page is the last one', r.thirdPage === 3, `page=${r.thirdPage}`);
check('the list is exhausted', r.exhausted === false, `has_more=${r.exhausted}`);
check('"more" at the end does not ask the backend',
    r.callsAfter === r.callsBefore,
    `${r.callsBefore} -> ${r.callsAfter} calls`);

// ---- 6: the tab is reachable ---------------------------------------------
// The row of the segment tabs must not be folded away, and the id of the
// "load more" button must not be used twice: the browser only knows the
// first one, and the second button would be dead.
const cssPath = path.join(root, 'src', 'gui', 'assets', 'style.css');
const htmlPath = path.join(root, 'src', 'gui', 'assets', 'index.html');
const css = fs.readFileSync(cssPath, 'utf8');
const html = fs.readFileSync(htmlPath, 'utf8');

for (const id of ['bili-fav-segments', 'bili-space-segments', 'bili-relations-segments']) {
    const marker = `#${id}`;
    const at = css.indexOf(marker);
    if (at < 0) {
        check(`${id} has a style of its own`, false, 'not in style.css');
        continue;
    }
    const block = css.slice(at, css.indexOf('}', at));
    const folded = /max-height\s*:\s*(0|0px)/.test(block)
        || /opacity\s*:\s*0/.test(block);
    check(`${id} is not folded away`, !folded, block.trim().replace(/\s+/g, ' '));
}

const ids = html.match(/\sid="([^"]+)"/g) || [];
const seen = new Map();
for (const raw of ids) {
    const name = raw.replace(/\sid="([^"]+)"/, '$1');
    seen.set(name, (seen.get(name) || 0) + 1);
}
const twice = [...seen.entries()].filter(([, count]) => count > 1).map(([name]) => name);
check('no id is used twice', twice.length === 0, twice.join(', '));

console.log('');
if (failures.length) {
    console.error(`FAILED: ${failures.length} check(s)`);
    process.exit(1);
}
console.log('all collection paging checks passed');
