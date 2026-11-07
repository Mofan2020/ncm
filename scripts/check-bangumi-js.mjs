#!/usr/bin/env node
/**
 * Checks the Bangumi page in src/gui/assets/app.js.
 *
 * Why this is not a pytest test: the logic lives in the frontend, so the
 * only honest way to check it is to load app.js and drive it.
 *
 * What is checked here:
 *
 *   1. 搜索        - a keyword goes to the bridge and the results come back.
 *   2. 搜索失败    - a failed search shows an error, not a blank page.
 *   3. 本地收藏    - the favourites are read from the bridge and drawn.
 *   4. 观看历史    - the history is read and drawn.
 *   5. 选集        - the episode list of one subject is drawn.
 *   6. 收藏切换    - the favourite button flips the local state.
 *   7. 资源链接    - the links are rendered, and they are the ones the
 *                    bridge handed over.
 *   8. 不发账号请求 - nothing here asks for an account: Bangumi has none.
 *
 * Run:  node scripts/check-bangumi-js.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const appPath = path.join(root, 'src', 'gui', 'assets', 'app.js');

/* app.js touches the DOM while booting, which never happens here. */
function makeElement() {
    const classes = new Set();
    const store = {
        dataset: {}, style: { setProperty() {}, removeProperty() {} },
        classList: {
            add(...names) { names.forEach((n) => classes.add(n)); },
            remove(...names) { names.forEach((n) => classes.delete(n)); },
            toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
            contains: (name) => classes.has(name),
        },
        addEventListener() {}, append() {},
        appendChild(c) { this.children.push(c); return c; },
        removeChild() {},
        removeAttribute(name) { delete store.attributes[name]; },
        setAttribute(name, value) {
            store.attributes[name] = String(value);
            if (name === 'class') {
                classes.clear();
                String(value || '').split(/\s+/).filter(Boolean)
                    .forEach((n) => classes.add(n));
            }
        },
        getAttribute(name) {
            if (name === 'href') return store.href || null;
            return store.attributes[name] || null;
        },
        // Not arrows: they need the element as ``this``.
        querySelector(sel) { return findByClass(this, sel)[0] || null; },
        querySelectorAll(sel) { return findByClass(this, sel); },
        closest: () => null, scrollTo() {}, replaceChildren() { this.children = []; },
        textContent: '', innerHTML: '', hidden: false, disabled: false,
        children: [], tagName: 'SPAN', value: '', href: '',
        attributes: {},
    };
    return new Proxy(store, {
        get: (target, prop) => (prop in target ? target[prop] : undefined),
        set: (target, prop, value) => {
            target[prop] = value;
            if (prop === 'className') {
                classes.clear();
                String(value || '').split(/\s+/).filter(Boolean)
                    .forEach((n) => classes.add(n));
            }
            return true;
        },
    });
}

/** Find the descendants whose class list holds the name in the selector. */
function findByClass(root, selector) {
    const name = String(selector || '').replace(/^\./, '');
    const out = [];
    const walk = (node) => {
        (node.children || []).forEach((child) => {
            const classes = child.classList;
            if (classes && classes.contains && classes.contains(name)) out.push(child);
            walk(child);
        });
    };
    walk(root);
    return out;
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

/* The harness runs inside the same context as app.js, so it can call the
   functions that are not on window.  It is async because the loaders are. */
const harness = `
    globalThis.__done = (async () => {
        const results = {calls: []};

        const subjects = [
            {id: 400602, name: 'Sousou no Frieren', name_cn: '葬送的芙莉莲',
             total_episodes: 28, rating_score: 8.5,
             images: {common: 'https://lain.bangumi.lat/c.jpg'}},
            {id: 459283, name: 'Frieren Movie', name_cn: '葬送的芙莉莲 剧场版',
             total_episodes: 11, rating_score: 6.8, images: {}},
        ];
        const episodes = [
            {id: 1227087, ep: 1, sort: 1, type: 0, type_name: '正片',
             name_cn: '冒险结束', label: '冒险结束', duration: '00:26:00',
             airdate: '2023-09-29'},
            {id: 1227088, ep: 2, sort: 2, type: 0, type_name: '正片',
             name_cn: '不见得一定是靠魔法…', label: '不见得一定是靠魔法…',
             duration: '00:26:00', airdate: '2023-09-29'},
        ];

        state.api = {
            search_bangumi: (keyword, limit, type) => {
                results.calls.push('search:' + keyword);
                return Promise.resolve({success: true, items: subjects});
            },
            get_bangumi_subject: (id) => {
                results.calls.push('subject:' + id);
                return Promise.resolve({
                    success: true,
                    subject: Object.assign({}, subjects[0], {
                        favorite: false, collection: {collect: 55720},
                        platform: 'TV', date: '2023-09-29', summary: '简介',
                    }),
                });
            },
            get_bangumi_episodes: (id) => {
                results.calls.push('episodes:' + id);
                return Promise.resolve({success: true, episodes: episodes});
            },
            get_bangumi_resource_links: (id, epId) => {
                results.calls.push('resources:' + id + ':' + (epId || ''));
                return Promise.resolve({success: true, links: [
                    {id: 's1', name: '资源站',
                     detail_url: 'https://x.test/subject/' + id,
                     search_url: 'https://x.test/search?q=' + id},
                ]});
            },
            get_bangumi_favorites: () => {
                results.calls.push('favorites');
                return Promise.resolve({success: true, items: [
                    {subject_id: '400602', name_cn: '葬送的芙莉莲'},
                ]});
            },
            get_bangumi_history: () => {
                results.calls.push('history');
                return Promise.resolve({success: true, items: [
                    {subject_id: '400602', episode_id: '1227087', ep: 1,
                     name_cn: '葬送的芙莉莲'},
                ]});
            },
            toggle_bangumi_favorite: (id) => {
                results.calls.push('toggle:' + id);
                return Promise.resolve({success: true, favorite: true});
            },
            clear_bangumi_history: () => {
                results.calls.push('clear-history');
                return Promise.resolve({success: true, removed: 1});
            },
        };

        // ---- 1: search ---------------------------------------------------
        bangumiState.keyword = '芙莉莲';
        bangumiState.results = [];
        await searchBangumi(true);
        results.searchCount = bangumiState.results.length;
        results.searchNames = bangumiState.results.map((item) => item.name_cn);

        // ---- 2: a failed search shows an error ---------------------------
        state.api.search_bangumi = () => Promise.resolve({success: false});
        bangumiState.results = [];
        bangumiState.error = '';
        await searchBangumi(true);
        results.searchError = bangumiState.error;

        // ---- 3: the local favourites -------------------------------------
        bangumiState.favorites = [];
        await loadBangumiFavorites(true);
        results.favoriteCount = bangumiState.favorites.length;

        // ---- 4: the local history ----------------------------------------
        bangumiState.history = [];
        await loadBangumiHistory(true);
        results.historyCount = bangumiState.history.length;

        // ---- 5: the episode list -----------------------------------------
        await openBangumiSubject(400602);
        results.subjectId = bangumiState.subject ? bangumiState.subject.id : null;
        results.episodeCount = bangumiState.episodes.length;
        results.episodeLabels = bangumiState.episodes.map((item) => item.label);

        // ---- 6: the favourite button flips the state ---------------------
        const before = bangumiState.subject.favorite;
        await toggleBangumiFavorite();
        results.favoriteBefore = before;
        results.favoriteAfter = bangumiState.subject.favorite;

        // ---- 7: the resource links ---------------------------------------
        results.resourceCount = bangumiState.resources.length;
        results.resourceUrl = bangumiState.resources.length
            ? bangumiState.resources[0].search_url : '';
        // The rendered link, not just the data: a link that never reaches
        // the page is worth nothing.
        const resourceList = document.getElementById('bangumi-resources');
        const anchors = resourceList.querySelectorAll
            ? resourceList.querySelectorAll('.bangumi-resource-link')
            : [];
        results.renderedLink = anchors.length ? anchors[0].getAttribute('href') : '';

        // ---- 8: nothing asked for an account -----------------------------
        results.accountCalls = results.calls.filter((name) =>
            name.indexOf('account') >= 0 || name.indexOf('login') >= 0);
        results.allCalls = results.calls.slice();

        // ---- 9: the settings page ----------------------------------------
        fillBangumiSettings({
            api_host: 'api.bangumi.lat',
            web_host: 'bangumi.lat',
            request_interval: 2,
            skip_ssl_verify: true,
            mirrors: [
                {id: 'a', name: '甲', api_hosts: ['api.a.test']},
                {id: 'b', name: '乙', api_hosts: ['api.b.test']},
                {id: 'c', name: '丙', api_hosts: ['api.c.test']},
            ],
            resource_sites: [
                {id: 's1', name: '站1', detail_url: 'https://x.test/{keyword}'},
            ],
        });
        results.mirrorCount = bangumiSettingsState.mirrors.length;
        results.siteCount = bangumiSettingsState.sites.length;
        results.apiHost = document.getElementById('bangumi-api-host').value;
        results.interval = document.getElementById('bangumi-request-interval').value;
        results.skipSsl = document.getElementById('bangumi-skip-ssl').checked;

        // ---- 10: the order can be changed ---------------------------------
        moveBangumiMirror(2, -1);
        results.mirrorOrder = bangumiSettingsState.mirrors.map((item) => item.id);
        moveBangumiMirror(0, -1);      // already at the top: nothing happens
        results.mirrorOrderAfterTop = bangumiSettingsState.mirrors.map((i) => i.id);

        // ---- 11: a mirror can be added and removed -----------------------
        document.getElementById('bangumi-mirror-id').value = 'new';
        document.getElementById('bangumi-mirror-host').value = 'api.new.test';
        addBangumiMirror();
        results.afterAdd = bangumiSettingsState.mirrors.length;
        results.addedHost = bangumiSettingsState.mirrors[bangumiSettingsState.mirrors.length - 1].api_hosts[0];

        // ---- 12: a resource site can be added ----------------------------
        document.getElementById('bangumi-site-name').value = '我的站';
        document.getElementById('bangumi-site-url').value = 'https://mine.test/{keyword}';
        addBangumiSite();
        results.afterSiteAdd = bangumiSettingsState.sites.length;
        results.siteUrl = bangumiSettingsState.sites[bangumiSettingsState.sites.length - 1].detail_url;

        return results;
    })();
`;

vm.createContext(sandbox);
const source = fs.readFileSync(appPath, 'utf8');
vm.runInContext(source + '\n' + harness, sandbox, { filename: 'app.js' });

const results = await sandbox.__done;

check('搜索把关键词送到 bridge', results.allCalls.includes('search:芙莉莲'),
      results.allCalls.join(','));
check('搜索结果有两条', results.searchCount === 2, String(results.searchCount));
check('搜索结果带中文名', (results.searchNames || []).includes('葬送的芙莉莲'),
      (results.searchNames || []).join(' | '));
check('搜索失败会显示错误', !!results.searchError, results.searchError);
check('本地收藏读到了', results.favoriteCount === 1, String(results.favoriteCount));
check('观看历史读到了', results.historyCount === 1, String(results.historyCount));
check('详情拿到了条目', results.subjectId === 400602, String(results.subjectId));
check('选集有两条', results.episodeCount === 2, String(results.episodeCount));
check('选集带标题', (results.episodeLabels || []).includes('冒险结束'),
      (results.episodeLabels || []).join(' | '));
check('收藏状态翻转了', results.favoriteBefore === false
      && results.favoriteAfter === true,
      `${results.favoriteBefore} -> ${results.favoriteAfter}`);
check('资源链接渲染了', results.resourceCount === 1, String(results.resourceCount));
check('资源链接用的是 bridge 给的地址',
      results.resourceUrl === 'https://x.test/search?q=400602', results.resourceUrl);
check('资源链接真的渲染出来了',
      results.renderedLink === 'https://x.test/search?q=400602', results.renderedLink);
check('没有账号请求', results.accountCalls.length === 0,
      results.accountCalls.join(','));

// --- the settings page ---
check('设置读回了镜像', results.mirrorCount === 3, String(results.mirrorCount));
check('设置读回了资源站', results.siteCount === 1, String(results.siteCount));
check('设置读回了 API 主机', results.apiHost === 'api.bangumi.lat', results.apiHost);
check('设置读回了请求间隔', results.interval === '2', results.interval);
check('设置读回了 SSL 开关', results.skipSsl === true, String(results.skipSsl));
check('镜像顺序能调', (results.mirrorOrder || []).join(',') === 'a,c,b',
      (results.mirrorOrder || []).join(','));
check('已经在顶部的不能再上移',
      (results.mirrorOrderAfterTop || []).join(',') === 'a,c,b',
      (results.mirrorOrderAfterTop || []).join(','));
check('能添加镜像', results.afterAdd === 4, String(results.afterAdd));
check('新镜像的主机记下来了', results.addedHost === 'api.new.test', results.addedHost);
check('能添加资源站', results.afterSiteAdd === 2, String(results.afterSiteAdd));
check('资源站地址记下来了',
      results.siteUrl === 'https://mine.test/{keyword}', results.siteUrl);

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed.`);
    process.exit(1);
}
console.log('\nAll Bangumi frontend checks passed.');
