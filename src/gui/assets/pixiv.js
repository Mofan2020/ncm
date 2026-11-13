/* Pixiv 那一块的全部界面逻辑。
 *
 * 单独一个文件是因为 app.js 已经一万五千行了：Pixiv 的页面自成一套
 * （六个标签、一个作品查看面板、一个账号面板），塞进去只会让本来就难找
 * 的东西更难找。这里只通过三个东西和主界面打交道：
 *
 *   state.api.pixiv_*   —— 后端（src/gui/pixiv_bridge.py）
 *   t(key)              —— 文案
 *   PixivUI.enter()     —— app.js 切到本页时叫一声
 *
 * 依赖 app.js 的全局 state / t / showToast，所以这个文件必须在 app.js
 * 之前加载：加载时不碰它们（都在函数体里用），只保证名字先存在。
 */
'use strict';

/** Pixiv 页面自己的状态。跟 app.js 的 state 分开，互不干扰。 */
const pixivState = {
    tab: 'discover',
    loggedIn: false,
    account: {},
    settings: {},
    loginState: 'idle',
    loginPoll: null,
    discover: [],
    feed: { works: [], page: 1, hasMore: false },
    search: { word: '', page: 1, works: [], total: 0, related: [] },
    bookmarks: { tag: '', rest: '', offset: 0, works: [], total: 0 },
    ranking: { mode: 'daily', page: 1, works: [], hasMore: false },
    detail: null,
    detailPage: 0,
    revealed: false,
    following: [],
    busy: false
};

/** 短名字，省得每行都写一遍 document.getElementById。 */
function pixivEl(id) {
    return document.getElementById(id);
}

function pixivApi() {
    return (typeof state !== 'undefined' && state.api) ? state.api : null;
}

/** 顺手把一行状态写到页面上；空字符串就是清掉。 */
function pixivSetState(id, message) {
    const el = pixivEl(id);
    if (el) el.textContent = message || '';
}

function pixivToast(type, message) {
    if (!message) return;
    if (typeof showToast === 'function') showToast(type, message);
}

/**
 * 一张作品的卡片。
 *
 * 缩略图走本地媒体服务器的 /pixiv 代理（i.pximg.net 不带 Referer 会 403，
 * 而 <img> 设不了请求头）。代理没给地址时退回原地址，最多裂一张图。
 *
 * 成人内容在 ``blur`` 模式下加 is-blurred：只糊，不藏——用户扫一眼缩略图
 * 仍然知道那是一张什么图，要不要点开由他自己决定。
 */
function pixivTile(work, options) {
    const opts = options || {};
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'video-card is-tile' + (work.blurred ? ' is-blurred' : '');
    card.setAttribute('aria-label', work.title || '');

    const thumb = document.createElement('div');
    thumb.className = 'vc-thumb';
    const src = work.thumb_proxy || work.thumb || work.small || work.regular;
    if (src) {
        const image = document.createElement('img');
        image.src = src;
        image.alt = '';
        image.loading = 'lazy';
        image.addEventListener('error', () => { image.remove(); });
        thumb.appendChild(image);
    }
    const pages = Number(work.page_count) || 1;
    if (pages > 1) {
        const badge = document.createElement('span');
        badge.className = 'vc-dur';
        badge.textContent = pages + 'P';
        thumb.appendChild(badge);
    }
    if (work.nsfw) {
        const flag = document.createElement('span');
        flag.className = 'pixiv-nsfw-badge';
        flag.textContent = 'R-18';
        thumb.appendChild(flag);
    }
    card.appendChild(thumb);

    const label = document.createElement('span');
    label.className = 'vc-title';
    label.textContent = work.title || '';
    card.appendChild(label);

    if (work.user_name) {
        const sub = document.createElement('span');
        sub.className = 'vc-sub';
        sub.textContent = work.user_name;
        card.appendChild(sub);
    }
    card.addEventListener('click', () => {
        if (opts.onClick) opts.onClick(work);
        else PixivUI.openDetail(work.id);
    });
    return card;
}

/** 把一批作品画进一个网格。 */
/**
 * 空状态：写明原因，再给一个重试。
 *
 * 「没有作品」是最没用的一句话——是没登录、网络不通、还是接口改了，用户
 * 看不出来，我也看不出来。原因照原样写出来。
 */
function pixivSetStateWithRetry(id, message, retry) {
    const el = pixivEl(id);
    if (!el) return;
    el.textContent = '';
    const text = document.createElement('span');
    text.textContent = message || '';
    el.appendChild(text);
    if (typeof retry === 'function') {
        const button = document.createElement('button');
        button.className = 'btn btn-sm btn-secondary';
        button.type = 'button';
        button.textContent = t('common.retry');
        button.addEventListener('click', retry);
        el.appendChild(button);
    }
}

function pixivRenderGrid(gridId, stateId, works, emptyKey) {
    const grid = pixivEl(gridId);
    if (!grid) return;
    grid.innerHTML = '';
    if (!works || !works.length) {
        pixivSetState(stateId, emptyKey ? t(emptyKey) : '');
        return;
    }
    pixivSetState(stateId, '');
    works.forEach((work) => grid.appendChild(pixivTile(work)));
}

const PixivUI = {
    /** 一次性接线。app.js 启动时叫。 */
    init() {
        const tabs = pixivEl('pixiv-tabs');
        if (tabs) {
            tabs.querySelectorAll('[data-pixiv-tab]').forEach((button) => {
                button.addEventListener('click', () => this.setTab(button.dataset.pixivTab));
            });
        }
        const back = pixivEl('btn-pixiv-back');
        if (back) back.addEventListener('click', () => showView('home'));
        const refresh = pixivEl('btn-pixiv-refresh');
        if (refresh) refresh.addEventListener('click', () => this.reload(true));

        const search = pixivEl('btn-pixiv-search');
        if (search) search.addEventListener('click', () => this.runSearch(true));
        const input = pixivEl('pixiv-search-input');
        if (input) {
            input.addEventListener('keydown', (event) => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    this.runSearch(true);
                }
            });
        }
        const moreSearch = pixivEl('btn-pixiv-search-more');
        if (moreSearch) moreSearch.addEventListener('click', () => this.runSearch(false));
        const feedMore = pixivEl('btn-pixiv-feed-more');
        if (feedMore) feedMore.addEventListener('click', () => this.loadFeed(false));

        const bmRefresh = pixivEl('btn-pixiv-bm-refresh');
        if (bmRefresh) bmRefresh.addEventListener('click', () => this.loadBookmarks(true));
        const bmMore = pixivEl('btn-pixiv-bm-more');
        if (bmMore) bmMore.addEventListener('click', () => this.loadBookmarks(false));

        const rankLoad = pixivEl('btn-pixiv-rank-load');
        if (rankLoad) rankLoad.addEventListener('click', () => this.loadRanking(true));
        const rankMore = pixivEl('btn-pixiv-rank-more');
        if (rankMore) rankMore.addEventListener('click', () => this.loadRanking(false));
        // 换档位/换内容/换日期都直接重拉：让人再点一次「刷新」是多余的一步。
        ['pixiv-rank-mode', 'pixiv-rank-content', 'pixiv-rank-date'].forEach((id) => {
            const el = pixivEl(id);
            if (el) el.addEventListener('change', () => this.loadRanking(true));
        });
        const rankDate = pixivEl('pixiv-rank-date');
        if (rankDate) {
            // 今天和未来都没有榜单（pixiv 第二天才出），日期框直接把上限卡住，
            // 免得选完看到一片空白。
            // 东京时间的昨天：epoch 加 9 小时，再按 UTC 取日期字段，不受本机
            // 时区影响（pixiv 按 JST 出榜）。
            const tokyo = new Date(Date.now() + 9 * 3600 * 1000);
            tokyo.setUTCDate(tokyo.getUTCDate() - 1);
            rankDate.max = tokyo.toISOString().slice(0, 10);
        }

        const login = pixivEl('btn-pixiv-login');
        if (login) login.addEventListener('click', () => this.login(false));
        const signup = pixivEl('btn-pixiv-signup');
        if (signup) signup.addEventListener('click', () => this.login(true));
        const logout = pixivEl('btn-pixiv-logout');
        if (logout) logout.addEventListener('click', () => this.logout());
        const exportBtn = pixivEl('btn-pixiv-export');
        if (exportBtn) exportBtn.addEventListener('click', () => this.exportData());
        const cookieSave = pixivEl('btn-pixiv-cookie-save');
        if (cookieSave) cookieSave.addEventListener('click', () => this.saveCookie());

        const close = pixivEl('btn-pixiv-detail-close');
        if (close) close.addEventListener('click', () => this.closeDetail());
        const prev = pixivEl('btn-pixiv-page-prev');
        if (prev) prev.addEventListener('click', () => this.stepPage(-1));
        const next = pixivEl('btn-pixiv-page-next');
        if (next) next.addEventListener('click', () => this.stepPage(1));
        const reveal = pixivEl('btn-pixiv-detail-reveal');
        if (reveal) reveal.addEventListener('click', () => this.reveal());
        const bookmark = pixivEl('btn-pixiv-detail-bookmark');
        if (bookmark) bookmark.addEventListener('click', () => this.toggleBookmark());
        const like = pixivEl('btn-pixiv-detail-like');
        if (like) like.addEventListener('click', () => this.like());
        const download = pixivEl('btn-pixiv-detail-download');
        if (download) download.addEventListener('click', () => this.download());

        // 设置项：改完立刻存，不用「保存」按钮。
        const nsfw = pixivEl('pixiv-nsfw-mode');
        if (nsfw) nsfw.addEventListener('change', () => this.saveSettings({ nsfw_mode: nsfw.value }));
        const r18 = pixivEl('pixiv-include-r18');
        if (r18) r18.addEventListener('change', () => this.saveSettings({ include_r18: r18.checked }));
        const hideAi = pixivEl('pixiv-hide-ai');
        if (hideAi) hideAi.addEventListener('change', () => this.saveSettings({ hide_ai: hideAi.checked }));
        const quality = pixivEl('pixiv-quality');
        if (quality) quality.addEventListener('change', () => this.saveSettings({ quality: quality.value }));
        const bmPrivate = pixivEl('pixiv-bm-private');
        if (bmPrivate) bmPrivate.addEventListener('change', () => this.saveSettings({ bookmark_private: bmPrivate.checked }));
        const template = pixivEl('pixiv-template');
        if (template) {
            template.addEventListener('change', () => this.saveSettings({ template: template.value }));
        }
        const pageSize = pixivEl('pixiv-page-size');
        if (pageSize) {
            pageSize.addEventListener('change', () => {
                const value = Math.max(12, Math.min(96, Number(pageSize.value) || 48));
                pageSize.value = String(value);
                this.saveSettings({ page_size: value });
            });
        }
        const chooseDir = pixivEl('btn-pixiv-choose-dir');
        if (chooseDir) {
            chooseDir.addEventListener('click', async () => {
                const api = pixivApi();
                if (!api) return;
                try {
                    const picked = await api.choose_directory();
                    if (!picked) return;
                    const box = pixivEl('pixiv-download-dir');
                    if (box) box.value = picked;
                    this.saveSettings({ download_dir: picked });
                } catch (err) {
                    pixivToast('error', String(err && err.message ? err.message : err));
                }
            });
        }

        document.addEventListener('keydown', (event) => {
            const panel = pixivEl('pixiv-detail');
            if (!panel || panel.hidden) return;
            if (event.key === 'Escape') this.closeDetail();
            else if (event.key === 'ArrowLeft') this.stepPage(-1);
            else if (event.key === 'ArrowRight') this.stepPage(1);
        });

        this.refreshState();
    },

    /** app.js 切到 Pixiv 页时叫：把当前标签刷新一遍。 */
    enter() {
        this.refreshState();
        const tab = pixivState.tab || 'discover';
        if (tab === 'discover' && !pixivState.discover.length) this.loadDiscover();
        if (tab === 'feed') this.loadFeed(true);
        if (tab === 'bookmarks') this.loadBookmarks(true);
        if (tab === 'ranking' && !pixivState.ranking.works.length) this.loadRanking(true);
        if (tab === 'account') this.loadFollowing();
        const input = pixivEl('pixiv-search-input');
        if (tab === 'search' && input && !pixivState.search.works.length) input.focus();
    },

    /** 顶栏刷新按钮：不管当前在哪一页，都重新拉一次。 */
    reload(force) {
        this.refreshState();
        const tab = pixivState.tab || 'discover';
        if (tab === 'discover') this.loadDiscover();
        else if (tab === 'feed') this.loadFeed(true);
        else if (tab === 'search' && pixivState.search.word) this.runSearch(true);
        else if (tab === 'bookmarks') this.loadBookmarks(true);
        else if (tab === 'ranking') this.loadRanking(true);
        else if (tab === 'account') this.loadFollowing();
        return force;
    },

    setTab(tab) {
        pixivState.tab = tab;
        document.querySelectorAll('#pixiv-tabs [data-pixiv-tab]').forEach((button) => {
            button.classList.toggle('is-active', button.dataset.pixivTab === tab);
        });
        ['discover', 'feed', 'search', 'bookmarks', 'ranking', 'account'].forEach((name) => {
            const seg = pixivEl('pixiv-seg-' + name);
            if (seg) seg.hidden = name !== tab;
        });
        if (tab === 'discover' && !pixivState.discover.length) this.loadDiscover();
        else if (tab === 'feed' && !pixivState.feed.works.length) this.loadFeed(true);
        else if (tab === 'bookmarks') this.loadBookmarks(true);
        else if (tab === 'ranking' && !pixivState.ranking.works.length) this.loadRanking(true);
        else if (tab === 'account') {
            this.refreshState();
            this.loadFollowing();
        } else if (tab === 'search') {
            const input = pixivEl('pixiv-search-input');
            if (input) input.focus();
        }
    },

    /* ------------------------------------------------------------ 后端状态 */

    async refreshState() {
        const api = pixivApi();
        if (!api) return;
        let info;
        try {
            info = await api.pixiv_state();
        } catch (err) {
            pixivSetState('pixiv-login-hint', t('pixiv.backend_missing'));
            return;
        }
        if (!info) return;
        pixivState.loggedIn = !!info.logged_in;
        pixivState.account = info.account || {};
        pixivState.settings = info.settings || {};
        this.renderAccount(info);
        this.applySettingsControls();
        const count = pixivEl('home-count-pixiv');
        if (count) count.textContent = info.logged_in ? (pixivState.account.name || '') : '';
        pixivSetState('pixiv-login-hint',
            info.expired ? t('pixiv.session_expired') : t('pixiv.login_hint'));
        // 账号中心那张卡不在这个文件里，登好 / 退出之后得让它知道。
        document.dispatchEvent(new CustomEvent('pixiv-state-changed'));
    },

    renderAccount(info) {
        const account = pixivState.account;
        const loggedIn = !!info.logged_in;
        const avatar = pixivEl('pixiv-account-avatar');
        if (avatar) {
            const src = account.avatar_proxy || account.avatar || '';
            avatar.hidden = !(loggedIn && src);
            if (src) avatar.src = src;
        }
        const name = pixivEl('pixiv-account-name');
        if (name) {
            name.textContent = loggedIn
                ? (account.name || t('pixiv.account_unknown'))
                : t('pixiv.not_logged_in');
        }
        const sub = pixivEl('pixiv-account-sub');
        if (sub) {
            const parts = [];
            if (loggedIn && account.id) parts.push('ID ' + account.id);
            if (loggedIn && account.premium) parts.push(t('pixiv.premium'));
            if (loggedIn && account.following) {
                parts.push(t('pixiv.following_count', { count: account.following }));
            }
            sub.textContent = parts.join(' · ');
        }
        ['btn-pixiv-login', 'btn-pixiv-signup', 'pixiv-cookie-input', 'btn-pixiv-cookie-save']
            .forEach((id) => {
                const el = pixivEl(id);
                if (el) el.hidden = loggedIn;
            });
        ['btn-pixiv-export', 'btn-pixiv-logout'].forEach((id) => {
            const el = pixivEl(id);
            if (el) el.hidden = !loggedIn;
        });
    },

    applySettingsControls() {
        const settings = pixivState.settings || {};
        const nsfw = pixivEl('pixiv-nsfw-mode');
        if (nsfw) nsfw.value = settings.nsfw_mode || 'hide';
        const r18 = pixivEl('pixiv-include-r18');
        if (r18) r18.checked = !!settings.include_r18;
        const hideAi = pixivEl('pixiv-hide-ai');
        if (hideAi) hideAi.checked = !!settings.hide_ai;
        const quality = pixivEl('pixiv-quality');
        if (quality) quality.value = settings.quality || 'original';
        const bmPrivate = pixivEl('pixiv-bm-private');
        if (bmPrivate) bmPrivate.checked = !!settings.bookmark_private;
        const template = pixivEl('pixiv-template');
        if (template && document.activeElement !== template) {
            template.value = settings.template || '';
        }
        const pageSize = pixivEl('pixiv-page-size');
        if (pageSize && document.activeElement !== pageSize) {
            pageSize.value = String(settings.page_size || 48);
        }
        const dir = pixivEl('pixiv-download-dir');
        if (dir) dir.value = settings.download_dir || '';
    },

    async saveSettings(patch) {
        const api = pixivApi();
        if (!api) return;
        try {
            const info = await api.pixiv_update_settings(patch);
            if (info) {
                pixivState.settings = info.settings || pixivState.settings;
                this.applySettingsControls();
            }
        } catch (err) {
            pixivToast('error', String(err && err.message ? err.message : err));
            return;
        }
        // 过滤规则改了，屏幕上那些列表立刻重画：不然用户要等下一次请求
        // 才看得见「隐藏」生效，还以为开关没作用。
        const tab = pixivState.tab;
        if (tab === 'discover') this.loadDiscover();
        else if (tab === 'search' && pixivState.search.word) this.runSearch(true);
        else if (tab === 'bookmarks') this.loadBookmarks(true);
        else if (tab === 'ranking') this.loadRanking(true);
        else if (tab === 'feed') this.loadFeed(true);
    },

    /* ---------------------------------------------------------------- 列表 */

    async loadDiscover() {
        const api = pixivApi();
        if (!api) return;
        if (typeof api.pixiv_home !== 'function') {
            pixivSetState('pixiv-discover-state', t('pixiv.backend_missing'));
            return;
        }
        pixivSetState('pixiv-discover-state', t('common.loading'));
        try {
            const home = await api.pixiv_home();
            const works = (home && home.discover) || [];
            pixivState.discover = works;
            if (home && home.feed && home.feed.length) pixivState.feed.works = home.feed;
            const element = pixivEl('pixiv-discover-grid');
            if (element) {
                element.innerHTML = '';
                works.forEach((work) => element.appendChild(pixivTile(work)));
            }
            // 有作品也可以带话（比如取不到推荐流、兜底成排行榜），照样写出来。
            const note = (home && home.discover_error) || '';
            if (!works.length) {
                pixivSetStateWithRetry('pixiv-discover-state',
                    note || t('pixiv.discover_empty'), () => this.loadDiscover());
            } else {
                pixivSetState('pixiv-discover-state', note);
            }
        } catch (err) {
            pixivSetStateWithRetry('pixiv-discover-state',
                String(err && err.message ? err.message : err), () => this.loadDiscover());
        }
    },

    async loadFeed(reset) {
        const api = pixivApi();
        if (!api) return;
        if (!pixivState.loggedIn) {
            pixivRenderGrid('pixiv-feed-grid', 'pixiv-feed-state', [], 'pixiv.feed_needs_login');
            const more = pixivEl('btn-pixiv-feed-more');
            if (more) more.hidden = true;
            return;
        }
        const page = reset ? 1 : pixivState.feed.page + 1;
        pixivSetState('pixiv-feed-state', t('common.loading'));
        try {
            const result = await api.pixiv_feed(page);
            const works = (result && result.works) || [];
            pixivState.feed.page = page;
            pixivState.feed.hasMore = !!(result && result.has_more);
            pixivState.feed.works = reset ? works : pixivState.feed.works.concat(works);
            pixivRenderGrid('pixiv-feed-grid', 'pixiv-feed-state',
                pixivState.feed.works, 'pixiv.feed_empty');
            const more = pixivEl('btn-pixiv-feed-more');
            if (more) more.hidden = !pixivState.feed.hasMore;
            if (result && result.error) pixivSetState('pixiv-feed-state', result.error);
        } catch (err) {
            pixivSetState('pixiv-feed-state', String(err && err.message ? err.message : err));
        }
    },

    async runSearch(reset) {
        const api = pixivApi();
        if (!api) return;
        const input = pixivEl('pixiv-search-input');
        const word = (input ? input.value : '').trim() || pixivState.search.word;
        if (!word) {
            pixivSetState('pixiv-search-state', t('pixiv.search_placeholder'));
            return;
        }
        const orderEl = pixivEl('pixiv-search-order');
        const order = orderEl ? orderEl.value : 'date_d';
        const page = reset ? 1 : (pixivState.search.page + 1);
        pixivState.search.word = word;
        pixivSetState('pixiv-search-state', t('common.loading'));
        try {
            const result = await api.pixiv_search(word, page, order);
            const works = (result && result.works) || [];
            const total = Number(result && result.total) || 0;
            pixivState.search.page = page;
            pixivState.search.total = total;
            pixivState.search.related = (result && result.related_tags) || [];
            pixivState.search.works = reset ? works : pixivState.search.works.concat(works);
            const grid = pixivEl('pixiv-search-grid');
            if (grid) {
                grid.innerHTML = '';
                pixivState.search.works.forEach((work) => grid.appendChild(pixivTile(work)));
            }
            const hint = pixivEl('pixiv-search-hint');
            if (hint) {
                hint.textContent = total
                    ? t('pixiv.search_total', { total: total, count: pixivState.search.works.length })
                    : '';
            }
            pixivSetState('pixiv-search-state', pixivState.search.works.length
                ? ((result && result.error) || '') : t('pixiv.search_empty'));
            const more = pixivEl('btn-pixiv-search-more');
            if (more) more.hidden = pixivState.search.works.length >= total;
        } catch (err) {
            pixivSetState('pixiv-search-state', String(err && err.message ? err.message : err));
        }
    },

    async loadBookmarks(reset) {
        const api = pixivApi();
        if (!api) return;
        if (!pixivState.loggedIn) {
            pixivRenderGrid('pixiv-bm-grid', 'pixiv-bm-state', [], 'pixiv.bookmarks_need_login');
            const more = pixivEl('btn-pixiv-bm-more');
            if (more) more.hidden = true;
            return;
        }
        const tagEl = pixivEl('pixiv-bm-tag');
        const restEl = pixivEl('pixiv-bm-rest');
        const offset = reset ? 0 : pixivState.bookmarks.works.length;
        pixivState.bookmarks.tag = tagEl ? tagEl.value.trim() : '';
        pixivState.bookmarks.rest = restEl ? restEl.value : '';
        pixivSetState('pixiv-bm-state', t('common.loading'));
        try {
            const result = await api.pixiv_bookmarks(pixivState.bookmarks.tag, offset,
                pixivState.bookmarks.rest);
            const works = (result && result.works) || [];
            const total = Number(result && result.total) || 0;
            pixivState.bookmarks.total = total;
            pixivState.bookmarks.works = reset ? works : pixivState.bookmarks.works.concat(works);
            pixivRenderGrid('pixiv-bm-grid', 'pixiv-bm-state',
                pixivState.bookmarks.works, 'pixiv.bookmarks_empty');
            const hint = pixivEl('pixiv-bm-hint');
            if (hint) {
                hint.textContent = total ? t('pixiv.bookmarks_total', { total: total }) : '';
            }
            const more = pixivEl('btn-pixiv-bm-more');
            if (more) more.hidden = pixivState.bookmarks.works.length >= total;
            if (result && result.error) pixivSetState('pixiv-bm-state', result.error);
        } catch (err) {
            pixivSetState('pixiv-bm-state', String(err && err.message ? err.message : err));
        }
    },

    async loadRanking(reset) {
        const api = pixivApi();
        if (!api) return;
        const modeEl = pixivEl('pixiv-rank-mode');
        const mode = modeEl ? modeEl.value : 'daily';
        const contentEl = pixivEl('pixiv-rank-content');
        const content = contentEl ? contentEl.value : 'illust';
        const dateEl = pixivEl('pixiv-rank-date');
        // 日期留空 = 「最近一期」，让 pixiv 自己说他出到哪天了。选了今天或以
        // 后的日期后端会夹回昨天，这里不用重复判断。
        const date = dateEl ? String(dateEl.value || '').replace(/-/g, '') : '';
        const page = reset ? 1 : (pixivState.ranking.page + 1);
        pixivSetState('pixiv-rank-state', t('common.loading'));
        try {
            const result = await api.pixiv_ranking(mode, page, content, date);
            const works = (result && result.works) || [];
            pixivState.ranking.mode = mode;
            pixivState.ranking.page = page;
            pixivState.ranking.hasMore = !!(result && result.has_more);
            pixivState.ranking.works = reset ? works : pixivState.ranking.works.concat(works);
            pixivRenderGrid('pixiv-rank-grid', 'pixiv-rank-state',
                pixivState.ranking.works, 'pixiv.ranking_empty');
            const rankError = (result && result.error) || '';
            if (!pixivState.ranking.works.length && rankError) {
                pixivSetStateWithRetry('pixiv-rank-state', rankError,
                    () => this.loadRanking(true));
            }
            const more = pixivEl('btn-pixiv-rank-more');
            if (more) more.hidden = !pixivState.ranking.hasMore;
            // 「那天还没有榜单，这是最近一期」这类话要说给用户听，否则他
            // 选了日期看到日期不对，只会以为是程序出错。
            const note = (result && result.note) || '';
            if (note && pixivState.ranking.works.length) {
                pixivSetState('pixiv-rank-state', note);
            }
        } catch (err) {
            pixivSetState('pixiv-rank-state', String(err && err.message ? err.message : err));
        }
    },

    async loadFollowing() {
        const api = pixivApi();
        if (!api) return;
        const list = pixivEl('pixiv-following-list');
        if (!list) return;
        if (!pixivState.loggedIn) {
            list.innerHTML = '';
            pixivSetState('pixiv-following-state', t('pixiv.feed_needs_login'));
            return;
        }
        try {
            const result = await api.pixiv_following(0);
            pixivState.following = (result && result.users) || [];
            list.innerHTML = '';
            if (!pixivState.following.length) {
                pixivSetState('pixiv-following-state', t('pixiv.following_empty'));
                return;
            }
            pixivSetState('pixiv-following-state', '');
            pixivState.following.forEach((user) => {
                const row = document.createElement('button');
                row.type = 'button';
                row.className = 'pixiv-follow-row';
                const name = document.createElement('span');
                name.className = 'pixiv-follow-name';
                name.textContent = user.name || user.id;
                row.appendChild(name);
                const action = document.createElement('span');
                action.className = 'pixiv-follow-action';
                action.textContent = user.followed ? t('pixiv.unfollow') : t('pixiv.follow');
                row.appendChild(action);
                row.addEventListener('click', (event) => {
                    event.stopPropagation();
                    this.followUser(user.id, !user.followed);
                });
                list.appendChild(row);
            });
        } catch (err) {
            pixivSetState('pixiv-following-state', String(err && err.message ? err.message : err));
        }
    },

    /* ------------------------------------------------------------ 详情面板 */

    async openDetail(pid) {
        const api = pixivApi();
        if (!api || !pid) return;
        const panel = pixivEl('pixiv-detail');
        if (!panel) return;
        panel.hidden = false;
        panel.classList.add('is-open');
        pixivState.detailPage = 0;
        pixivState.revealed = false;
        pixivState.detail = { id: pid };
        pixivSetState('pixiv-detail-title', t('common.loading'));
        pixivEl('pixiv-detail-curtain').hidden = true;
        const meta = pixivEl('pixiv-detail-meta');
        if (meta) meta.textContent = '';
        ['pixiv-detail-tags', 'pixiv-detail-desc', 'pixiv-detail-stats',
         'pixiv-artist-works', 'pixiv-related-grid', 'pixiv-related-state'].forEach((id) => {
            const el = pixivEl(id);
            if (el) el.innerHTML = '';
        });
        try {
            const work = await api.pixiv_illust(pid);
            if (!work || !work.id) {
                pixivSetState('pixiv-detail-title', (work && work.error) || t('pixiv.detail_failed'));
                return;
            }
            pixivState.detail = work;
            pixivState.detailPage = 0;
            this.renderDetail(work);
            // 相关作品单独请求：它慢一点不该拖住整个详情页。
            this.loadRelated(work.id);
        } catch (err) {
            pixivSetState('pixiv-detail-title', String(err && err.message ? err.message : err));
        }
    },

    renderDetail(work) {
        const title = pixivEl('pixiv-detail-title');
        if (title) title.textContent = work.title || '';
        // 标题下面那一行只放「这是谁的、什么时候的、多大」：数字另有一块，
        // 混在一行里挤到换行，看起来就像没排版。
        const meta = pixivEl('pixiv-detail-meta');
        if (meta) {
            const pages = (work.pages || []).length || work.page_count || 1;
            const parts = [];
            if (work.user_name) parts.push(work.user_name);
            parts.push(work.type === 2 ? t('pixiv.type_ugoira')
                : (pages > 1 ? t('pixiv.type_manga') : t('pixiv.type_illust')));
            if (work.create_date) parts.push(String(work.create_date).slice(0, 10));
            if (work.width && work.height) parts.push(`${work.width} × ${work.height}`);
            meta.textContent = parts.join(' · ');
        }
        this.renderStats(work);
        const tags = pixivEl('pixiv-detail-tags');
        if (tags) {
            tags.innerHTML = '';
            (work.tags || []).forEach((tag) => {
                const chip = document.createElement('span');
                chip.className = 'pixiv-tag';
                chip.textContent = tag;
                tags.appendChild(chip);
            });
        }
        const desc = pixivEl('pixiv-detail-desc');
        if (desc) {
            // 描述是 HTML，只取文本：把外面来的标签挂进 DOM 等于让它决定
            // 我们页面的结构。
            desc.textContent = String(work.description || '').replace(/<[^>]*>/g, ' ').trim();
        }
        const artist = pixivEl('pixiv-detail-artist');
        if (artist) {
            artist.textContent = work.user_name
                ? t('pixiv.more_from', { name: work.user_name }) : '';
            artist.dataset.userId = work.user_id || '';
        }
        const grid = pixivEl('pixiv-artist-works');
        if (grid) {
            grid.innerHTML = '';
            (work.artist_works || []).forEach((other) => grid.appendChild(pixivTile(other)));
        }
        const curtain = pixivEl('pixiv-detail-curtain');
        if (curtain) curtain.hidden = !work.nsfw || pixivState.revealed;
        this.renderDetailPage();
    },

    /// 相关作品。pixiv 的 `/related` 是 404，能用的是 `/recommend/init`，
    /// 游客也拿得到（实测 4 条），登录后更多。
    async loadRelated(pid) {
        const api = pixivApi();
        const grid = pixivEl('pixiv-related-grid');
        const state = pixivEl('pixiv-related-state');
        if (!api || !grid || !state) return;
        const wanted = String(pid || '');
        try {
            const result = await api.pixiv_related(wanted);
            // 请求期间用户可能已经翻到别的作品了：那样就别把旧的结果画上去。
            if (!pixivState.detail || String(pixivState.detail.id) !== wanted) return;
            const works = (result && result.works) || [];
            grid.innerHTML = '';
            works.forEach((work) => grid.appendChild(pixivTile(work)));
            state.textContent = works.length ? '' : ((result && result.error) || '');
        } catch (err) {
            if (!pixivState.detail || String(pixivState.detail.id) !== wanted) return;
            state.textContent = String(err && err.message ? err.message : err);
        }
    },

    renderStats(work) {
        const box = pixivEl('pixiv-detail-stats');
        if (!box) return;
        box.innerHTML = '';
        const pages = (work.pages || []).length || work.page_count || 1;
        const rows = [
            ['pixiv.stat_bookmarks', work.bookmark_count],
            ['pixiv.stat_likes', work.like_count],
            ['pixiv.stat_views', work.view_count],
            ['pixiv.stat_pages', pages]
        ];
        rows.forEach(([key, value]) => {
            const cell = document.createElement('div');
            const term = document.createElement('dt');
            term.textContent = t(key);
            const num = document.createElement('dd');
            const count = Number(value) || 0;
            num.textContent = count.toLocaleString();
            cell.appendChild(term);
            cell.appendChild(num);
            box.appendChild(cell);
        });
    },

    renderDetailPage() {
        const work = pixivState.detail || {};
        const pages = work.pages || [];
        const index = Math.min(pixivState.detailPage, Math.max(0, pages.length - 1));
        pixivState.detailPage = index;
        const image = pixivEl('pixiv-detail-image');
        if (image) {
            const page = pages[index] || {};
            const src = page.proxy || page.regular || page.original || work.original_proxy
                || work.regular || work.thumb_proxy || work.thumb || '';
            image.src = src || '';
            image.hidden = !src;
        }
        const pager = pixivEl('pixiv-detail-pager');
        if (pager) {
            pager.textContent = pages.length > 1
                ? t('pixiv.page_of', { index: index + 1, total: pages.length }) : '';
        }
        const prev = pixivEl('btn-pixiv-page-prev');
        const next = pixivEl('btn-pixiv-page-next');
        const many = pages.length > 1;
        if (prev) prev.hidden = !many;
        if (next) next.hidden = !many;
        const bookmark = pixivEl('btn-pixiv-detail-bookmark');
        if (bookmark) {
            bookmark.classList.toggle('is-active', !!work.bookmarked);
            bookmark.title = work.bookmarked ? t('pixiv.unbookmark') : t('pixiv.bookmark');
        }
    },

    stepPage(delta) {
        const pages = (pixivState.detail && pixivState.detail.pages) || [];
        if (pages.length < 2) return;
        pixivState.detailPage = Math.max(0,
            Math.min(pages.length - 1, pixivState.detailPage + delta));
        this.renderDetailPage();
    },

    reveal() {
        pixivState.revealed = true;
        const curtain = pixivEl('pixiv-detail-curtain');
        if (curtain) curtain.hidden = true;
    },

    closeDetail() {
        const panel = pixivEl('pixiv-detail');
        if (panel) {
            panel.hidden = true;
            panel.classList.remove('is-open');
        }
        pixivState.detail = null;
        const image = pixivEl('pixiv-detail-image');
        if (image) image.src = '';
    },

    /* ---------------------------------------------------------------- 写 */

    async toggleBookmark() {
        const api = pixivApi();
        const work = pixivState.detail;
        if (!api || !work || !work.id) return;
        if (!pixivState.loggedIn) {
            pixivToast('warning', t('pixiv.bookmarks_need_login'));
            this.setTab('account');
            return;
        }
        const add = !work.bookmarked;
        try {
            const result = await api.pixiv_bookmark(work.id, add, null);
            if (!result || !result.ok) {
                pixivToast('error', (result && result.message) || t('pixiv.action_failed'));
                return;
            }
            work.bookmarked = add;
            this.renderDetailPage();
            pixivToast('success', add ? t('pixiv.bookmark_done') : t('pixiv.unbookmark_done'));
        } catch (err) {
            pixivToast('error', String(err && err.message ? err.message : err));
        }
    },

    async like() {
        const api = pixivApi();
        const work = pixivState.detail;
        if (!api || !work || !work.id) return;
        if (!pixivState.loggedIn) {
            pixivToast('warning', t('pixiv.bookmarks_need_login'));
            this.setTab('account');
            return;
        }
        // 点赞在 pixiv 那边**不能撤销**，所以先问一句。这不是「多一步」，
        // 是唯一一次能让用户改主意的机会。
        if (typeof window.confirm === 'function'
            && !window.confirm(t('pixiv.like_confirm'))) {
            return;
        }
        try {
            const result = await api.pixiv_like(work.id);
            if (!result || !result.ok) {
                pixivToast('error', (result && result.message) || t('pixiv.action_failed'));
                return;
            }
            pixivToast('success', result.already
                ? t('pixiv.like_already') : t('pixiv.like_done'));
        } catch (err) {
            pixivToast('error', String(err && err.message ? err.message : err));
        }
    },

    async download() {
        const api = pixivApi();
        const work = pixivState.detail;
        if (!api || !work || !work.id) return;
        const call = api.pixiv_download;
        if (typeof call !== 'function') {
            pixivToast('warning', t('pixiv.download_unavailable'));
            return;
        }
        pixivToast('info', t('pixiv.download_started'));
        try {
            const result = await call.call(api, work.id);
            if (result && result.ok) {
                const files = (result.files || []).length;
                pixivToast('success', t('pixiv.download_done', { count: files }));
            } else {
                pixivToast('error', (result && (result.error || result.message))
                    || t('pixiv.action_failed'));
            }
        } catch (err) {
            pixivToast('error', String(err && err.message ? err.message : err));
        }
    },

    async followUser(uid, follow) {
        const api = pixivApi();
        if (!api || !uid) return;
        try {
            const result = await api.pixiv_follow(uid, follow);
            if (!result || !result.ok) {
                pixivToast('error', (result && result.message) || t('pixiv.action_failed'));
                return;
            }
            this.loadFollowing();
        } catch (err) {
            pixivToast('error', String(err && err.message ? err.message : err));
        }
    },

    /* ------------------------------------------------------------ 账号 */

    async login(signup) {
        const api = pixivApi();
        if (!api) return;
        this.setTab('account');
        pixivSetState('pixiv-login-hint', t('pixiv.login_waiting'));
        try {
            const result = await api.pixiv_login_open(!!signup);
            if (result && result.success === false) {
                pixivSetState('pixiv-login-hint', result.message || t('pixiv.login_failed'));
                return;
            }
        } catch (err) {
            pixivSetState('pixiv-login-hint', String(err && err.message ? err.message : err));
            return;
        }
        this.pollLogin(true);
    },

    /** 登录窗口开着的时候每秒问一次后端「登好了没有」。 */
    pollLogin(start) {
        if (pixivState.loginPoll) {
            clearInterval(pixivState.loginPoll);
            pixivState.loginPoll = null;
        }
        if (!start) return;
        const tick = async () => {
            const api = pixivApi();
            if (!api) return;
            let info;
            try {
                info = await api.pixiv_login_status();
            } catch (err) {
                return;
            }
            if (!info) return;
            pixivState.loginState = info.state;
            if (info.state === 'ok') {
                this.pollLogin(false);
                pixivToast('success', t('pixiv.login_ok'));
                this.refreshState();
            } else if (info.state === 'failed' || info.state === 'cancelled') {
                this.pollLogin(false);
                pixivSetState('pixiv-login-hint',
                    info.message || t('pixiv.login_cancelled'));
            }
        };
        pixivState.loginPoll = setInterval(tick, 1200);
    },

    async saveCookie() {
        const api = pixivApi();
        const input = pixivEl('pixiv-cookie-input');
        if (!api || !input) return;
        const cookie = input.value.trim();
        if (!cookie) return;
        try {
            const result = await api.pixiv_login_cookie(cookie);
            if (result && result.success) {
                input.value = '';
                pixivToast('success', t('pixiv.login_ok'));
                this.refreshState();
            } else {
                pixivSetState('pixiv-login-hint',
                    (result && result.message) || t('pixiv.login_failed'));
            }
        } catch (err) {
            pixivSetState('pixiv-login-hint', String(err && err.message ? err.message : err));
        }
    },

    async logout() {
        const api = pixivApi();
        if (!api) return;
        try {
            await api.pixiv_logout();
        } catch (err) {
            /* 退出登录失败也只能继续把界面切过去 */
        }
        pixivState.discover = [];
        pixivState.feed = { works: [], page: 1, hasMore: false };
        pixivState.bookmarks = { tag: '', rest: '', offset: 0, works: [], total: 0 };
        pixivState.following = [];
        await this.refreshState();
        pixivRenderGrid('pixiv-bm-grid', 'pixiv-bm-state', [], 'pixiv.bookmarks_need_login');
        pixivRenderGrid('pixiv-feed-grid', 'pixiv-feed-state', [], 'pixiv.feed_needs_login');
        const list = pixivEl('pixiv-following-list');
        if (list) list.innerHTML = '';
    },

    async exportData() {
        const api = pixivApi();
        if (!api) return;
        pixivToast('info', t('pixiv.export_started'));
        try {
            const result = await api.pixiv_export_data();
            if (result && result.success) {
                const counts = result.counts || {};
                pixivToast('success', t('pixiv.export_done', {
                    bookmarks: counts.bookmarks || 0,
                    following: counts.following || 0
                }));
            } else if (result && result.cancelled) {
                /* 用户自己取消了，不用说话 */
            } else {
                pixivToast('error', (result && result.message) || t('pixiv.export_failed'));
            }
        } catch (err) {
            pixivToast('error', String(err && err.message ? err.message : err));
        }
    }
};

window.PixivUI = PixivUI;
