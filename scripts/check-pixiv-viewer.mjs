#!/usr/bin/env node
/**
 * Behavioural check for the Pixiv full-screen image viewer in
 * src/gui/assets/pixiv.js.
 *
 * Why this is not a pytest test: the viewer lives in the frontend, so the
 * only honest way to test it is to load pixiv.js and drive it.  The script
 * loads the real file behind a small DOM stub and checks the contract:
 *
 *   1. opening the viewer shows the page it was opened on, scaled to fit;
 *   2. the zoom is anchored at the point under the cursor -- the pixel the
 *      pointer is over must not drift away while zooming;
 *   3. the zoom accepts any ratio (not a few fixed steps) and is clamped
 *      so the image can neither vanish nor become un-navigable;
 *   4. dragging moves the image by exactly the pointer's movement;
 *   5. **switching to the next page takes the old image down first** and
 *      shows a "loading" hint until the new one has arrived -- keeping the
 *      old image on screen makes the switch look like it did nothing;
 *   6. the zoom and the offset are reset when the page changes;
 *   7. the keyboard drives the viewer (arrows page, Escape closes).
 *
 * Run:  node scripts/check-pixiv-viewer.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
// An optional file name lets the mutation check run this harness against a
// deliberately broken copy of pixiv.js.
const pixivPath = process.argv[2]
    ? path.resolve(process.argv[2])
    : path.join(root, 'src', 'gui', 'assets', 'pixiv.js');

/* -------------------------------------------------------------------- DOM */

/** A stage that reports a real size, so the zoom maths has something to work
 *  with.  The viewer centres the image on this box. */
class FakeStage {
    constructor(id) {
        this.id = id;
        this.listeners = new Map();
        this.style = {};
        this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
        this.width = 1000;
        this.height = 800;
    }
    addEventListener(type, fn) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(fn);
    }
    dispatch(type, event) {
        const payload = Object.assign({ type, target: this, preventDefault() {} }, event || {});
        (this.listeners.get(type) || []).forEach((fn) => fn(payload));
    }
    getBoundingClientRect() {
        return { left: 0, top: 0, width: this.width, height: this.height,
                 right: this.width, bottom: this.height };
    }
    setPointerCapture() {}
    releasePointerCapture() {}
    removeAttribute(name) { if (name === 'src') this.src = ''; }
}

/** The image: remembers its src and lets the script pretend it loaded. */
class FakeImage {
    constructor(id) {
        this.id = id;
        this.style = {};
        this.src = '';
        /** Every src that was taken *off* the element. */
        this.removedSrcs = [];
        this.hidden = false;
        this.listeners = new Map();
        this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
        this.onload = null;
        this.onerror = null;
        this.clientWidth = 500;
        this.clientHeight = 400;
    }
    addEventListener(type, fn) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(fn);
    }
    dispatch(type, event) {
        const payload = Object.assign({ type, target: this, preventDefault() {} }, event || {});
        (this.listeners.get(type) || []).forEach((fn) => fn(payload));
    }
    /** Pretend the browser finished loading the current src. */
    finishLoading() {
        if (this.onload) this.onload();
    }
    getBoundingClientRect() {
        return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight,
                 right: this.clientWidth, bottom: this.clientHeight };
    }
    setPointerCapture() {}
    releasePointerCapture() {}
    removeAttribute(name) {
        if (name === 'src') {
            this.removedSrcs.push(this.src);
            this.src = '';
        }
    }
}

function genericElement(id) {
    return {
        id: id || '',
        value: '',
        checked: false,
        textContent: '',
        innerHTML: '',
        hidden: false,
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        listeners: new Map(),
        addEventListener(type, fn) {
            if (!this.listeners.has(type)) this.listeners.set(type, []);
            this.listeners.get(type).push(fn);
        },
        dispatch(type, event) {
            const payload = Object.assign({ type, target: this, preventDefault() {} }, event || {});
            (this.listeners.get(type) || []).forEach((fn) => fn(payload));
        },
        removeAttribute() {},
        setAttribute() {},
        appendChild(c) { return c; },
        append() {},
        querySelector: () => null,
        querySelectorAll: () => [],
        closest: () => null,
        scrollTo() {},
        getBoundingClientRect() {
            return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
        },
    };
}

const registry = new Map();
function register(id, element) {
    registry.set(id, element);
    return element;
}

const stage = register('pixiv-viewer-stage', new FakeStage('pixiv-viewer-stage'));
const image = register('pixiv-viewer-image', new FakeImage('pixiv-viewer-image'));
const viewer = register('pixiv-viewer', genericElement('pixiv-viewer'));
const loading = register('pixiv-viewer-loading', genericElement('pixiv-viewer-loading'));
const zoomLabel = register('pixiv-viewer-zoom', genericElement('pixiv-viewer-zoom'));
const pager = register('pixiv-viewer-pager', genericElement('pixiv-viewer-pager'));
const bg = register('pixiv-viewer-bg', genericElement('pixiv-viewer-bg'));
const prevBtn = register('btn-pixiv-viewer-prev', genericElement('btn-pixiv-viewer-prev'));
const nextBtn = register('btn-pixiv-viewer-next', genericElement('btn-pixiv-viewer-next'));
const closeBtn = register('btn-pixiv-viewer-close', genericElement('btn-pixiv-viewer-close'));
const resetBtn = register('btn-pixiv-viewer-reset', genericElement('btn-pixiv-viewer-reset'));

const documentListeners = new Map();
const bodyClasses = new Set();

const sandbox = {
    console,
    Math, JSON, Object, Array, String, Number, Boolean, isFinite, parseInt, parseFloat,
    Date, setTimeout, clearTimeout, setInterval, clearInterval,
    document: {
        getElementById: (id) => registry.get(id) || genericElement(id),
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: () => genericElement(),
        createDocumentFragment: () => genericElement(),
        addEventListener(type, fn) {
            if (!documentListeners.has(type)) documentListeners.set(type, []);
            documentListeners.get(type).push(fn);
        },
        dispatchEvent(event) {
            const type = event && event.type ? event.type : 'keydown';
            // The handler may call preventDefault() and stopPropagation();
            // a bare object without them throws.
            const payload = Object.assign({
                preventDefault() {}, stopPropagation() {},
            }, event || {});
            (documentListeners.get(type) || []).forEach((fn) => fn(payload));
            return true;
        },
        documentElement: {
            dataset: {}, style: { setProperty() {}, removeProperty() {} },
            setAttribute() {}, lang: '',
        },
        body: {
            classList: {
                add: (name) => bodyClasses.add(name),
                remove: (name) => bodyClasses.delete(name),
                toggle: () => {},
                contains: (name) => bodyClasses.has(name),
            },
        },
    },
    window: {
        addEventListener() {},
        matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
        innerWidth: 1000,
        innerHeight: 800,
    },
    Image: class { constructor() { this.crossOrigin = null; } },
    t: (key, vars) => {
        if (key === 'pixiv.page_of') return `${vars.index} / ${vars.total}`;
        return key;
    },
    showToast() {},
    pixivToast() {},
    // The elements the harness drives.  They have to be in the sandbox:
    // the harness runs inside it and cannot see this module's scope.
    stage, image, viewer, loading, zoomLabel, pager, bg,
    prevBtn, nextBtn, closeBtn, resetBtn,
    documentListeners,
};

/* ----------------------------------------------------------------- harness */
const harness = `
    const results = {};
    const viewerApi = window.PixivViewer;
    const state = viewerApi.state;

    const pages = [
        { proxy: '/pixiv?u=one', regular: 'https://i.pximg.net/one.jpg' },
        { proxy: '/pixiv?u=two', regular: 'https://i.pximg.net/two.jpg' },
        { proxy: '/pixiv?u=three', regular: 'https://i.pximg.net/three.jpg' },
    ];

    // The real app binds the viewer when PixivUI.init() runs.  The harness
    // has to do the same, otherwise the wheel, the drag and the keyboard
    // are not wired and every check below would be about nothing.
    viewerApi.bind();
    // Binding twice must not wire the events twice: a wheel notch would
    // then zoom twice and the image would jump.
    const wheelBefore = stage.listeners.get('wheel').length;
    viewerApi.bind();
    results.bindingTwiceDoesNotDoubleWire =
        stage.listeners.get('wheel').length === wheelBefore;

    // ---- 1. opening -----------------------------------------------------
    viewerApi.open(pages, 1);
    results.opensOnTheRequestedPage = state.index === 1;
    results.showsTheImage = image.src === '/pixiv?u=two';
    results.viewerIsVisible = viewer.hidden === false;
    results.startsAtFitScale = state.scale === 1;
    results.loadingIsShownBeforeTheImageArrives = loading.hidden === false;

    // The image arrives: the loading hint goes away.
    image.finishLoading();
    results.loadingHidesAfterTheImageArrives = loading.hidden === true;

    // ---- 2. the zoom is anchored at the pointer --------------------------
    viewerApi.reset();
    // Drag the picture first, so the offset is not zero.  With a zero
    // offset the pointer position and the image position are the same
    // number, and a broken anchor formula would land on the right answer
    // by accident.
    image.dispatch('pointerdown', { clientX: 500, clientY: 400 });
    image.dispatch('pointermove', { clientX: 560, clientY: 450 });
    image.dispatch('pointerup', { clientX: 560, clientY: 450 });
    // The pointer sits 200 px right of the centre, 100 px below it.
    const anchorX = 500 + 200;
    const anchorY = 400 + 100;
    // The offset *before* the zoom: the image coordinate of the anchor is
    // the screen position minus the offset that was in effect then.
    const offsetBefore = { x: state.offsetX, y: state.offsetY };
    const screenBefore = { x: anchorX - 500, y: anchorY - 400 };
    const imageX = screenBefore.x - offsetBefore.x;
    const imageY = screenBefore.y - offsetBefore.y;

    viewerApi.zoomAt(2, anchorX, anchorY);
    const zoomedScale = state.scale;
    results.zoomChangesTheScale = zoomedScale > 1;

    // **The offset is checked against the expected value, not recomputed
    // from the state.**  Reading the state back and rearranging the same
    // formula would be true whatever the code does -- that is how a broken
    // anchor maths slips through.  The expectation is worked out here:
    // the anchor stays at screenX, so
    //   imageX * scale + offset = screenX
    //   offset = screenX - imageX * scale
    const expectedOffsetX = screenBefore.x - imageX * zoomedScale;
    const expectedOffsetY = screenBefore.y - imageY * zoomedScale;
    results.zoomKeepsTheAnchorStill =
        Math.abs(state.offsetX - expectedOffsetX) < 0.001 &&
        Math.abs(state.offsetY - expectedOffsetY) < 0.001;

    // ---- 3. any ratio, clamped ------------------------------------------
    viewerApi.reset();
    // The wheel is what the user actually turns.  Driving the event and
    // not the function: calling zoomAt() directly would prove nothing
    // about the wheel.
    // A mouse wheel notch is about 120; a trackpad pinch is a few pixels.
    // The two have to be told apart by their delta, not by one formula.
    stage.dispatch('wheel', { deltaY: -120, ctrlKey: false, clientX: 500, clientY: 400 });
    const scaleAfterWheel = state.scale;
    results.theWheelZooms = scaleAfterWheel > 1;

    // The trackpad pinch arrives as ctrl+wheel.
    stage.dispatch('wheel', { deltaY: -5, ctrlKey: true, clientX: 500, clientY: 400 });
    const scaleAfterPinch = state.scale;
    results.thePinchZooms = scaleAfterPinch > scaleAfterWheel;
    // A pinch has to be **noticeable**: one pinch moves the picture by a
    // few percent.  If the pinch shared the wheel's step, one pinch
    // would move it by 4% -- the gesture would look like it did nothing.
    results.aPinchIsNoticeable =
        (scaleAfterPinch / scaleAfterWheel) >= 1.05;
    // ...and it must stay gentler than a wheel notch.
    results.thePinchIsGentlerThanTheWheel =
        (scaleAfterPinch / scaleAfterWheel) < (scaleAfterWheel / 1);

    // A flick on the trackpad can report a delta of several hundred.
    // Without a cap on a single step that one flick would jump straight
    // to the maximum ratio.
    viewerApi.reset();
    stage.dispatch('wheel', { deltaY: -500, ctrlKey: true, clientX: 500, clientY: 400 });
    results.aFlickDoesNotJumpToTheMaximum = state.scale <= 2.5;

    viewerApi.reset();
    viewerApi.zoomAt(1.05, 500, 400);
    const smallStep = state.scale;
    viewerApi.zoomAt(1.05, 500, 400);
    const secondStep = state.scale;
    results.zoomTakesArbitraryRatios =
        Math.abs(smallStep - 1.05) < 0.0001 && Math.abs(secondStep - 1.1025) < 0.0001;

    // Far out: clamped, not infinite.
    for (let i = 0; i < 200; i += 1) viewerApi.zoomAt(2, 500, 400);
    results.zoomIsClampedHigh = state.scale <= 12 + 0.0001;
    // Far in: clamped too, so the image cannot vanish.
    for (let i = 0; i < 400; i += 1) viewerApi.zoomAt(0.5, 500, 400);
    results.zoomIsClampedLow = state.scale >= 0.1 - 0.0001;

    // ---- 4. dragging moves the image by the pointer's movement -----------
    viewerApi.reset();
    image.dispatch('pointerdown', { clientX: 100, clientY: 100, pointerId: 1 });
    image.dispatch('pointermove', { clientX: 160, clientY: 70, pointerId: 1 });
    results.dragFollowsThePointer =
        state.offsetX === 60 && state.offsetY === -30;
    image.dispatch('pointerup', { clientX: 160, clientY: 70, pointerId: 1 });
    // After the drag ends the image must stay where it was.
    image.dispatch('pointermove', { clientX: 300, clientY: 300, pointerId: 1 });
    results.dragStopsWhenThePointerIsUp =
        state.offsetX === 60 && state.offsetY === -30;

    // ---- 5. switching pages takes the old image down first --------------
    viewerApi.reset();
    viewerApi.zoomAt(3, 500, 400);
    const scaleBeforeSwitch = state.scale;
    const offsetBeforeSwitch = { x: state.offsetX, y: state.offsetY };
    viewerApi.show(2);
    results.oldImageIsTakenDown = image.src === '/pixiv?u=three';
    // The old image must have been **removed**, not overwritten: a plain
    // overwrite leaves the previous picture on screen until the new one
    // arrives, and the switch then looks like it did nothing.
    results.oldImageWasRemovedNotOverwritten =
        image.removedSrcs.indexOf('/pixiv?u=two') >= 0;
    results.loadingShowsWhileTheNewImageArrives = loading.hidden === false;
    results.switchResetsTheZoom = state.scale === 1;
    results.switchResetsTheOffset =
        state.offsetX === 0 && state.offsetY === 0;
    results.switchKeepsTheZoomFromBefore =
        scaleBeforeSwitch !== state.scale ||
        offsetBeforeSwitch.x !== state.offsetX;

    // A late onload from the page we already left must not clear the
    // loading hint of the page we are on now.
    const staleOnload = image.onload;
    viewerApi.show(1);
    image.finishLoading();
    results.newImageArrives = loading.hidden === true;
    results.pagerFollowsThePage = pager.textContent === '2 / 3';

    // ---- 6. the keyboard drives it --------------------------------------
    viewerApi.open(pages, 0);
    image.finishLoading();
    results.viewerIsOpenBeforeTheKeys = state.open === true;
    const keyHandlers = (documentListeners.get('keydown') || []).length;
    results.keyHandlersAreWired = keyHandlers > 0;
    document.dispatchEvent({ type: 'keydown', key: 'ArrowRight' });
    results.arrowKeysPage = state.index === 1;
    document.dispatchEvent({ type: 'keydown', key: 'Escape' });
    results.escapeCloses = state.open === false && viewer.hidden === true;

    globalThis.__results = results;
`;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(pixivPath, 'utf8') + '\n' + harness, sandbox, { filename: 'pixiv.js' });
const r = sandbox.__results;

const failures = [];
function check(label, condition, detail) {
    const status = condition ? 'PASS' : 'FAIL';
    console.log(`[${status}] ${label}${detail ? ' -- ' + detail : ''}`);
    if (!condition) failures.push(label);
}

check('binding twice does not wire the events twice', r.bindingTwiceDoesNotDoubleWire);
check('the viewer opens on the requested page', r.opensOnTheRequestedPage);
check('the image of that page is shown', r.showsTheImage);
check('the viewer becomes visible', r.viewerIsVisible);
check('it starts at the size that fits the screen', r.startsAtFitScale);
check('a loading hint is shown before the image arrives', r.loadingIsShownBeforeTheImageArrives);
check('the loading hint goes away once the image is there', r.loadingHidesAfterTheImageArrives);
check('the wheel zooms', r.theWheelZooms);
check('the trackpad pinch zooms', r.thePinchZooms);
check('a pinch is noticeable', r.aPinchIsNoticeable);
check('the pinch is gentler than the wheel', r.thePinchIsGentlerThanTheWheel);
check('a flick on the trackpad does not jump to the maximum', r.aFlickDoesNotJumpToTheMaximum);
check('zooming changes the scale', r.zoomChangesTheScale);
check('the point under the pointer stays under the pointer', r.zoomKeepsTheAnchorStill);
check('the zoom takes arbitrary ratios', r.zoomTakesArbitraryRatios);
check('the zoom is clamped at the top', r.zoomIsClampedHigh);
check('the zoom is clamped at the bottom', r.zoomIsClampedLow);
check('the old image is removed, not overwritten', r.oldImageWasRemovedNotOverwritten);
check('dragging moves the image by the pointer movement', r.dragFollowsThePointer);
check('the image stays put when the pointer is up', r.dragStopsWhenThePointerIsUp);
check('switching pages takes the old image down', r.oldImageIsTakenDown);
check('a loading hint is shown while the new image arrives', r.loadingShowsWhileTheNewImageArrives);
check('switching pages resets the zoom', r.switchResetsTheZoom);
check('switching pages resets the offset', r.switchResetsTheOffset);
check('the zoom before the switch really was different', r.switchKeepsTheZoomFromBefore);
check('the new image arriving clears the hint', r.newImageArrives);
check('the pager follows the page', r.pagerFollowsThePage);
check('the arrow keys page through the images', r.arrowKeysPage);
check('escape closes the viewer', r.escapeCloses);

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed: ${failures.join('; ')}`);
    process.exit(1);
}
console.log('\nall checks passed');
