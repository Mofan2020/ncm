#!/usr/bin/env node
/**
 * Behavioural check for the lyrics-following state machine in
 * src/gui/assets/app.js.
 *
 * Why this is not a pytest test: the state machine lives in the frontend, so
 * the only honest way to test it is to load app.js and drive it.  The script
 * loads the file behind a small DOM stub, replaces the timers and the clock
 * with ones the script controls, and checks the contract:
 *
 *   1. the playing line stays centred while nobody touches the panel;
 *   2. a scroll by the user takes the panel over -- the list stays where the
 *      user put it and the song does not yank it back;
 *   3. the panel returns to the playing line on its own once the user has
 *      been idle for the configured number of seconds.  The return is driven
 *      by a timer, NOT by the scroll event: the old code checked the idle
 *      time inside the scroll handler, so it was never evaluated again after
 *      the user stopped scrolling and the panel never came back;
 *   4. the panel's own scrolling is not mistaken for the user scrolling;
 *   5. the idle delay comes from the setting, and changing the setting
 *      re-arms a pending return;
 *   6. the two panels (player side and full-screen now playing) keep their
 *      own state.
 *
 * Run:  node scripts/check-lyrics-scroll.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
// An optional file name lets the mutation check run this harness against a
// deliberately broken copy of app.js.
const appPath = process.argv[2]
    ? path.resolve(process.argv[2])
    : path.join(root, 'src', 'gui', 'assets', 'app.js');

/* ------------------------------------------------------------------ clock */
/* The timers and the clock are the thing under test, so both are replaced by
   ones the script drives by hand.  The helper is called advanceClock, not
   advance: app.js has its own advance() for the play queue. */
let clock = 1_000_000;
let nextTimerId = 1;
const timers = new Map();

function fakeSetTimeout(fn, delay) {
    const id = nextTimerId++;
    timers.set(id, { fn, at: clock + (delay || 0), delay: delay || 0, every: false });
    return id;
}
function fakeSetInterval(fn, delay) {
    const id = nextTimerId++;
    timers.set(id, { fn, at: clock + (delay || 0), delay: delay || 0, every: true });
    return id;
}
function fakeClear(id) {
    timers.delete(id);
}
/** Move the clock forward and run everything that comes due. */
function advanceClock(ms) {
    const target = clock + ms;
    for (;;) {
        let due = null;
        for (const entry of timers) {
            const timer = entry[1];
            if (timer.at <= target && (!due || timer.at < due[1].at)) due = entry;
        }
        if (!due) break;
        const timer = due[1];
        clock = timer.at;
        if (timer.every) timer.at = clock + (timer.delay || 16);
        else timers.delete(due[0]);
        timer.fn();
    }
    clock = target;
}
/** The delay of the pending one-shot timer, or null. */
function pendingTimeout() {
    for (const entry of timers) {
        const timer = entry[1];
        if (!timer.every) return timer;
    }
    return null;
}

/* app.js asks the clock how late it is; it has to get the same answer the
   timers use, or the "this scroll was mine" window never opens. */
class FakeDate extends Date {
    static now() { return clock; }
}

/* -------------------------------------------------------------------- DOM */
class FakeLine {
    constructor(top) {
        this.offsetHeight = 20;
        this.top = top;
        this.active = false;
        this.classList = {
            add: () => { this.active = true; },
            remove: () => { this.active = false; },
            contains: (name) => this.active && name.indexOf('is-active') >= 0,
        };
    }
    getBoundingClientRect() {
        return { top: this.top, height: this.offsetHeight, bottom: this.top + this.offsetHeight };
    }
}

class FakeBox {
    constructor(id) {
        this.id = id;
        this.scrollTop = 0;
        this.clientHeight = 400;
        this.scrollHeight = 1000;
        this.dataset = {};
        this.style = { setProperty() {}, removeProperty() {} };
        this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
        this.listeners = new Map();
        this.lines = [new FakeLine(0), new FakeLine(100), new FakeLine(200)];
        this.scrrolledTo = [];
    }
    addEventListener(type, fn) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(fn);
    }
    dispatch(type) {
        (this.listeners.get(type) || []).forEach((fn) => fn({ type, target: this }));
    }
    scrollTo(options) {
        this.scrollTop = options.top;
        this.scrrolledTo.push(options.top);
        // The browser fires a scroll event for every scroll, including the
        // ones the script asked for.
        this.dispatch('scroll');
    }
    getBoundingClientRect() {
        return { top: 0, height: this.clientHeight, bottom: this.clientHeight };
    }
    querySelector() { return null; }
    querySelectorAll(selector) {
        if (selector.indexOf('lyric-line') >= 0) return this.lines;
        return [];
    }
    closest() { return null; }
}

function genericElement() {
    return {
        dataset: {}, style: { setProperty() {}, removeProperty() {} },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        addEventListener() {}, appendChild(c) { return c; }, append() {},
        removeAttribute() {}, setAttribute() {}, querySelector: () => null,
        querySelectorAll: () => [], closest: () => null, scrollTo() {},
        value: '', checked: false, textContent: '', innerHTML: '',
    };
}

const registry = new Map();
function register(id, element) {
    registry.set(id, element);
    return element;
}

const lyricsBox = register('lyrics-scroll', new FakeBox('lyrics-scroll'));
const mobileBox = register('npm-lyrics', new FakeBox('npm-lyrics'));
const audio = register('audio', Object.assign(genericElement(), { currentTime: 0 }));
const idleSlider = register('lyrics-scroll-idle', Object.assign(genericElement(), { value: '4' }));
const idleLabel = register('lyrics-scroll-idle-value', Object.assign(genericElement(), { textContent: '' }));

const sandbox = {
    console,
    Math, JSON, Object, Array, String, Number, Boolean, isFinite, parseInt, parseFloat,
    Date: FakeDate,
    setTimeout: fakeSetTimeout, clearTimeout: fakeClear,
    setInterval: fakeSetInterval, clearInterval: fakeClear,
    document: {
        getElementById: (id) => registry.get(id) || genericElement(),
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: () => genericElement(),
        createDocumentFragment: () => genericElement(),
        addEventListener() {},
        documentElement: {
            dataset: {}, style: { setProperty() {}, removeProperty() {} },
            setAttribute() {}, lang: '',
        },
        body: { classList: { add() {}, remove() {}, toggle() {}, contains: () => false } },
    },
    window: {
        addEventListener() {},
        matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    },
    Image: class { constructor() { this.crossOrigin = null; } },
    advanceClock,
    pendingTimeout,
    lyricsBox,
    mobileBox,
    audio,
    idleSlider,
    idleLabel,
};

/* ----------------------------------------------------------------- harness */
const harness = `
    const results = {};
    // The two lists the app has: the one next to the player and the
    // full-screen now-playing view.  The real app wires them the same way.
    bindLyricsList(lyricsBox, () => updateLyricHighlight(true));
    bindLyricsList(mobileBox, () => updateMobileLyricHighlight());

    // Count the centring calls instead of only running them: the maths of
    // the centring itself is covered by the python tests, this file is about
    // *who* is allowed to move the list.
    const realCenter = centerLyricLine;
    let centerCalls = 0;
    centerLyricLine = function (box, line, smooth) {
        centerCalls += 1;
        return realCenter(box, line, smooth);
    };

    player.lyrics = [
        { time: 0, text: 'first' },
        { time: 1000, text: 'second' },
        { time: 2000, text: 'third' },
    ];
    player.lyricIndex = -1;
    audio.currentTime = 0;

    // ---- 1. the song drives the panel ------------------------------------
    audio.currentTime = 0.5;      // 500 ms -> the first line
    updateLyricHighlight(true);
    results.songCentres = centerCalls === 1;
    results.startsFollowing = lyricsList(lyricsBox).following === true;

    // The glide the panel runs to the line fires scroll events of its own.
    // Those must not be read as the user scrolling.
    advanceClock(300);
    results.ownGlideIsNotATakeover = lyricsList(lyricsBox).following === true;

    // ---- 2. the user takes over -----------------------------------------
    centerCalls = 0;
    advanceClock(300);            // let the "this scroll was mine" window pass
    lyricsBox.dispatch('scroll'); // the user scrolled the list
    results.userTakesOver = lyricsList(lyricsBox).following === false;

    // The song keeps playing while the user reads: the panel must not move.
    audio.currentTime = 1.5;      // 1500 ms -> the second line
    updateLyricHighlight();
    results.songDoesNotYankBack = centerCalls === 0;

    // ---- 3. the return is driven by a timer, not by the scroll event -----
    const pending = pendingTimeout();
    results.returnIsArmed = pending !== null;
    results.returnUsesTheSetting = pending !== null && pending.delay === 4000;

    // The user stops scrolling: no further scroll events arrive.  Only the
    // clock moves on, and the panel has to come back by itself.
    centerCalls = 0;
    advanceClock(4000);
    results.returnsOnItsOwn = lyricsList(lyricsBox).following === true;
    results.returnCentresTheLine = centerCalls === 1;

    // The panel's own scrolling is not the user scrolling: while the panel
    // follows the song, a scroll it made itself must not stop it.
    markLyricsAutoScroll(lyricsBox);
    lyricsBox.dispatch('scroll');
    results.ownScrollIsNotATakeover = lyricsList(lyricsBox).following === true;

    // ---- 4. the setting is honoured and re-arms a pending return --------
    player.lyricsScrollIdleSeconds = 7;
    advanceClock(300);
    lyricsBox.dispatch('scroll');
    const pending7 = pendingTimeout();
    results.settingIsUsed = pending7 !== null && pending7.delay === 7000;
    // Change the setting while the return is pending: the pending return has
    // to use the new delay, not the old one.
    player.lyricsScrollIdleSeconds = 2;
    rearmLyricsReturn();
    const pending2 = pendingTimeout();
    results.settingChangeRearms = pending2 !== null && pending2.delay === 2000;

    // ---- 5. the two panels keep their own state -------------------------
    const desktopBefore = lyricsList(lyricsBox).following;
    mobileBox.dispatch('scroll');
    results.mobileTakesOver = lyricsList(mobileBox).following === false;
    results.desktopUnaffected = lyricsList(lyricsBox).following === desktopBefore;
    results.listsAreIndependent = lyricsList(lyricsBox) !== lyricsList(mobileBox);

    // ---- 6. the label shows the delay in the current language -----------
    state.translations['player.seconds'] = '{seconds} 秒';
    idleSlider.value = '9';
    updateLyricsScrollIdleLabel();
    results.labelFollowsTheSlider = idleLabel.textContent === '9 秒';

    globalThis.__results = results;
`;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(appPath, 'utf8') + '\n' + harness, sandbox, { filename: 'app.js' });
const r = sandbox.__results;

const failures = [];
function check(label, condition, detail) {
    const status = condition ? 'PASS' : 'FAIL';
    console.log(`[${status}] ${label}${detail ? ' -- ' + detail : ''}`);
    if (!condition) failures.push(label);
}

check('the song centres the playing line', r.songCentres);
check('the panel starts out following the song', r.startsFollowing);
check("the panel's own glide is not read as a takeover", r.ownGlideIsNotATakeover);
check('a scroll by the user takes the panel over', r.userTakesOver);
check('the song does not pull the panel back while the user reads', r.songDoesNotYankBack);
check('a return is armed while the user is reading', r.returnIsArmed);
check('the return uses the configured delay', r.returnUsesTheSetting, 'expected 4000 ms');
check('the panel returns to the playing line on its own', r.returnsOnItsOwn);
check('the return centres the playing line', r.returnCentresTheLine);
check("the panel's own scroll is not read as a takeover", r.ownScrollIsNotATakeover);
check('the configured delay is used', r.settingIsUsed, 'expected 7000 ms');
check('changing the setting re-arms the pending return', r.settingChangeRearms, 'expected 2000 ms');
check('the now-playing panel takes over on its own', r.mobileTakesOver);
check('the two panels do not share their state', r.listsAreIndependent);
check('the label shows the delay', r.labelFollowsTheSlider);

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed: ${failures.join('; ')}`);
    process.exit(1);
}
console.log('\nall checks passed');
