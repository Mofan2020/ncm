#!/usr/bin/env node
/**
 * Statistical check for the player's queue order in src/gui/assets/app.js.
 *
 * Why this is not a pytest test: the play-mode logic lives in the frontend, so
 * the only honest way to test it is to load app.js and call it.  This script
 * loads the file behind a tiny DOM stub, drives `nextIndex()` many times and
 * checks the contract:
 *
 *   1. 单曲循环 (single)  - automatic advance repeats the current track,
 *                           a manual "next" still moves on.
 *   2. 列表循环 (list)    - advances by one and wraps around.
 *   3. 随机播放 (shuffle) - uniform over every other track, never the current
 *                           one, and NOT influenced by play counts.
 *
 * Run:  node scripts/check-player-js.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const appPath = path.join(root, 'src', 'gui', 'assets', 'app.js');

/* app.js touches the DOM while booting, which never happens here: the stub only
   has to survive being loaded and being called. */
function makeElement() {
    const store = {
        dataset: {}, style: { setProperty() {}, removeProperty() {} },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        addEventListener() {}, appendChild(c) { return c; }, append() {},
        removeAttribute() {}, setAttribute() {}, querySelector: () => null,
        querySelectorAll: () => [], closest: () => null, scrollTo() {},
    };
    return new Proxy(store, {
        get: (target, prop) => (prop in target ? target[prop] : undefined),
        set: (target, prop, value) => { target[prop] = value; return true; },
    });
}

const sandbox = {
    console,
    Math, Date, JSON, Object, Array, String, Number, Boolean, isFinite, parseInt, parseFloat,
    setTimeout, clearTimeout, setInterval, clearInterval,
    document: {
        getElementById: () => makeElement(),
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: () => makeElement(),
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
    const TRACKS = 6;
    const ITERATIONS = 240000;
    const results = {};

    function setQueue(count) {
        player.queue = Array.from({ length: count }, (_, i) => ({
            name: 'track' + i, key: 'online:' + i, source: 'online',
        }));
        player.index = 0;
    }

    // ---- 1. single / list -------------------------------------------------
    setQueue(TRACKS);
    player.mode = 'single';
    results.singleAutoRepeats = nextIndex(true) === player.index;
    results.singleManualAdvances = nextIndex(false) === (player.index + 1) % TRACKS;

    setQueue(TRACKS);
    player.mode = 'list';
    player.index = TRACKS - 1;
    results.listWraps = nextIndex(true) === 0;
    player.index = 2;
    results.listSteps = nextIndex(true) === 3;
    results.previousWraps = previousIndex() === 1;
    player.index = 0;
    results.previousFromFirst = previousIndex() === TRACKS - 1;

    // ---- 2. shuffle: uniform, never the current track ---------------------
    setQueue(TRACKS);
    player.mode = 'shuffle';
    player.counts = {};
    player.queue.forEach((track, i) => { player.counts[track.key] = i * 500 + 1; });

    const histogram = new Array(TRACKS).fill(0);
    let repeats = 0;
    for (let i = 0; i < ITERATIONS; i += 1) {
        const next = nextIndex(true);
        if (next === player.index) repeats += 1;
        if (next < 0 || next >= TRACKS) { repeats += 1; continue; }
        histogram[next] += 1;
        player.index = next;
    }
    // Every index is equally likely to be the destination; the previous track is
    // simply never picked again, so the share of each index is ITERATIONS/TRACKS.
    const expected = ITERATIONS / TRACKS;
    const maxDeviation = Math.max(...histogram.map((value) => Math.abs(value - expected) / expected));

    // A second run without any play counts must behave the same way.
    player.counts = {};
    const histogram2 = new Array(TRACKS).fill(0);
    for (let i = 0; i < ITERATIONS; i += 1) {
        const next = nextIndex(true);
        histogram2[next] += 1;
        player.index = next;
    }
    const maxDeviation2 = Math.max(...histogram2.map((value) => Math.abs(value - expected) / expected));

    results.shuffleNeverRepeats = repeats === 0;
    results.shuffleMaxDeviation = Math.max(maxDeviation, maxDeviation2);
    results.shuffleHistogram = histogram;
    results.shuffleHistogramNoCounts = histogram2;
    results.expected = expected;

    // ---- 3. a one-track queue must not spin forever -----------------------
    setQueue(1);
    results.shuffleSingleTrack = nextIndex(true) === 0;

    globalThis.__results = results;
`;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(appPath, 'utf8') + '\n' + harness, sandbox, { filename: 'app.js' });
const r = sandbox.__results;

check('single mode: automatic advance repeats the current track', r.singleAutoRepeats);
check('single mode: manual next still moves on', r.singleManualAdvances);
check('list mode: wraps around at the end', r.listWraps);
check('list mode: steps forward by one', r.listSteps);
check('list mode: previous wraps backwards', r.previousWraps);
check('list mode: previous from the first track wraps to the last', r.previousFromFirst);
check('shuffle: never repeats the current track', r.shuffleNeverRepeats);
check('shuffle: one-track queue stays put', r.shuffleSingleTrack);
check(
    'shuffle: every track gets the same share (±2%, unaffected by play counts)',
    r.shuffleMaxDeviation < 0.02,
    `max deviation ${(r.shuffleMaxDeviation * 100).toFixed(3)}% of ${r.expected.toFixed(0)} `
    + `-- histogram ${r.shuffleHistogram.join('/')} vs ${r.shuffleHistogramNoCounts.join('/')} (no counts)`,
);

console.log('');
if (failures.length) {
    console.error(`FAILED: ${failures.length} check(s)`);
    process.exit(1);
}
console.log('all player checks passed');
