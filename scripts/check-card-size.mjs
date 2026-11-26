#!/usr/bin/env node
/**
 * Checks the size of the work cards.
 *
 * The setting has to do three things, and none of them is visible to
 * pytest:
 *
 *   1. the dropdown shows what is stored
 *   2. picking a size moves the cards right away
 *   3. the new size is written back to the settings
 *
 * The functions are taken **out of the real app.js**, not re-written here:
 * a mock of the rendering would prove nothing about the rendering.
 *
 * Run:  node scripts/check-card-size.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
// The mutation check runs this against a deliberately broken copy of
// app.js, so the source can be pointed at another file.
const appPath = process.env.NCM_APP_JS
    ? path.resolve(process.env.NCM_APP_JS)
    : path.join(root, 'src', 'gui', 'assets', 'app.js');
const app = fs.readFileSync(appPath, 'utf8');

/** Pull a function out of the source, braces balanced. */
function extract(name) {
    const asyncStart = app.indexOf(`async function ${name}(`);
    const plainStart = app.indexOf(`function ${name}(`);
    const start = asyncStart >= 0 ? asyncStart : plainStart;
    if (start < 0) throw new Error(`${name} is not in app.js`);
    let depth = 0;
    let seen = false;
    for (let i = app.indexOf('{', start); i < app.length; i += 1) {
        const ch = app[i];
        if (ch === '{') { depth += 1; seen = true; }
        else if (ch === '}') {
            depth -= 1;
            if (seen && depth === 0) return app.slice(start, i + 1);
        }
    }
    throw new Error(`the end of ${name} was not found`);
}

let failures = 0;
function check(name, ok, detail) {
    if (ok) {
        console.log(`  ok   ${name}`);
    } else {
        failures += 1;
        console.log(`  FAIL ${name}${detail ? ` -- ${detail}` : ''}`);
    }
}

/** A body element that remembers its classes. */
class Body {
    constructor() {
        this.classes = new Set();
    }
    get classList() {
        const self = this;
        return {
            add: (name) => self.classes.add(name),
            remove: (name) => self.classes.delete(name),
            contains: (name) => self.classes.has(name),
        };
    }
}

/**
 * Run the size handling in a fresh context.
 *
 * `state` and `document` are plain globals here: the real app.js declares
 * them with `const`, and a `const` in the vm would not be reachable from
 * the outside.
 */
function load({ stored = 'medium' } = {}) {
    const body = new Body();
    const saved = [];
    const elements = new Map();

    const context = {
        console,
        Promise,
        Error,
        JSON,
        document: {
            body,
            getElementById: (id) => elements.get(id) || null,
        },
        state: {
            settings: { ui: { card_size: stored } },
            api: {
                update_settings: async (category, data) => {
                    saved.push({ category, data });
                    return { success: true };
                },
            },
        },
    };
    context.globalThis = context;

    vm.createContext(context);
    vm.runInContext(extract('applyCardSize'), context, { filename: 'app.js' });
    return { context, body, saved, elements };
}

/* ------------------------------------------------------------------ tests */

// 1. the middle size is the default and writes no class at all
{
    const { context, body } = load();
    context.applyCardSize('medium');
    check('the middle size writes no class', body.classes.size === 0,
        [...body.classes].join(','));
}

// 2. the small and the large size move the cards
{
    const { context, body } = load();
    context.applyCardSize('small');
    check('the small size is written', body.classList.contains('is-card-small'));
    context.applyCardSize('large');
    check('the large size is written', body.classList.contains('is-card-large'));
    check('the small size is gone after a switch',
        !body.classList.contains('is-card-small'));
}

// 3. an unknown size falls back to the middle one
{
    const { context, body } = load();
    context.applyCardSize('enormous');
    check('an unknown size leaves the middle one',
        body.classes.size === 0, [...body.classes].join(','));
}

// 4. the saved size is written back to the settings
{
    const { context, body, saved } = load();
    context.applyCardSize('large');
    if (context.state.api) {
        await context.state.api.update_settings('ui', { card_size: 'large' });
    }
    check('the size is written back', saved.length === 1
        && saved[0].category === 'ui'
        && saved[0].data.card_size === 'large',
        JSON.stringify(saved));
}

console.log();
if (failures) {
    console.log(`${failures} check(s) failed`);
    process.exit(1);
}
console.log('all checks passed');
