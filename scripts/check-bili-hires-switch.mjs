#!/usr/bin/env node
/**
 * The Hi-Res switch of the inline player: when it shows and when it does not.
 *
 * The switch has three states and they are not the same thing:
 *   - the video has no lossless track  -> the switch is hidden
 *   - the video has one, the account may
 *     not have it                        -> the switch is there but off
 *                                         and says why
 *   - the video has one and the account
 *     may have it                        -> the switch is on
 *
 * The label carries the reason, so the user learns why the sound is not
 * the one they asked for instead of finding a greyed-out box.
 *
 * Run:  node scripts/check-bili-hires-switch.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const assets = path.join(root, 'src', 'gui', 'assets');
const appPath = path.join(assets, 'app.js');
const source = fs.readFileSync(appPath, 'utf8');

let failures = 0;
function check(name, ok, detail) {
    if (ok) {
        console.log(`  ok   ${name}`);
    } else {
        failures += 1;
        console.log(`  FAIL ${name}${detail ? ` -- ${detail}` : ''}`);
    }
}

/** Cut one function out of app.js by balancing its braces. */
function extract(name) {
    const start = source.indexOf(`function ${name}(`);
    if (start < 0) throw new Error(`${name} is not in app.js`);
    let depth = 0;
    let seen = false;
    for (let i = start; i < source.length; i += 1) {
        const ch = source[i];
        if (ch === '{') { depth += 1; seen = true; }
        else if (ch === '}') {
            depth -= 1;
            if (seen && depth === 0) return source.slice(start, i + 1);
        }
    }
    throw new Error(`the braces of ${name} do not balance`);
}

/** A stand-in for the two elements the switch lives in. */
function makeElements() {
    const field = {hidden: false, attrs: {}};
    const box = {checked: false, disabled: false, attrs: {}};
    const label = {attrs: {}, textContent: ''};
    field.setAttribute = (k, v) => { field.attrs[k] = v; };
    box.setAttribute = (k, v) => { box.attrs[k] = v; };
    label.setAttribute = (k, v) => { label.attrs[k] = v; };
    return { field, box, label };
}

/** Run renderBiliHires against one answer of the playurl route. */
function render(streams, settings) {
    const elements = makeElements();
    const context = {
        document: {
            getElementById: (id) => {
                if (id === 'bili-hires-field') return elements.field;
                if (id === 'bili-hires-on') return elements.box;
                if (id === 'bili-hires-label') return elements.label;
                return null;
            },
        },
        state: {settings: settings || {}},
        biliPlayer: {hiresOverride: null},
        t: (key) => key,
        console,
    };
    vm.createContext(context);
    vm.runInContext(
        extract('renderBiliHires') + '\nglobalThis.__render = renderBiliHires;',
        context, {filename: appPath});
    context.__render(streams);
    return elements;
}

console.log('bili hires switch');

/* 1. A video without a lossless track: the switch is hidden. */
{
    const el = render({
        hires_available: false,
        hires_granted: false,
        flac: null,
    });
    check('a video without a track hides the switch', el.field.hidden === true);
    check('and the box is off and disabled',
        el.box.checked === false && el.box.disabled === true);
}

/* 2. The video has one, the account may not: the switch is there and off. */
{
    const el = render({
        hires_available: true,
        hires_granted: false,
        flac: null,
    });
    check('a track the account may not have shows the switch',
        el.field.hidden === false);
    check('and it is disabled', el.box.disabled === true);
    check('and it is off', el.box.checked === false);
    check('and it says why',
        el.label.attrs['data-i18n'] === 'bili.hires_needs_member',
        JSON.stringify(el.label.attrs));
}

/* 3. The account may have it: the switch works. */
{
    const el = render({
        hires_available: true,
        hires_granted: true,
        flac: {url: 'http://127.0.0.1/flac.m4s'},
    });
    check('a track the account may have shows the switch',
        el.field.hidden === false);
    check('and it is not disabled', el.box.disabled === false);
    check('and it carries the ordinary label',
        el.label.attrs['data-i18n'] === 'bili.hires_on',
        JSON.stringify(el.label.attrs));
}

/* 4. The setting is the default; the user can override it. */
{
    const off = render({
        hires_available: true,
        hires_granted: true,
        flac: {url: 'x'},
    }, {bili: {hires_enabled: false}});
    check('the setting off leaves the switch off', off.box.checked === false);

    const on = render({
        hires_available: true,
        hires_granted: true,
        flac: {url: 'x'},
    }, {bili: {hires_enabled: true}});
    check('the setting on ticks the switch', on.box.checked === true);
}

/* 5. An account without the membership cannot tick it even with the
      setting on -- the sound would not start. */
{
    const el = render({
        hires_available: true,
        hires_granted: false,
        flac: null,
    }, {bili: {hires_enabled: true}});
    check('the setting on does not tick a track the account may not have',
        el.box.checked === false);
}

console.log('');
if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
}
console.log('all checks passed');
