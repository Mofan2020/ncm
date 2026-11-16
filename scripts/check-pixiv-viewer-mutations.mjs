#!/usr/bin/env node
/**
 * Mutation check for the Pixiv full-screen image viewer.
 *
 * Why this file exists: a check that stays green when the code it guards
 * is broken proves nothing.  This script takes the *real* pixiv.js, breaks
 * one thing at a time, and expects the checker to notice.
 *
 * The mutations are done on a **copy** of pixiv.js in a temporary
 * directory -- the source tree is never touched, so an interrupted run
 * cannot leave broken code behind.
 *
 * Run:  node scripts/check-pixiv-viewer-mutations.mjs
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const source = path.join(root, 'src', 'gui', 'assets', 'pixiv.js');
const checker = path.join(root, 'scripts', 'check-pixiv-viewer.mjs');
const original = fs.readFileSync(source, 'utf8');

const workdir = fs.mkdtempSync(path.join(os.tmpdir(), 'pixiv-viewer-mutation-'));

/** The mutations: what to break, and what the checker should notice. */
const MUTATIONS = [
    {
        name: 'the zoom no longer keeps the pointer in place',
        // The anchor maths: the offset is no longer derived from the
        // screen position, so the point under the cursor drifts away.
        from: '    pixivViewer.offsetX = screenX - imageX * ratio;\n' +
              '    pixivViewer.offsetY = screenY - imageY * ratio;',
        to: '    pixivViewer.offsetX = imageX - imageX * ratio;\n' +
            '    pixivViewer.offsetY = imageY - imageY * ratio;',
    },
    {
        name: 'switching pages keeps the old image on screen',
        from: "        image.removeAttribute('src');\n        image.style.transform = '';",
        to: "        image.style.transform = '';",
    },
    {
        name: 'switching pages shows no loading hint',
        from: '    if (loading) loading.hidden = false;\n\n    const pager',
        to: '    if (loading) loading.hidden = true;\n\n    const pager',
    },
    {
        name: 'the loading hint never goes away',
        from: '        if (loading) loading.hidden = true;\n        pixivViewerApply();',
        to: '        pixivViewerApply();',
    },
    {
        name: 'the zoom is clamped to a few fixed steps',
        from: '    return Math.max(VIEWER_MIN_SCALE, Math.min(VIEWER_MAX_SCALE, value));',
        to: '    return Math.round(value);',
    },
    {
        name: 'dragging does not move the image',
        from: '            pixivViewer.offsetX = from.offsetX + (event.clientX - from.x);\n' +
              '            pixivViewer.offsetY = from.offsetY + (event.clientY - from.y);',
        to: '            pixivViewer.offsetX = from.offsetX;\n' +
            '            pixivViewer.offsetY = from.offsetY;',
    },
    {
        name: 'the image keeps moving after the pointer is up',
        from: '        image.addEventListener(\'pointerup\', endDrag);',
        to: '        image.addEventListener(\'pointerup\', () => {});',
    },
    {
        name: 'switching pages does not reset the zoom',
        from: '    pixivViewer.scale = 1;\n    pixivViewer.offsetX = 0;\n    pixivViewer.offsetY = 0;\n    if (loading) loading.hidden = false;',
        to: '    if (loading) loading.hidden = false;',
    },
    {
        name: 'the arrow keys do not page through the images',
        from: "        } else if (event.key === 'ArrowRight') {\n" +
              '            pixivViewerShow(pixivViewer.index + 1);',
        to: "        } else if (event.key === 'ArrowRight') {\n" +
            '            pixivViewerShow(pixivViewer.index);',
    },
    {
        name: 'escape does not close the viewer',
        from: "        if (event.key === 'Escape') {\n            pixivViewerClose();",
        to: "        if (event.key === 'Escape') {\n",
    },
    {
        name: 'the wheel does not zoom',
        from: '            pixivViewerZoomAt(factor, event.clientX, event.clientY);',
        to: '            /* the wheel does nothing */',
    },
    {
        name: 'a single wheel step is not capped',
        from: '            const factor = Math.max(0.5, Math.min(2, Math.exp(-delta / divisor)));',
        to: '            const factor = Math.exp(-delta / divisor);',
    },
    {
        name: 'the wheel and the pinch share one step',
        from: '            const divisor = event.ctrlKey ? 40 : 320;',
        to: '            const divisor = 120;',
    },
    {
        name: 'binding twice wires the events twice',
        from: '    if (pixivViewer.bound) return;\n    pixivViewer.bound = true;',
        to: '    pixivViewer.bound = true;',
    },
];

let caught = 0;
let missed = 0;

for (const mutation of MUTATIONS) {
    if (!original.includes(mutation.from)) {
        console.log(`[SKIP] ${mutation.name} -- the script is broken: the snippet is not in pixiv.js`);
        missed += 1;
        continue;
    }
    const broken = original.replace(mutation.from, mutation.to);
    const path_ = path.join(workdir, 'pixiv.js');
    fs.writeFileSync(path_, broken, 'utf8');

    const done = spawnSync(process.execPath, [checker, path_], {
        encoding: 'utf8',
        cwd: root,
    });
    const output = (done.stdout || '') + (done.stderr || '');

    if (done.status === 0) {
        console.log(`[MISS] ${mutation.name} -- the checker stayed green`);
        missed += 1;
    } else if (done.status === 1) {
        console.log(`[CAUGHT] ${mutation.name}`);
        caught += 1;
    } else {
        console.log(`[BROKEN] ${mutation.name} -- the checker itself broke (status ${done.status})`);
        console.log(output.split('\n').slice(-8).join('\n'));
        missed += 1;
    }
}

fs.rmSync(workdir, { recursive: true, force: true });

console.log(`\n${caught} of ${MUTATIONS.length} mutations were caught`);
if (missed) {
    console.error(`${missed} mutation(s) slipped through`);
    process.exit(1);
}
console.log('every mutation was caught');
