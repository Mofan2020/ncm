#!/usr/bin/env node
/**
 * Feature check for the phone front end (android/web).
 *
 * Why this is not a pytest test: the behaviour lives in the browser, so
 * the only honest way to test it is to drive the page and look at what
 * happens.  The script loads the real android/web/index.html in Chromium
 * with a stubbed bridge and checks the things that were broken:
 *
 *   1. the download list shows an empty state instead of spinning forever;
 *   2. the followed artists are rows with a small avatar next to the name;
 *   3. tapping the cover shows the lyrics, tapping them goes back;
 *   4. the track details list the artist and the artist opens a page;
 *   5. the settings page exists and shows the cache size;
 *   6. the cache size is readable in a human unit.
 *
 * Run:  node scripts/check-android-features.mjs
 *
 * Needs Playwright:  npm i -g playwright && npx playwright install chromium
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

let chromium;
try {
    ({ chromium } = await import('playwright'));
} catch (err) {
    const require = createRequire(import.meta.url);
    const roots = [
        process.env.PLAYWRIGHT_ROOT,
        '/opt/homebrew/lib/node_modules',
        '/usr/local/lib/node_modules',
        path.join(os.homedir(), '.npm-global/lib/node_modules'),
    ].filter(Boolean);
    for (const dir of roots) {
        const candidate = path.join(dir, 'playwright');
        if (!fs.existsSync(candidate)) continue;
        try {
            ({ chromium } = require(candidate));
            break;
        } catch (err) { /* try the next one */ }
    }
}
if (!chromium) {
    console.error('playwright is not installed: npm i -g playwright');
    process.exit(2);
}

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const web = path.join(root, 'android', 'web');

const VIEWPORT = { width: 393, height: 852 };

/* The bridge answers with the same shapes the real one does, so the page
   takes the same paths it takes on the phone. */
function stubBridge() {
    const rows = Array.from({ length: 40 }, (_, i) => ({
        id: String(9000 + i),
        name: '测试歌曲 ' + (i + 1),
        artists: '测试歌手',
        artist_list: [{ id: '77', name: '测试歌手' }],
        album: '测试专辑',
        duration: 200 + i,
        cover_url: '',
        source: 'online',
    }));
    const artists = Array.from({ length: 12 }, (_, i) => ({
        id: String(500 + i),
        name: '歌手 ' + (i + 1),
        cover_url: '',
    }));
    const answer = (value) => async () => value;
    window.pywebview = {
        api: {
            get_settings: answer({
                success: true,
                auth: { remember_login: true },
                playback: { show_translation: true, prefer_online: true,
                    play_mode: 'sequence', online_quality: 'standard' },
                ui: { theme: 'dark', language: 'zh' },
            }),
            get_translations: answer({}),
            get_login_status: answer({
                success: true, user: { nickname: '测试', user_id: 1 },
            }),
            get_home_fm: answer({ success: true, data: rows.slice(0, 10) }),
            get_new_songs: answer({ success: true, data: rows.slice(10, 20) }),
            get_recommend_playlists: answer({ success: true, data: [] }),
            get_top_lists: answer({ success: true, data: [] }),
            get_mvs: answer({ success: true, data: [] }),
            get_search_suggest: answer({ success: true, data: [] }),
            get_user_playlists: answer({ success: true, data: [] }),
            get_liked_playlists: answer({ success: true, data: [] }),
            get_local_music: answer({ success: true, data: [] }),
            get_download_stats: answer({ success: true, finished: 0, total: 0 }),
            get_download_queue: answer({ success: true, data: [] }),
            get_downloaded: answer({ success: true, data: [] }),
            get_local_tracks: answer({ success: true, tracks: [] }),
            get_followed_artists: answer({ success: true, artists, count: artists.length }),
            get_track_lyrics: answer({
                success: true, has_lyrics: true, source: 'online',
                lines: [
                    { time: 0, text: '第一句歌词', translation: '' },
                    { time: 5000, text: '第二句歌词', translation: 'second line' },
                    { time: 10000, text: '第三句歌词', translation: '' },
                ],
            }),
            get_artist_detail: answer({
                success: true,
                artist: { id: '77', name: '测试歌手', cover_url: '' },
                hot_songs: rows.slice(0, 5),
                albums: [], followed: false, fans_count: 0, follow_days: 0,
            }),
            get_cache_stats: answer({
                success: true, available: true, root: '/tmp',
                audio: { count: 3, bytes: 5 * 1024 * 1024, budget: 0 },
                cover: { count: 2, bytes: 200 * 1024, budget: 0 },
                meta_bytes: 1024, meta_count: 4,
            }),
            clear_cache: answer({ success: true }),
            update_settings: answer({ success: true }),
        },
    };
}

const browser = await chromium.launch();
const failures = [];

function check(label, ok, detail) {
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ' -- ' + detail : ''}`);
    if (!ok) failures.push(label);
}

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ncm-features-'));
fs.copyFileSync(path.join(web, 'index.html'), path.join(dir, 'index.html'));
fs.copyFileSync(path.join(web, 'app.js'), path.join(dir, 'app.js'));
fs.copyFileSync(path.join(web, 'style.css'), path.join(dir, 'style.css'));

const page = await browser.newPage({ viewport: VIEWPORT });
page.on('pageerror', (err) => failures.push(`page error: ${err.message}`));
await page.addInitScript(stubBridge);
await page.goto('file://' + path.join(dir, 'index.html'));
await page.waitForTimeout(900);

/* ------------------------------------------------------ 1. download list */
await page.evaluate(() => go('downloads'));
await page.waitForTimeout(500);
const queue = await page.evaluate(() => {
    const host = document.querySelector('#download-queue');
    if (!host) return null;
    return {
        html: host.innerHTML,
        spinners: host.querySelectorAll('.spinner, .loading, .spin').length,
        text: host.textContent || '',
    };
});
check('the download queue is not a spinner that never stops',
    queue && queue.spinners === 0,
    queue ? `${queue.spinners} spinners, text "${queue.text.trim()}"` : 'no host');
check('the download queue says it is empty',
    queue && /还没有下载任务/.test(queue.text),
    queue ? queue.text.trim() : '');

/* ------------------------------------------------------- 2. artist rows */
await page.evaluate(() => openFollowedArtists());
await page.waitForTimeout(500);
const followed = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('#sheet-body .artist-row'));
    const first = rows[0];
    const avatar = first ? first.querySelector('.artist-avatar') : null;
    const name = first ? first.querySelector('.grow') : null;
    return {
        count: rows.length,
        avatar: avatar ? Math.round(avatar.getBoundingClientRect().width) : 0,
        avatarHeight: avatar ? Math.round(avatar.getBoundingClientRect().height) : 0,
        avatarTop: avatar ? Math.round(avatar.getBoundingClientRect().top) : 0,
        nameTop: name ? Math.round(name.getBoundingClientRect().top) : 0,
        nameText: name ? name.textContent : '',
        tiles: document.querySelectorAll('#sheet-body .card').length,
    };
});
check('the followed artists are rows, not big tiles',
    followed && followed.tiles === 0 && followed.count > 0,
    followed ? `${followed.count} rows, ${followed.tiles} tiles` : 'none');
check('the avatar is small', followed && followed.avatar > 0 && followed.avatar <= 56,
    followed ? `${followed.avatar}x${followed.avatarHeight}px` : 'none');
check('the avatar and the name are on the same line',
    followed && Math.abs(followed.avatarTop - followed.nameTop) < 24,
    followed ? `avatar top ${followed.avatarTop}, name top ${followed.nameTop}` : '');
await page.evaluate(() => closeSheet());
await page.waitForTimeout(200);

/* ------------------------------------------------------------ 3. lyrics */
await page.evaluate(() => {
    state.queue = [{ id: 1, name: '测试歌曲', artists: '测试歌手',
        artist_list: [{ id: '77', name: '测试歌手' }], album: '测试专辑',
        duration: 200, cover_url: '', source: 'online' }];
    state.index = 0;
    openPlayer();
});
await page.waitForTimeout(400);
const beforeTap = await page.evaluate(() => ({
    coverHidden: document.querySelector('#player-cover').hidden,
    lyricsHidden: document.querySelector('#player-lyrics').hidden,
}));
check('the player starts on the cover',
    beforeTap && beforeTap.coverHidden === false && beforeTap.lyricsHidden === true,
    JSON.stringify(beforeTap));

await page.evaluate(() => document.querySelector('#player-cover').click());
await page.waitForTimeout(700);
const afterTap = await page.evaluate(() => ({
    coverHidden: document.querySelector('#player-cover').hidden,
    lyricsHidden: document.querySelector('#player-lyrics').hidden,
    lines: document.querySelectorAll('#lyrics-box .lyric-line').length,
    text: (document.querySelector('#lyrics-box').textContent || '').trim(),
}));
check('tapping the cover shows the lyrics',
    afterTap && afterTap.coverHidden === true && afterTap.lyricsHidden === false,
    JSON.stringify(afterTap));
check('the lyrics are drawn', afterTap && afterTap.lines === 3,
    afterTap ? `${afterTap.lines} lines` : '');

await page.evaluate(() => document.querySelector('#player-lyrics').click());
await page.waitForTimeout(400);
const backToCover = await page.evaluate(() => ({
    coverHidden: document.querySelector('#player-cover').hidden,
    lyricsHidden: document.querySelector('#player-lyrics').hidden,
}));
check('tapping the lyrics goes back to the cover',
    backToCover && backToCover.coverHidden === false && backToCover.lyricsHidden === true,
    JSON.stringify(backToCover));
await page.evaluate(() => closePlayer());
await page.waitForTimeout(200);

/* ------------------------------------------------- 4. track details */
await page.evaluate(() => {
    const track = { id: 5, name: '测试歌曲', artists: '测试歌手',
        artist_list: [{ id: '77', name: '测试歌手' }], album: '测试专辑',
        duration: 245000, cover_url: '', source: 'online' };
    openTrackDetails(track);
});
await page.waitForTimeout(400);
const details = await page.evaluate(() => {
    const body = document.querySelector('#sheet-body');
    const link = body ? body.querySelector('.artist-link') : null;
    const text = body ? body.textContent : '';
    return {
        hasLink: !!link,
        linkText: link ? link.textContent : '',
        mentionsAlbum: /测试专辑/.test(text),
        mentionsDuration: /04:05/.test(text),
        mentionsWrongDuration: /3747:|245:|245000/.test(text),
    };
});
check('the track details list the album and the length',
    details && details.mentionsAlbum && details.mentionsDuration,
    JSON.stringify(details));
check('the length is in minutes and seconds, not milliseconds',
    details && details.mentionsWrongDuration === false,
    details ? details.text : '');
check('the artist in the details is a link', details && details.hasLink,
    details ? details.linkText : '');

await page.evaluate(() => {
    const link = document.querySelector('#sheet-body .artist-link');
    if (link) link.click();
});
await page.waitForTimeout(800);
/* The artist page is a page, not a sheet, so it lands in the detail view. */
const artistPage = await page.evaluate(() => {
    const detail = document.querySelector('#view-detail');
    return {
        active: detail ? detail.classList.contains('is-active') : false,
        title: (document.querySelector('#detail-head') || {}).textContent || '',
        body: (document.querySelector('#detail-body') || {}).textContent || '',
        rows: document.querySelectorAll('#detail-body .track').length,
    };
});
check('the artist link opens the artist page',
    artistPage && artistPage.active && /测试歌手/.test(artistPage.title),
    artistPage ? artistPage.title.replace(/\s+/g, ' ').slice(0, 60) : '');
check('the artist page lists the hot songs',
    artistPage && artistPage.rows > 0,
    artistPage ? `${artistPage.rows} songs` : '');
await page.evaluate(() => closeDetail());
await page.waitForTimeout(200);

/* ------------------------------------------------------- 5. settings */
await page.evaluate(() => openSettings());
await page.waitForTimeout(700);
const settings = await page.evaluate(() => {
    const body = document.querySelector('#sheet-body');
    if (!body) return null;
    const text = body.textContent || '';
    const toggles = body.querySelectorAll('.switch');
    const remember = Array.from(toggles).find((t) => {
        const row = t.closest('.list-row');
        return row && /记住登录/.test(row.textContent);
    });
    return {
        text,
        toggles: toggles.length,
        rememberOn: remember ? remember.classList.contains('is-on') : null,
        hasCacheSize: /缓存大小/.test(text),
        hasClear: /清除缓存/.test(text),
    };
});
check('the settings page opens', settings && settings.toggles >= 1,
    settings ? `${settings.toggles} switches` : 'no settings');
check('remember-login is switched on',
    settings && settings.rememberOn === true,
    settings ? String(settings.rememberOn) : '');
check('the cache size is shown', settings && settings.hasCacheSize,
    settings ? settings.text.replace(/\s+/g, ' ').slice(0, 120) : '');
check('the cache can be cleared', settings && settings.hasClear, '');

/* ------------------------------------------------- 6. the size unit */
const units = await page.evaluate(() => [
    formatCacheSize(0), formatCacheSize(512),
    formatCacheSize(2048), formatCacheSize(5 * 1024 * 1024),
    formatCacheSize(3 * 1024 * 1024 * 1024),
]);
check('the cache size reads in a human unit',
    units[0] === '0 B' && units[1] === '512 B' && units[2] === '2.0 KB'
    && units[3] === '5.0 MB' && units[4] === '3.00 GB',
    JSON.stringify(units));

/* ------------------------------------- 7. the settings entry exists */
await page.evaluate(() => { closeSheet(); go('me'); });
await page.waitForTimeout(500);
const mePage = await page.evaluate(() => {
    const text = document.querySelector('#view-me').textContent || '';
    return { hasSettings: /设置/.test(text), text: text.replace(/\s+/g, ' ').slice(0, 160) };
});
check('the settings entry is on the me page', mePage && mePage.hasSettings,
    mePage ? mePage.text : '');

await page.close();
fs.rmSync(dir, { recursive: true, force: true });
await browser.close();

console.log('');
if (failures.length) {
    console.log(`${failures.length} check(s) failed:`);
    failures.forEach((f) => console.log(`  - ${f}`));
    process.exit(1);
}
console.log('all checks passed');
