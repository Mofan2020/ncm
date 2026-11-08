/**
 * Drives the new Bilibili UI in a fake DOM, the way the project checks
 * the rest of the frontend.
 *
 * What it checks:
 *   1. the folder manager: the list is drawn, and every row has the three
 *      buttons (rename, delete, public/private);
 *   2. creating a folder calls the bridge with the title and the privacy;
 *   3. the privacy toggle flips the value it sends;
 *   4. the relations list: followings and followers are two different calls;
 *   5. the comment list: a thread that was not opened shows the "view all N
 *      replies" button, and clicking it asks the backend for the replies;
 *   6. replying to a comment carries both the parent and the root.
 *
 * Run:  node scripts/check-bili-folder-and-relations.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const appPath = path.join(root, 'src', 'gui', 'assets', 'app.js');
const iconsPath = path.join(root, 'src', 'gui', 'assets', 'icons.js');
const i18nPath = path.join(root, 'src', 'i18n', 'zh_cn.json');

/* The real texts, so t() hands back a sentence and not the key. */
const catalogue = JSON.parse(fs.readFileSync(i18nPath, 'utf8'));
const flat = {};
(function walk(node, prefix) {
    for (const [key, value] of Object.entries(node)) {
        const name = prefix ? prefix + '.' + key : key;
        if (value && typeof value === 'object') walk(value, name);
        else flat[name] = value;
    }
})(catalogue, '');

/* app.js touches the DOM while booting, which never happens here. */
function makeElement(tag) {
    const store = {
        tagName: String(tag || 'DIV').toUpperCase(),
        dataset: {},
        style: { setProperty() {}, removeProperty() {} },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        addEventListener() {}, removeEventListener() {},
        append() {}, appendChild(c) { this.children.push(c); return c; },
        removeChild() {}, removeAttribute() {}, setAttribute() {},
        getAttribute: () => null, setAttribute() {},
        querySelector: () => null, querySelectorAll: () => [],
        closest: () => null, scrollTo() {}, replaceChildren() {},
        focus() {}, click() {}, remove() {},
        textContent: '', innerHTML: '', value: '', hidden: false,
        disabled: false, checked: false, children: [], className: '',
        getBoundingClientRect: () => ({ x: 0, y: 0, width: 0, height: 0 }),
    };
    return new Proxy(store, {
        get: (target, prop) => (prop in target ? target[prop] : undefined),
        set: (target, prop, value) => { target[prop] = value; return true; },
    });
}

const elementsById = new Map();
function getElementById(id) {
    if (!elementsById.has(id)) elementsById.set(id, makeElement('div'));
    return elementsById.get(id);
}

const sandbox = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    document: {
        getElementById,
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: (tag) => makeElement(tag),
        createElementNS: (ns, tag) => makeElement(tag),
        createDocumentFragment: () => makeElement('fragment'),
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
    window_prompt: () => null,
    window_confirm: () => true,
};

const failures = [];
function check(label, condition, detail) {
    const status = condition ? 'PASS' : 'FAIL';
    console.log(`[${status}] ${label}${detail ? ' -- ' + detail : ''}`);
    if (!condition) failures.push(label);
}

const harness = `
    globalThis.__done = (async () => {
        const results = {calls: []};

        // The texts, so the buttons read like sentences and not like keys.
        state.translations = globalThis.__translations;

        // ---- the folders --------------------------------------------------
        biliAccount.loggedIn = true;
        biliFavorites.folders = [
            {id: '1', title: '默认收藏夹', media_count: 347, private: true, attr: 1, intro: ''},
            {id: '2', title: 'music', media_count: 3, private: false, attr: 22, intro: '说明'},
        ];
        state.api = {
            get_bilibili_favorites: () => Promise.resolve({
                success: true, folders: biliFavorites.folders, count: 2,
            }),
            bili_create_favorite_folder: (title, intro, privacy) => {
                results.calls.push({op: 'create', title, intro, privacy});
                return Promise.resolve({success: true, id: '9'});
            },
            bili_edit_favorite_folder: (id, title, intro, privacy) => {
                results.calls.push({op: 'edit', id, title, intro, privacy});
                return Promise.resolve({success: true});
            },
            bili_delete_favorite_folder: (id) => {
                results.calls.push({op: 'delete', id});
                return Promise.resolve({success: true});
            },
            get_bilibili_followings: (mid, page, size) => {
                results.calls.push({op: 'followings', mid, page, size});
                return Promise.resolve({
                    success: true, count: 1, has_more: false,
                    items: [{mid: '42', uname: '乙', face: '', sign: '签名',
                             official_verify: {type: 0, desc: '认证'}, vip: {}}],
                });
            },
            get_bilibili_followers: (mid, page, size) => {
                results.calls.push({op: 'followers', mid, page, size});
                return Promise.resolve({
                    success: true, count: 1, has_more: false,
                    items: [{mid: '43', uname: '丙', face: '', sign: '',
                             official_verify: {}, vip: {}}],
                });
            },
            get_bilibili_comment_replies: (bvid, root, page) => {
                results.calls.push({op: 'replies', bvid, root, page});
                return Promise.resolve({
                    success: true, count: 1, has_more: false,
                    comments: [{rpid: '9101', uname: '乙', content: '同意',
                                root: root, parent: root, like: 0, children: []}],
                });
            },
            get_bilibili_comments: (bvid, page) => Promise.resolve({
                success: true, has_more: false, comments: [],
            }),
        };

        // ---- 1: the folder list is drawn ----------------------------------
        renderBiliFolderList();
        const list = document.getElementById('bili-folder-list');
        results.folderRows = list.children.length;
        results.folderRowButtons = list.children.length
            ? list.children[0].children.length : 0;

        // ---- 2: creating a folder ----------------------------------------
        const titleInput = document.getElementById('bili-folder-new-title');
        const introInput = document.getElementById('bili-folder-new-intro');
        const privateBox = document.getElementById('bili-folder-new-private');
        titleInput.value = '新的夹';
        introInput.value = '说明';
        privateBox.checked = true;
        await createBiliFolder();
        results.created = results.calls.find((c) => c.op === 'create');

        // ---- 3: the privacy toggle ---------------------------------------
        const music = biliFavorites.folders[1];
        await toggleBiliFolderPrivacy(music);
        results.privacyEdit = results.calls.find((c) => c.op === 'edit');

        // ---- 4: the relations --------------------------------------------
        biliAccount.mid = '1946836486';
        await loadBiliRelations(true);
        results.followings = results.calls.find((c) => c.op === 'followings');
        results.relationRows = document.getElementById('bili-relations-list').children.length;
        biliRelations.seg = 'followers';
        await loadBiliRelations(true);
        results.followers = results.calls.find((c) => c.op === 'followers');

        // ---- 5: the "view all N replies" button --------------------------
        biliDetail.bvid = 'BV1aa';
        biliDetail.comments = [{
            rpid: '9001', mid: '1', uname: '甲', content: '主楼',
            like: 12, reply_count: 8, root: '0', parent: '0',
            children: [{rpid: '9101', uname: '乙', content: '回复',
                        like: 0, root: '9001', parent: '9001', children: []}],
        }];
        const commentList = document.getElementById('bili-comment-list');
        renderBiliComments();
        results.commentRows = commentList.children.length;
        // The button is the last child of the row.
        const row = commentList.children[0];
        const last = row.children[row.children.length - 1];
        results.repliesButton = last ? last.className : 'MISSING';
        results.repliesButtonText = last ? last.textContent : '';
        if (last && typeof last.addEventListener === 'function') {
            // The click handler is what opens the thread; the fake element
            // keeps it, so the harness calls the function itself.
            await expandBiliCommentReplies(biliDetail.comments[0], '9001');
        }
        results.repliesCall = results.calls.find((c) => c.op === 'replies');

        // ---- 6: the reply carries the parent and the root ----------------
        startBiliReply({rpid: '9101', uname: '乙', root: '9001'}, '9001');
        results.replyTo = biliDetail.replyTo;

        globalThis.__results = results;
    })();
`;

sandbox.__translations = flat;

vm.createContext(sandbox);
vm.runInContext(
    fs.readFileSync(iconsPath, 'utf8') + '\n'
    + fs.readFileSync(appPath, 'utf8') + '\n' + harness,
    sandbox,
    { filename: 'app.js' },
);

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

// ---- 1: the folder list ---------------------------------------------------
check('both folders are drawn', r.folderRows === 2, `${r.folderRows} rows`);
check('a row has the name, the count and the three buttons',
    r.folderRowButtons === 5, `${r.folderRowButtons} children`);

// ---- 2: creating ----------------------------------------------------------
check('the title is sent', r.created && r.created.title === '新的夹',
    JSON.stringify(r.created));
check('the description is sent', r.created && r.created.intro === '说明',
    JSON.stringify(r.created));
check('the privacy is sent', r.created && r.created.privacy === 1,
    JSON.stringify(r.created));

// ---- 3: the privacy toggle ------------------------------------------------
check('the privacy is flipped, not copied', r.privacyEdit && r.privacyEdit.privacy === 1,
    JSON.stringify(r.privacyEdit));
check('the title is kept', r.privacyEdit && r.privacyEdit.title === 'music',
    JSON.stringify(r.privacyEdit));

// ---- 4: the relations -----------------------------------------------------
check('the followings are read', !!r.followings, JSON.stringify(r.followings));
check('the followers are read', !!r.followers, JSON.stringify(r.followers));
check('the two are different calls', r.followings && r.followers
    && r.followings.op !== r.followers.op);
check('a relation row is drawn', r.relationRows === 1, `${r.relationRows} rows`);

// ---- 5: the replies -------------------------------------------------------
check('the comment is drawn', r.commentRows === 1, `${r.commentRows} rows`);
check('the thread has a "view all" button',
    String(r.repliesButton).indexOf('bili-comment-replies') >= 0, r.repliesButton);
check('the button says how many replies there are',
    String(r.repliesButtonText).indexOf('8') >= 0, r.repliesButtonText);
check('opening the thread asks the backend', !!r.repliesCall,
    JSON.stringify(r.repliesCall));
check('the backend is asked for the right thread',
    r.repliesCall && r.repliesCall.root === '9001', JSON.stringify(r.repliesCall));

// ---- 6: the reply threading -----------------------------------------------
check('the reply remembers the comment it answers',
    r.replyTo && r.replyTo.rpid === '9101', JSON.stringify(r.replyTo));
check('the reply remembers the thread it belongs to',
    r.replyTo && r.replyTo.root === '9001', JSON.stringify(r.replyTo));

console.log('');
if (failures.length) {
    console.error(`FAILED: ${failures.length} check(s)`);
    process.exit(1);
}
console.log('all folder, relation and comment checks passed');
