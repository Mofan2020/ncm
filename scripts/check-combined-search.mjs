#!/usr/bin/env node
/**
 * 聚合搜索里网易云那一半的回归检查。
 *
 * 用户报过：聚合搜索里网易云搜不了用户、歌曲（单曲）、歌单。
 *
 * 这个脚本把 app.js 整个加载进一个 vm（DOM 是假的），给
 * `state.api.search_multi` 喂一份**真桥接返回过的形状**的数据，然后真的
 * 跑一遍聚合搜索，看 `combined.rows` 里到底有没有东西。
 *
 * 为什么不是 pytest：聚合搜索的活儿全在前端，只有把 app.js 加载起来
 * 跑一遍才算数。
 *
 * 跑：node scripts/check-combined-search.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const appPath = path.join(root, 'src', 'gui', 'assets', 'app.js');

/* app.js 一加载就会碰 DOM。这个桩只要能撑住加载和调用就行。 */
function makeElement() {
    const store = {
        dataset: {}, style: { setProperty() {}, removeProperty() {} },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        addEventListener() {}, appendChild(c) { return c; }, append() {},
        removeAttribute() {}, setAttribute() {}, querySelector: () => null,
        querySelectorAll: () => [], closest: () => null, scrollTo() {},
        innerHTML: '', textContent: '', value: '', hidden: false,
        options: [], children: [],
    };
    return new Proxy(store, {
        get: (target, prop) => (prop in target ? target[prop] : undefined),
        set: (target, prop, value) => { target[prop] = value; return true; },
    });
}

const sandbox = {
    console,
    Math, Date, JSON, Object, Array, String, Number, Boolean, isFinite, parseInt, parseFloat,
    setTimeout, clearTimeout, setInterval, clearInterval,
    document: {
        getElementById: () => makeElement(),
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: () => makeElement(),
        createElementNS: () => makeElement(),
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
    Image: class { constructor() { this.crossOrigin = null; } },
};

const failures = [];
function check(label, condition, detail) {
    const status = condition ? 'PASS' : 'FAIL';
    console.log(`[${status}] ${label}${detail ? ' -- ' + detail : ''}`);
    if (!condition) failures.push(label);
}

/* 桥接返回过的形状：songs 是 _track_payload 的结果，playlists 是
 * _playlist_payload 的结果，users 是 userprofiles 那种。 */
const PAYLOAD = {
    success: true,
    keyword: '周杰伦',
    results: {
        songs: [
            { id: 1, name: '晴天', artists: '周杰伦', album: '叶惠美',
              cover_url: 'http://x/1.jpg', duration: 269000, play_count: 100 },
            { id: 2, name: '稻香', artists: '周杰伦', album: '魔杰座',
              cover_url: 'http://x/2.jpg', duration: 223000, play_count: 90 },
        ],
        playlists: [
            { id: 10, name: '周杰伦精选', creator: '某个用户',
              cover_url: 'http://x/p.jpg', track_count: 30 },
        ],
        artists: [
            { id: 100, name: '周杰伦', cover_url: 'http://x/a.jpg' },
        ],
        albums: [
            { id: 200, name: '叶惠美', cover_url: 'http://x/al.jpg' },
        ],
        users: [
            { id: 300, name: '爱听歌的人', avatar_url: 'http://x/u.jpg' },
        ],
    },
};

vm.createContext(sandbox);
/* 图标是另一个文件里的（icons.js），app.js 调它。 */
vm.runInContext(fs.readFileSync(path.join(root, 'src', 'gui', 'assets', 'icons.js'), 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(appPath, 'utf8'), sandbox);

const harness = `
    state.api = {
        search_multi: async () => PAYLOAD,
        search_bilibili_tracks: async () => ({ success: true, tracks: [] }),
        search_bilibili_pgc: async () => ({ success: true, results: [] }),
        pixiv_search: async () => ({ success: true, works: [] }),
    };
    await renderCombinedSearch('周杰伦', {
        source: 'netease', category: 'all', sort: 'relevance',
        duration: 'all', partition: 0,
    });
    const rows = combined.rows;
    const results = {
        kinds: rows.map((row) => row.kind),
        names: rows.map((row) => row.name),
        neteaseError: combined.neteaseError || '',
        loading: combined.loading,
    };
`;
sandbox.PAYLOAD = PAYLOAD;
sandbox.__harness = harness;

const results = await vm.runInContext(`(async () => { ${harness} return results; })()`, sandbox);

console.log(`聚合搜索拿到的行：${JSON.stringify(results.kinds)} 名字：${JSON.stringify(results.names)}`);
check('聚合搜索没有报错', results.neteaseError === '', results.neteaseError);
check('搜到了单曲', results.kinds.includes('song'), JSON.stringify(results.kinds));
check('搜到了歌单', results.kinds.includes('playlist'), JSON.stringify(results.kinds));
check('搜到了歌手', results.kinds.includes('artist'), JSON.stringify(results.kinds));
check('搜到了专辑', results.kinds.includes('album'), JSON.stringify(results.kinds));
check('搜到了用户', results.kinds.includes('user'), JSON.stringify(results.kinds));

if (failures.length) {
    console.log(`\n${failures.length} 项没过。`);
    process.exit(1);
}
console.log('\n全部通过。');
