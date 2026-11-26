#!/usr/bin/env node
/**
 * Checks the picture side of the Bilibili private messages.
 *
 * Two things have to work, and neither of them is visible to pytest:
 *
 *   1. a picture message shows the picture, not the JSON that came with it
 *   2. the picture button sends the picture the user picked
 *
 * The functions are taken **out of the real app.js**, not re-written here:
 * a mock of the rendering would prove nothing about the rendering.
 *
 * Run:  node scripts/check-bili-im-picture.mjs
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

/* ------------------------------------------------------------------- DOM */

/**
 * A very small DOM.
 *
 * `appendChild` **moves** the node (the old parent loses it), and setting
 * `innerHTML = ''` throws the children away -- the real DOM does both, and
 * a fake that copies would let a stale node stay in the tree.
 */
class El {
    constructor(tag) {
        this.tagName = String(tag || 'div').toUpperCase();
        this.children = [];
        this.parent = null;
        this.className = '';
        this.textContent = '';
        this.src = '';
        this.alt = '';
        this.loading = '';
        this.handlers = {};
    }

    get classList() {
        const self = this;
        return {
            add(name) {
                const parts = self.className.split(/\s+/).filter(Boolean);
                if (!parts.includes(name)) parts.push(name);
                self.className = parts.join(' ');
            },
        };
    }

    setAttribute(name, value) {
        this[name] = value;
    }

    removeAttribute(name) {
        this[name] = '';
    }

    appendChild(child) {
        if (child.parent) {
            const index = child.parent.children.indexOf(child);
            if (index >= 0) child.parent.children.splice(index, 1);
        }
        child.parent = this;
        this.children.push(child);
        return child;
    }

    remove() {
        if (!this.parent) return;
        const index = this.parent.children.indexOf(this);
        if (index >= 0) this.parent.children.splice(index, 1);
        this.parent = null;
    }

    addEventListener(name, fn) {
        (this.handlers[name] = this.handlers[name] || []).push(fn);
    }

    set innerHTML(value) {
        this.children.forEach((child) => { child.parent = null; });
        this.children = [];
        this._html = value;
    }

    get innerHTML() {
        return this._html || '';
    }

    /** Every descendant, depth first. */
    all() {
        const out = [];
        const walk = (node) => {
            node.children.forEach((child) => { out.push(child); walk(child); });
        };
        walk(this);
        return out;
    }

    find(predicate) {
        return this.all().find(predicate) || null;
    }
}

/** The elements the private-message panel uses. */
function makeElements() {
    const map = new Map();
    const ids = ['bili-im-sessions', 'bili-im-head', 'bili-im-messages',
        'bili-im-state', 'bili-im-input'];
    ids.forEach((id) => map.set(id, new El('div')));
    return map;
}

/* ------------------------------------------------------------------ setup */

let failures = 0;
function check(name, ok, detail) {
    if (ok) {
        console.log(`  ok   ${name}`);
    } else {
        failures += 1;
        console.log(`  FAIL ${name}${detail ? ` -- ${detail}` : ''}`);
    }
}

/**
 * Run the two functions in a fresh context.
 *
 * `biliIm` and `state` are plain globals here: the real app.js declares them
 * with `const`, and a `const` in the vm would not be reachable from the
 * outside.
 */
function load({ messages = [], talkerId = '123', sessionId = '900',
    api = {}, name = '小明', sendingImage = false } = {}) {
    const elements = makeElements();
    const toasts = [];
    const refreshed = [];
    const calls = [];

    const context = {
        console,
        Promise,
        Error,
        JSON,
        document: {
            getElementById: (id) => elements.get(id) || null,
            createElement: (tag) => new El(tag),
        },
        state: {
            mediaBaseUrl: 'http://127.0.0.1:8931',
            api,
        },
        biliIm: {
            sessions: [],
            sessionId,
            talkerId,
            name,
            messages,
            loading: false,
            error: '',
            sending: 0,
            sendingImage,
        },
        t: (key) => key,
        showToast: (kind, text) => { toasts.push({ kind, text }); },
        bridgeHas: (method) => typeof context.state.api[method] === 'function',
        buildStateBlock: (icon, title) => {
            const block = new El('div');
            block.textContent = title;
            return block;
        },
        openBiliConversation: (session) => {
            refreshed.push(session);
            // The server answers with the messages it knows about.  When
            // the test puts a list here, the panel is filled with it --
            // that is how "the server has not counted the message in yet"
            // is reproduced.
            if (Array.isArray(context.biliIm.serverMessages)) {
                context.biliIm.messages = context.biliIm.serverMessages.slice();
            }
        },
    };
    context.globalThis = context;

    const code = [
        extract('biliImageSrc'),
        extract('biliFailure'),
        extract('renderBiliMessages'),
        extract('sendBiliMessage'),
        extract('sendBiliImageMessage'),
    ].join('\n\n');
    vm.createContext(context);
    vm.runInContext(code, context, { filename: 'app.js' });
    return { context, elements, toasts, refreshed, calls };
}

/* ------------------------------------------------------------------ tests */

// 1. a picture message shows the picture
{
    const { context, elements } = load({
        messages: [{
            content: '',
            is_self: false,
            image_url: 'https://i0.hdslb.com/bfs/im/abc.jpg',
            image_width: 800,
            image_height: 600,
        }],
    });
    context.renderBiliMessages();
    const panel = elements.get('bili-im-messages');
    const picture = panel.find((node) => node.tagName === 'IMG');
    check('a picture message renders an image', !!picture);
    check('the image points at the proxied address',
        !!picture && picture.src.includes(encodeURIComponent('https://i0.hdslb.com/bfs/im/abc.jpg')),
        picture ? picture.src : 'no image');
    check('the picture message carries no text',
        !panel.children.some((node) => node.tagName === 'SPAN'));
}

// 2. a text message still shows its text
{
    const { context, elements } = load({
        messages: [{ content: '晚上好', is_self: true, image_url: '' }],
    });
    context.renderBiliMessages();
    const panel = elements.get('bili-im-messages');
    const span = panel.find((node) => node.tagName === 'SPAN');
    check('a text message still shows its text', !!span && span.textContent === '晚上好');
    check('a text message draws no image',
        !panel.find((node) => node.tagName === 'IMG'));
}

// 3. the picture button sends the picture the user picked
{
    const sent = [];
    const { context, toasts, refreshed } = load({
        api: {
            choose_file: async () => ({ success: true, path: '/tmp/shot.png' }),
            bili_send_image_message: async (...args) => {
                sent.push(args);
                return { success: true, image_url: 'https://i0.hdslb.com/bfs/im/up.jpg' };
            },
        },
    });
    await context.sendBiliImageMessage();
    check('the picture is sent to the other side',
        sent.length === 1 && sent[0][0] === '123' && sent[0][1] === '/tmp/shot.png'
        && sent[0][2] === '900',
        JSON.stringify(sent));
    check('a success toast is shown',
        toasts.length === 1 && toasts[0].kind === 'success');
    check('the thread is refreshed after sending',
        refreshed.length === 1 && refreshed[0].talker_id === '123');
}

// 4. a failed send shows the error and does not pretend it worked
{
    const { context, toasts, refreshed } = load({
        api: {
            choose_file: async () => ({ success: true, path: '/tmp/shot.png' }),
            bili_send_image_message: async () => ({ success: false, message: '风控' }),
        },
    });
    await context.sendBiliImageMessage();
    check('a failed send shows an error',
        toasts.length === 1 && toasts[0].kind === 'error', JSON.stringify(toasts));
    check('a failed send does not refresh the thread', refreshed.length === 0);
}

// 5. without a conversation nothing is sent
{
    const sent = [];
    const { context, toasts } = load({
        talkerId: '',
        api: {
            choose_file: async () => ({ success: true, path: '/tmp/shot.png' }),
            bili_send_image_message: async (...args) => { sent.push(args); return { success: true }; },
        },
    });
    await context.sendBiliImageMessage();
    check('without a conversation nothing is sent', sent.length === 0);
    check('without a conversation the user is told', toasts.length === 1);
}

// 6. a cancelled file dialog sends nothing
{
    const sent = [];
    const { context, toasts } = load({
        api: {
            choose_file: async () => ({ success: false, path: '' }),
            bili_send_image_message: async (...args) => { sent.push(args); return { success: true }; },
        },
    });
    await context.sendBiliImageMessage();
    check('a cancelled dialog sends nothing', sent.length === 0);
    check('a cancelled dialog says nothing either', toasts.length === 0);
}

// 7. the bridge method the button needs is really called
{
    const { context, elements } = load({
        api: {},
    });
    await context.sendBiliImageMessage();
    check('without the bridge method nothing is sent',
        !elements.get('bili-im-messages').children.length);
}

// 8. a second click while the first one is on its way sends nothing
{
    const sent = [];
    let release = null;
    const { context } = load({
        api: {
            choose_file: async () => ({ success: true, path: '/tmp/shot.png' }),
            bili_send_image_message: async (...args) => {
                sent.push(args);
                // Hold the answer until the test lets it go.
                await new Promise((resolve) => { release = resolve; });
                return { success: true };
            },
        },
    });
    const first = context.sendBiliImageMessage();
    const second = context.sendBiliImageMessage();
    await new Promise((resolve) => setTimeout(resolve, 0));
    check('a second click does not send a second message', sent.length === 1,
        `sent ${sent.length} times`);
    if (release) release();
    await first;
    await second;
}

// 9. the same for a text message
{
    const sent = [];
    let release = null;
    const { context } = load({
        api: {
            bili_send_message: async (...args) => {
                sent.push(args);
                await new Promise((resolve) => { release = resolve; });
                return { success: true };
            },
        },
    });
    const input = new El('input');
    input.value = '晚上好';
    context.document.getElementById = (id) => (id === 'bili-im-input' ? input : null);
    const first = context.sendBiliMessage();
    const second = context.sendBiliMessage();
    await new Promise((resolve) => setTimeout(resolve, 0));
    check('a second click does not send a second text message', sent.length === 1,
        `sent ${sent.length} times`);
    if (release) release();
    await first;
    await second;
}

// 10. the message shows up right away, before the server answers
{
    const { context, toasts } = load({
        api: {
            bili_send_message: async () => {
                // The answer never comes before the check looks.
                await new Promise(() => {});
                return { success: true };
            },
        },
    });
    const input = new El('input');
    input.value = '晚上好';
    context.document.getElementById = (id) => (id === 'bili-im-input' ? input : null);
    context.sendBiliMessage();
    await new Promise((resolve) => setTimeout(resolve, 0));
    check('the message shows up before the answer arrives',
        context.biliIm.messages.length === 1
        && context.biliIm.messages[0].content === '晚上好',
        JSON.stringify(context.biliIm.messages));
    check('the input is cleared right away', input.value === '');
}

// 11. the message stays on the screen when the server has not counted it in
{
    const { context } = load({
        api: {
            bili_send_message: async () => ({ success: true }),
        },
    });
    context.biliIm.serverMessages = [{ content: '对方发的', is_self: false }];
    const input = new El('input');
    input.value = '晚上好';
    context.document.getElementById = (id) => (id === 'bili-im-input' ? input : null);
    await context.sendBiliMessage();
    check('the sent message stays visible',
        context.biliIm.messages.some((item) => item.content === '晚上好'),
        JSON.stringify(context.biliIm.messages));
}

// 12. a failed send puts the reason where it can be read
{
    const { context } = load({
        api: {
            bili_send_message: async () => ({ success: false, message: '被风控了' }),
        },
    });
    const input = new El('input');
    input.value = '晚上好';
    context.document.getElementById = (id) => (id === 'bili-im-input' ? input : null);
    await context.sendBiliMessage();
    check('a failed send says why', context.biliIm.error === '被风控了',
        String(context.biliIm.error));
    check('a failed send puts the text back',
        input.value === '晚上好', input.value);
}

// 13. a failed picture says why, too
{
    const { context } = load({
        api: {
            choose_file: async () => ({ success: true, path: '/tmp/shot.png' }),
            bili_send_image_message: async () => ({ success: false, message: '图片太大' }),
        },
    });
    await context.sendBiliImageMessage();
    check('a failed picture says why', context.biliIm.error === '图片太大',
        String(context.biliIm.error));
}

console.log();
if (failures) {
    console.log(`${failures} check(s) failed`);
    process.exit(1);
}
console.log('all checks passed');
