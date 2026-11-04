#!/usr/bin/env node
/**
 * 检查设置里的「B站默认画质」是不是真的用在了播放上。
 *
 * 为什么不是 pytest：这段逻辑在 app.js 里，只有把文件载进去、真的调用
 * playBiliTrack 才能验证。
 *
 * 出过的错：设置里存了 quality，下载那边读它，播放这边三个入口全部写死
 * 0——不管设成 1080P 还是 4K，一播就是「自动」，看起来像设置没保存。而且
 * 播放器下拉框在「请求的画质不在名单里」时直接回落到「自动」，用户永远
 * 不知道自己实际在看什么。
 *
 * 这里查五件事：
 *   1. 内联播放器  - playBiliTrack 用设置里的画质去取流。
 *   2. 独立窗口    - 打开窗口时把同一个画质传过去。
 *   3. 番剧        - playBangumiShow 也用设置里的画质。
 *   4. 没设置      - 设置是 0 时就老老实实要 0（自动）。
 *   5. 下拉框显示   - 请求的画质不在名单里时，显示真正在播的那个。
 *
 * Run:  node scripts/check-bili-default-quality.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const appPath = path.join(root, 'src', 'gui', 'assets', 'app.js');

/* app.js touches the DOM while booting, which never happens here. */
function makeElement() {
    const store = {
        dataset: {}, style: { setProperty() {}, removeProperty() {} },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        addEventListener() {}, append() {},
        appendChild(c) { this.children.push(c); return c; },
        removeChild() {}, removeAttribute() {}, setAttribute() {},
        remove() {}, focus() {}, blur() {},
        getAttribute: () => null,
        querySelector: () => null, querySelectorAll: () => [],
        closest: () => null, scrollTo() {}, replaceChildren() {},
        textContent: '', innerHTML: '', hidden: false, disabled: false,
        children: [], tagName: 'SPAN', value: '', options: [],
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

const sandbox = {
    console,
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
        // 取流走的是这个共用加载器。这里只留下「把请求转给 bridge」这一件
        // 事——要验的正是它拿到的那两个参数（bvid、quality）。
        BiliStreamLoader: {
            load: async (opts) => {
                await opts.api.get_bilibili_video_streams(opts.bvid, opts.quality);
                return { ok: false, resumed: 0 };
            },
            play: () => Promise.resolve(false),
        },
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

/* The harness runs inside the same context as app.js. */
const harness = `
    globalThis.__done = (async () => {
        const results = {streamCalls: [], windowCalls: [], bangumiCalls: []};

        // 任何没写明的方法都回一个失败的结果：这样别的初始化调用不会炸。
        function bridge(overrides) {
            return new Proxy(overrides, {
                get: (target, prop) => {
                    if (prop in target) return target[prop];
                    return () => Promise.resolve({success: false});
                },
            });
        }

        // ---- 1 内联播放器：用设置里的 80（1080P）-------------------------
        state.settings = {bili: {quality: 80, separate_player_window: false}};
        state.api = bridge({
            get_bilibili_video_streams: (bvid, quality) => {
                results.streamCalls.push({bvid, quality});
                return Promise.resolve({success: false});
            },
        });
        await playBiliTrack({bvid: 'BV1aa', name: 'video', artists: ''});

        // ---- 2 独立窗口：同一个画质要传给窗口 ----------------------------
        state.settings = {bili: {quality: 112, separate_player_window: true}};
        state.api = bridge({
            open_bili_player_window: (bvid, page, quality) => {
                results.windowCalls.push({bvid, page, quality});
                return Promise.resolve({success: true});
            },
        });
        await playBiliTrack({bvid: 'BV1bb', name: 'video', artists: ''});

        // ---- 3 番剧 ------------------------------------------------------
        state.settings = {bili: {quality: 64}};
        state.api = bridge({
            get_bili_bangumi_streams: (seasonId, episodeId, quality) => {
                results.bangumiCalls.push({seasonId, episodeId, quality});
                return Promise.resolve({success: false});
            },
        });
        await playBangumiShow({season_id: 26257, ep_id: 3001, title: '三体'});

        // ---- 4 没设置：还是 0 --------------------------------------------
        state.settings = {bili: {separate_player_window: false}};
        state.api = bridge({
            get_bilibili_video_streams: (bvid, quality) => {
                results.streamCalls.push({bvid, quality});
                return Promise.resolve({success: false});
            },
        });
        await playBiliTrack({bvid: 'BV1cc', name: 'video', artists: ''});

        // ---- 5 下拉框：请求 112，视频只有 64 ------------------------------
        const select = document.getElementById('bili-quality');
        biliPlayer.quality = 112;
        renderBiliQualities({
            qualities: [{id: 64, name: '720P', available: true}],
            picked_quality: 64,
        });
        results.shownQuality = select.value;

        // 请求的画质在名单里时，就显示请求的那个。
        biliPlayer.quality = 64;
        renderBiliQualities({
            qualities: [{id: 64, name: '720P', available: true}],
            picked_quality: 64,
        });
        results.shownExact = select.value;

        return results;
    })();
`;

// The page loads icons.js before app.js; openBiliDetail() calls setIcon,
// so the check needs the same two files in the same order.
const iconsPath = path.join(root, 'src', 'gui', 'assets', 'icons.js');

vm.createContext(sandbox);
vm.runInContext(
    fs.readFileSync(iconsPath, 'utf8') + '\n'
    + fs.readFileSync(appPath, 'utf8') + '\n' + harness,
    sandbox,
    { filename: 'app.js' },
);

let results;
try {
    results = await sandbox.__done;
} catch (err) {
    console.error('the harness threw:', err && err.message);
    console.error(err && err.stack);
    process.exit(1);
}

check('内联播放器用设置里的画质（1080P=80）',
    results.streamCalls[0] && results.streamCalls[0].quality === 80,
    `quality=${results.streamCalls[0] && results.streamCalls[0].quality}`);
check('独立窗口拿到同一个画质（1080P+=112）',
    results.windowCalls[0] && results.windowCalls[0].quality === 112,
    `quality=${results.windowCalls[0] && results.windowCalls[0].quality}`);
check('番剧播放用设置里的画质（720P=64）',
    results.bangumiCalls[0] && results.bangumiCalls[0].quality === 64,
    `quality=${results.bangumiCalls[0] && results.bangumiCalls[0].quality}`);
check('设置是 0 时请求 0',
    results.streamCalls[1] && results.streamCalls[1].quality === 0,
    `quality=${results.streamCalls[1] && results.streamCalls[1].quality}`);
check('请求的画质不在名单里时显示实际在播的那个',
    results.shownQuality === '64',
    `shown=${results.shownQuality}`);
check('请求的画质在名单里时显示它',
    results.shownExact === '64',
    `shown=${results.shownExact}`);

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed: ${failures.join('; ')}`);
    process.exit(1);
}
console.log('\nall checks passed');
// app.js 里还有几个 toast 的定时器排在后面，显式收尾，别让它们把退出码
// 弄成失败。
process.exit(0);
