#!/usr/bin/env node
/**
 * 收藏夹分页的检查。
 *
 * 改动前：收藏夹是「累加 + 加载更多」。翻到第 8 页时前面 7 页两百多条
 * 全留在 DOM 里，越往后越卡；而且翻不回已经看过的位置，只有一条单行道。
 * 每页条数也写死成 30，改不了。
 *
 * 改成页码之后要证明的事：
 *
 *   1. 第一页        - 打开收藏夹读第 1 页，按默认每页条数。
 *   2. 整页替换      - 翻页换掉整张列表，不是往后追加。
 *   3. 上一页回去    - 能往回翻。
 *   4. 页码标签      - 显示「第 N / M 页」，M 由总数算出来。
 *   5. 每页条数      - 改了就回第一页重读，条数传给后端。
 *   6. 上限 40       - 传 100 出去还是 40（接口 ps=41 会回 code -400）。
 *   7. 到头禁用      - 第一页没有上一页，最后一页没有下一页。
 *   8. 换页清勾选    - 否则批量操作会对着看不见的条目下手。
 *   9. 没有总数      - 只显示「第 N 页」，下一页由 has_more 决定。
 *  10. 结构          - 分页控件在 HTML 里，"加载更多"按钮已经拿掉。
 *
 * Run:  node scripts/check-bili-fav-paging.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const appPath = path.join(root, 'src', 'gui', 'assets', 'app.js');

/* app.js 启动时会碰 DOM，这里给它一套最小的假 DOM。 */
function makeElement() {
    const store = {
        dataset: {}, style: { setProperty() {}, removeProperty() {} },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        addEventListener() {}, append() {},
        appendChild(c) { this.children.push(c); return c; },
        removeChild() {}, removeAttribute() {}, setAttribute() {},
        getAttribute: () => null,
        querySelector: () => null, querySelectorAll: () => [],
        closest: () => null, scrollTo() {}, replaceChildren() {},
        textContent: '', innerHTML: '', hidden: false, disabled: false,
        children: [], tagName: 'SPAN', value: '',
    };
    return new Proxy(store, {
        get: (target, prop) => (prop in target ? target[prop] : undefined),
        set: (target, prop, value) => { target[prop] = value; return true; },
    });
}

const elementsById = new Map();
function getElementById(id) {
    if (!elementsById.has(id)) elementsById.set(id, makeElement());
    return elementsById.get(id);
}

/* 真实的译文。t() 拿不到译文会原样返回键名，那样断言"第 2 / 4 页"
   就永远不可能对——测的就是键名了。 */
function flattenTranslations(node, prefix = '') {
    const out = {};
    for (const [key, value] of Object.entries(node)) {
        const name = prefix ? `${prefix}.${key}` : key;
        if (value && typeof value === 'object' && !Array.isArray(value)) {
            Object.assign(out, flattenTranslations(value, name));
        } else {
            out[name] = String(value);
        }
    }
    return out;
}
const zhPath = path.join(root, 'src', 'i18n', 'zh_cn.json');
const translations = flattenTranslations(JSON.parse(fs.readFileSync(zhPath, 'utf8')));

const sandbox = {
    console,
    __translations: translations,
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
        createElementNS: (ns, tag) => {
            const el = makeElement();
            el.tagName = String(tag || 'svg').toUpperCase();
            el.namespaceURI = ns;
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
    navigator: { onLine: true, language: 'zh-CN', userAgent: 'node' },
    localStorage: {
        getItem: () => null, setItem() {}, removeItem() {}, clear() {},
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
    globalThis.__done = (async () => {
        const results = {calls: []};

        // 一个 4 页的收藏夹，每页 30 条，总共 100 条（最后页不满）。
        const TOTAL = 100;
        const pageOf = (page, size) => {
            const from = (page - 1) * size;
            const to = Math.min(from + size, TOTAL);
            const rows = [];
            for (let i = from; i < to; i += 1) {
                rows.push({
                    bvid: 'BV' + i,
                    aid: i + 1,
                    title: '视频 ' + i,
                    author: 'UP',
                    cover: '',
                    duration: 60,
                });
            }
            return rows;
        };

        state.api = {
            get_bilibili_favorite_videos: (folderId, page, pageSize) => {
                const number = Number(page) || 1;
                const size = Number(pageSize) || 30;
                results.calls.push({page: number, size, folder: folderId});
                const rows = pageOf(number, size);
                return Promise.resolve({
                    success: true,
                    items: rows,
                    page: number,
                    has_more: number * size < TOTAL,
                    total: results.hideTotal ? 0 : TOTAL,
                });
            },
        };

        // t() 用的是 state.translations；没有它标签就只会显示键名。
        state.translations = globalThis.__translations || {};

        // renderBiliFavorites 在没有登录 / 没有收藏夹时提前返回，
        // 分页条就画不出来。这两个条件先摆好。
        biliAccount.loggedIn = true;
        biliFavorites.folders = [{id: '123', title: '默认收藏夹', count: TOTAL}];
        biliFavorites.folderId = '123';
        biliFavorites.page = 1;
        biliFavorites.pageSize = 30;
        biliFavorites.items = [];

        // ---- 1: 第一页 ---------------------------------------------------
        await loadBiliFavoritePage(1);
        results.firstCall = results.calls[0];
        results.firstLen = biliFavorites.items.length;
        results.firstPage = biliFavorites.page;
        results.firstIds = biliFavorites.items.map((t) => t.bvid);

        // ---- 2 和 3: 下一页整页替换 --------------------------------------
        await gotoBiliFavoritePage(2);
        results.secondCall = results.calls[results.calls.length - 1];
        results.secondLen = biliFavorites.items.length;
        results.secondPage = biliFavorites.page;
        results.secondIds = biliFavorites.items.map((t) => t.bvid);

        await gotoBiliFavoritePage(1);
        results.backPage = biliFavorites.page;
        results.backIds = biliFavorites.items.map((t) => t.bvid);

        // ---- 4: 页码标签 -------------------------------------------------
        await loadBiliFavoritePage(2);
        renderBiliFavorites();
        results.label = document.getElementById('bili-fav-page-label').textContent;
        results.pagerHidden = document.getElementById('bili-fav-pager').hidden;

        // ---- 5: 每页条数 -------------------------------------------------
        results.callsBefore = results.calls.length;
        setBiliFavPageSize(10);
        await new Promise((r) => setTimeout(r, 0));
        results.sizeCall = results.calls[results.calls.length - 1];
        results.sizePage = biliFavorites.page;
        results.sizeLen = biliFavorites.items.length;
        results.sizeLabel = document.getElementById('bili-fav-page-label').textContent;

        // ---- 6: 超过 40 被夹住 -------------------------------------------
        await loadBiliFavoritePage(2);
        results.beforeClamp = biliFavorites.pageSize;
        setBiliFavPageSize(100);
        await new Promise((r) => setTimeout(r, 0));
        results.clampedCall = results.calls[results.calls.length - 1];
        results.afterClamp = biliFavorites.pageSize;

        // ---- 7: 到头禁用 -------------------------------------------------
        await loadBiliFavoritePage(1);
        results.prevDisabledOnFirst = document.getElementById('btn-bili-fav-prev').disabled;
        results.nextDisabledOnFirst = document.getElementById('btn-bili-fav-next').disabled;
        // 100 / 40 = 3 页；第 3 页没有下一页
        await loadBiliFavoritePage(3);
        results.prevDisabledOnLast = document.getElementById('btn-bili-fav-prev').disabled;
        results.nextDisabledOnLast = document.getElementById('btn-bili-fav-next').disabled;

        // ---- 8: 换页清空勾选 ---------------------------------------------
        await loadBiliFavoritePage(1);
        biliFavorites.selected.add(biliFavorites.items[0].bvid);
        results.selectedBefore = biliFavorites.selected.size;
        await gotoBiliFavoritePage(2);
        results.selectedAfter = biliFavorites.selected.size;

        // ---- 9: 总数拿不到 -----------------------------------------------
        results.hideTotal = true;
        await loadBiliFavoritePage(1);
        results.unknownLabel = document.getElementById('bili-fav-page-label').textContent;
        results.unknownNext = document.getElementById('btn-bili-fav-next').disabled;
        await loadBiliFavoritePage(1);  // 第 1 页还有下一页
        results.unknownNextFirst = document.getElementById('btn-bili-fav-next').disabled;

        globalThis.__results = results;
    })();
`;

const iconsPath = path.join(root, 'src', 'gui', 'assets', 'icons.js');

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

// ---- 1: 第一页 -----------------------------------------------------------
check('打开收藏夹读第 1 页', r.firstCall && r.firstCall.page === 1,
    JSON.stringify(r.firstCall));
check('默认每页 30 条', r.firstCall && r.firstCall.size === 30,
    JSON.stringify(r.firstCall));
check('第一页装了一页的量', r.firstLen === 30, `${r.firstLen} 条`);

// ---- 2: 整页替换 ---------------------------------------------------------
check('下一页读第 2 页', r.secondCall && r.secondCall.page === 2,
    JSON.stringify(r.secondCall));
check('翻页是整页替换，不是累加', r.secondLen === 30, `${r.secondLen} 条（累加会是 60）`);
check('第二页的内容是第 31 条起',
    r.secondIds && r.secondIds[0] === 'BV30',
    `第一条 ${r.secondIds && r.secondIds[0]}`);
check('第一页的内容已经不在了',
    r.secondIds && !r.secondIds.includes('BV0'),
    'BV0 仍在列表里');

// ---- 3: 往回翻 -----------------------------------------------------------
check('能翻回第一页', r.backPage === 1, `page=${r.backPage}`);
check('翻回来内容也换回来了', r.backIds && r.backIds[0] === 'BV0',
    `第一条 ${r.backIds && r.backIds[0]}`);

// ---- 4: 页码标签 ---------------------------------------------------------
check('标签写着第几页 / 共几页',
    r.label === '第 2 / 4 页', JSON.stringify(r.label));
check('分页条是显示着的', r.pagerHidden === false, `hidden=${r.pagerHidden}`);

// ---- 5: 每页条数 ---------------------------------------------------------
check('改每页条数会重新读', r.sizeCall && r.sizeCall.size === 10,
    JSON.stringify(r.sizeCall));
check('改每页条数回到第一页', r.sizePage === 1, `page=${r.sizePage}`);
check('一页就只有 10 条', r.sizeLen === 10, `${r.sizeLen} 条`);
check('页数按新条数重算', r.sizeLabel === '第 1 / 10 页', JSON.stringify(r.sizeLabel));

// ---- 6: 上限 40 ----------------------------------------------------------
check('传 100 出去被夹到 40',
    r.clampedCall && r.clampedCall.size === 40,
    JSON.stringify(r.clampedCall));
check('内部记的也是 40', r.afterClamp === 40, `${r.afterClamp}`);

// ---- 7: 到头禁用 ---------------------------------------------------------
check('第一页没有上一页', r.prevDisabledOnFirst === true,
    `disabled=${r.prevDisabledOnFirst}`);
check('第一页有下一页', r.nextDisabledOnFirst === false,
    `disabled=${r.nextDisabledOnFirst}`);
check('最后一页有上一页', r.prevDisabledOnLast === false,
    `disabled=${r.prevDisabledOnLast}`);
check('最后一页没有下一页', r.nextDisabledOnLast === true,
    `disabled=${r.nextDisabledOnLast}`);

// ---- 8: 换页清勾选 -------------------------------------------------------
check('翻页前勾上了', r.selectedBefore === 1, `${r.selectedBefore}`);
check('翻页后勾选被清空', r.selectedAfter === 0, `${r.selectedAfter}`);

// ---- 9: 没有总数 ---------------------------------------------------------
check('没有总数时只写第几页',
    r.unknownLabel === '第 1 页', JSON.stringify(r.unknownLabel));
check('没有总数时下一页靠 has_more 决定', r.unknownNextFirst === false,
    `disabled=${r.unknownNextFirst}`);

// ---- 10: 结构 ------------------------------------------------------------
const html = fs.readFileSync(path.join(root, 'src', 'gui', 'assets', 'index.html'), 'utf8');

/* 只看分页块本身。整份 HTML 里还有别的 <select>（设置页那些），
   拿整个文件匹配 option 会把它们算进来。 */
const pagerAt = html.indexOf('id="bili-fav-pager"');
check('HTML 里有分页块', pagerAt >= 0, pagerAt >= 0 ? '' : 'not found');
const pagerBlock = pagerAt >= 0
    ? html.slice(html.lastIndexOf('<div', pagerAt), html.indexOf('</div>', pagerAt) + 6)
    : '';

for (const id of ['bili-fav-pager', 'btn-bili-fav-prev', 'btn-bili-fav-next',
                  'bili-fav-page-label', 'bili-fav-page-size']) {
    check(`HTML 里有 ${id}`, html.includes(`id="${id}"`),
        html.includes(`id="${id}"`) ? '' : 'not found');
}
check('"加载更多"按钮已经从收藏夹里拿掉',
    !html.includes('id="btn-bili-fav-more"'),
    html.includes('id="btn-bili-fav-more"') ? 'still there' : '');

const sizeOptions = ['10', '20', '30', '40'];
for (const size of sizeOptions) {
    const found = new RegExp(`<option value="${size}"`).test(pagerBlock);
    check(`每页条数有 ${size} 这个选项`, found, found ? '' : 'not found in the pager');
}
const tooBig = pagerBlock.match(/<option value="(\d+)"/g) || [];
const over = tooBig
    .map((raw) => Number(raw.replace(/[^\d]/g, '')))
    .filter((n) => n > 40);
check('分页块里没有超过 40 的选项', over.length === 0,
    over.length ? `多出来的: ${over.join(',')}` : '');

console.log('');
if (failures.length) {
    console.error(`FAILED: ${failures.length} check(s)`);
    process.exit(1);
}
console.log('all favourite paging checks passed');
