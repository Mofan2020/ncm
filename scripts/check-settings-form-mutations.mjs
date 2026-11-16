#!/usr/bin/env node
/**
 * Mutation check for the settings form.
 *
 * The checks in check-settings-form.mjs are worth nothing if they stay
 * green when the form is broken.  This breaks one thing at a time in a
 * **copy** of app.js and expects the checker to notice.
 *
 * The source tree is never touched: the copy goes into a temporary
 * directory, so an interrupted run cannot leave broken code behind.
 *
 * Run:  node scripts/check-settings-form-mutations.mjs
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const source = path.join(root, 'src', 'gui', 'assets', 'app.js');
const checker = path.join(root, 'scripts', 'check-settings-form.mjs');
const original = fs.readFileSync(source, 'utf8');

const workdir = fs.mkdtempSync(path.join(os.tmpdir(), 'settings-form-mutation-'));

const MUTATIONS = [
    {
        name: 'the form does not fill the "borrow SDM" box',
        from: "    setChecked('update-use-sdm-check', s.ui.update_use_sdm !== false);",
        to: "    setChecked('update-use-sdm-check', true);",
    },
    {
        name: 'the form does not fill the thread count',
        from: "    if (threads) threads.value = String(Number(s.ui.update_threads) || 8);",
        to: "    if (threads) threads.value = '8';",
    },
    {
        name: 'the form does not fill the R-18 switch',
        from: "    setChecked('pixiv-include-r18', pixiv.include_r18 === true);",
        to: "    setChecked('pixiv-include-r18', false);",
    },
    {
        name: 'the form does not fill the adult content mode',
        from: "    setValue('pixiv-nsfw-mode', pixiv.nsfw_mode || 'hide');",
        to: "    setValue('pixiv-nsfw-mode', 'hide');",
    },
    {
        name: 'the form does not fill the page size',
        from: "    setValue('pixiv-page-size', String(Number(pixiv.page_size) || 48));",
        to: "    setValue('pixiv-page-size', '48');",
    },
    {
        name: 'the form does not fill the template',
        from: "        pixivTemplate.value = pixiv.template || '';",
        to: "        pixivTemplate.value = '';",
    },
    {
        name: 'the form reads the pixiv section from the wrong place',
        from: "    const pixiv = s.pixiv || {};",
        to: "    const pixiv = s.bili || {};",
    },
    {
        name: 'the save does not send the "borrow SDM" setting',
        from: "            update_use_sdm: document.getElementById('update-use-sdm-check').checked,",
        to: "            /* update_use_sdm is not sent */",
    },
    {
        name: 'the save does not send the R-18 setting',
        from: "            include_r18: !!((document.getElementById('pixiv-include-r18') || {}).checked),",
        to: "            include_r18: false,",
    },
    {
        name: 'the save does not send the pixiv section at all',
        from: "    for (const category of ['download', 'playback', 'ui', 'auth', 'bili', 'bangumi', 'pixiv']) {",
        to: "    for (const category of ['download', 'playback', 'ui', 'auth', 'bili', 'bangumi']) {",
    },
    {
        name: 'opening the modal does not ask the backend again',
        from: "    loadSettings().catch(() => { /* 后端没连上：弹窗照常开着 */ });",
        to: "    /* the form keeps whatever was left in it */",
    },
];

let caught = 0;
let missed = 0;

for (const mutation of MUTATIONS) {
    if (!original.includes(mutation.from)) {
        console.log(`[SKIP] ${mutation.name} -- the snippet is not in app.js`);
        missed += 1;
        continue;
    }
    const broken = original.replace(mutation.from, mutation.to);
    const copy = path.join(workdir, 'app.js');
    fs.writeFileSync(copy, broken, 'utf8');

    const done = spawnSync(process.execPath, [checker], {
        encoding: 'utf8',
        cwd: root,
        env: { ...process.env, NCM_APP_JS: copy },
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
        console.log(output.split('\n').slice(-6).join('\n'));
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
