#!/usr/bin/env node
/**
 * Checks for the backup window of src/gui/assets/app.js.
 *
 * Why this is not a pytest test: the logic lives in the frontend, so the
 * only honest way to test it is to load app.js and call it.  The script
 * loads the file behind a tiny DOM stub and checks three things:
 *
 *   1. 本机记录的数量  - the summary said "0 restored records" no matter
 *                        what was restored, because the count was read
 *                        from the wrong shape.
 *   2. 列表里的名字    - the rows are clipped from the left, which needs
 *                        the right-to-left direction; that direction
 *                        mirrors the brackets of a Chinese name, so the
 *                        text goes into its own isolate.
 *   3. 按钮的文字      - a button without a word is a button nobody
 *                        dares to press.
 *
 * Run:  node scripts/check-backup-js.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const appPath = path.join(root, 'src', 'gui', 'assets', 'app.js');

/* app.js touches the DOM while booting, which never happens here: the stub
   only has to survive being loaded and being called. */
function makeElement() {
    const store = {
        dataset: {}, style: { setProperty() {}, removeProperty() {} },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        addEventListener() {}, append() {},
        appendChild(c) { this.children.push(c); return c; },
        removeAttribute() {}, setAttribute() {}, querySelector: () => null,
        querySelectorAll: () => [], closest: () => null, scrollTo() {},
        textContent: '', children: [], tagName: 'SPAN',
    };
    return new Proxy(store, {
        get: (target, prop) => (prop in target ? target[prop] : undefined),
        set: (target, prop, value) => { target[prop] = value; return true; },
    });
}

/* The same id has to give the same element back, otherwise a function
   that writes into a button and one that reads it out never meet. */
const elementsById = new Map();
function getElementById(id) {
    if (!elementsById.has(id)) elementsById.set(id, makeElement());
    return elementsById.get(id);
}

const sandbox = {
    console,
    Math, Date, JSON, Object, Array, String, Number, Boolean, isFinite, parseInt, parseFloat,
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
    Image: class { constructor() { this.crossOrigin = null; } },
};

const failures = [];
function check(label, condition, detail) {
    const status = condition ? 'PASS' : 'FAIL';
    console.log(`[${status}] ${label}${detail ? ' -- ' + detail : ''}`);
    if (!condition) failures.push(label);
}

const harness = `
    const results = {};

    // ---- 1. the number of the restored local records ---------------------
    // The export hands over the whole answer, the import hands over the
    // restored part itself.  Both have to be counted.
    results.fromWhole = localRecordCount({
        local_restored: { play_counts: 3, favorites: 2, recent: 1 },
    });
    results.fromInner = localRecordCount({ play_counts: 3, favorites: 2, recent: 1 });
    results.fromEmpty = localRecordCount({});
    results.fromMissing = localRecordCount(undefined);

    // ---- 2. the name in a row --------------------------------------------
    const row = folderRowName('我的收藏（12 个视频）');
    results.rowChild = row.children.length;
    results.rowChildTag = row.children[0] ? row.children[0].tagName : '';
    results.rowChildText = row.children[0] ? row.children[0].textContent : '';

    // ---- 3. the labels of the buttons ------------------------------------
    // Without a video open the buttons get the plain label.  The catalogue
    // is not there yet, so t() hands back the key -- that is what the
    // window shows until the translations arrive.
    biliDetail.bvid = '';
    renderBiliDetailButtons();
    results.likeKey = document.getElementById('btn-bili-like').textContent;
    results.favoriteKey = document.getElementById('btn-bili-favorite').textContent;
    results.coinKey = document.getElementById('btn-bili-coin').textContent;

    // With the catalogue the label is the sentence, not the key.
    state.translations = {
        'bili.like': '点赞',
        'bili.favorite_add': '收藏',
        'bili.coin': '投币',
    };
    renderBiliDetailButtons();
    results.likeLabel = document.getElementById('btn-bili-like').textContent;
    results.favoriteLabel = document.getElementById('btn-bili-favorite').textContent;
    results.coinLabel = document.getElementById('btn-bili-coin').textContent;

    // A video that is open and already liked says so.
    biliDetail.bvid = 'BV1x';
    biliDetail.liked = true;
    biliDetail.favorited = true;
    renderBiliDetailHeader();
    results.likedLabel = document.getElementById('btn-bili-like').textContent;
    results.favoritedLabel = document.getElementById('btn-bili-favorite').textContent;

    globalThis.__results = results;
`;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(appPath, 'utf8') + '\n' + harness, sandbox, { filename: 'app.js' });
const r = sandbox.__results;

check('local records: counted from the whole answer', r.fromWhole === 6, `got ${r.fromWhole}`);
check('local records: counted from the restored part', r.fromInner === 6, `got ${r.fromInner}`);
check('local records: an empty answer counts zero', r.fromEmpty === 0, `got ${r.fromEmpty}`);
check('local records: a missing answer counts zero', r.fromMissing === 0, `got ${r.fromMissing}`);

check('a row puts the text into an isolate', r.rowChild === 1 && r.rowChildTag === 'BDI',
    `${r.rowChild} child(ren), tag ${r.rowChildTag}`);
check('the isolate carries the text', r.rowChildText === '我的收藏（12 个视频）', r.rowChildText);

check('without a catalogue the button shows the key', r.likeKey === 'bili.like', r.likeKey);
check('the like button has a label', r.likeLabel === '点赞', r.likeLabel);
check('the favourite button has a label', r.favoriteLabel === '收藏', r.favoriteLabel);
check('the coin button has a label', r.coinLabel === '投币', r.coinLabel);
check('a liked video says so', r.likedLabel === 'bili.liked', r.likedLabel);
check('a favourited video says so', r.favoritedLabel === 'bili.favorite_done', r.favoritedLabel);

console.log('');
if (failures.length) {
    console.error(`FAILED: ${failures.length} check(s)`);
    process.exit(1);
}
console.log('all backup checks passed');
