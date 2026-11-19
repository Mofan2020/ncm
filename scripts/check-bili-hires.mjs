#!/usr/bin/env node
/**
 * Checks for the Hi-Res track of a Bilibili video.
 *
 * The question the loader has to answer is not "does the user want
 * Hi-Res" but "does this video have a track this account may play".
 * Asking for a track the server did not hand over wastes a request and
 * leaves the player with a sound that never starts, so the decision has
 * to be made from the answer of the playurl route.
 *
 *   1. 想要而且有 → 用
 *   2. 想要但没有 → 不用（视频没有这条轨）
 *   3. 想要但没有权限 → 不用（账号拿不到）
 *   4. 不想要 → 不用
 *   5. 播放函数真的收到了 hires 这个标记
 *
 * Run:  node scripts/check-bili-hires.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const assets = path.join(root, 'src', 'gui', 'assets');
const source = fs.readFileSync(path.join(assets, 'bili-stream-loader.js'), 'utf8');

let failures = 0;
function check(name, ok, detail) {
    if (ok) {
        console.log(`  ok   ${name}`);
    } else {
        failures += 1;
        console.log(`  FAIL ${name}${detail ? ` -- ${detail}` : ''}`);
    }
}

/** Load the loader into a fresh context; record what it asked to play. */
function load(streams) {
    const played = [];
    const context = {
        window: {
            playBiliVideo: async (videoEl, answer, options) => {
                played.push({ answer, options });
                return true;
            },
        },
        setTimeout,
        clearTimeout,
        console,
        Error,
        Promise,
    };
    vm.createContext(context);
    vm.runInContext(source, context, { filename: 'bili-stream-loader.js' });
    return { loader: context.window.BiliStreamLoader, played };
}

function makeApi(streams) {
    return {
        get_bilibili_video_streams: async () => streams,
        get_bilibili_video_progress: async () => ({ success: false }),
    };
}

/** The streams of a video that carries a lossless track. */
const WITH_HIRES = {
    success: true,
    title: '视频',
    audio: { url: 'http://127.0.0.1/a.m4s', mime: 'audio/mp4', codecs: 'mp4a.40.2' },
    video: { url: 'http://127.0.0.1/v.m4s', mime: 'video/mp4', codecs: 'avc1.640032' },
    flac: { url: 'http://127.0.0.1/flac.m4s', mime: 'audio/mp4', codecs: 'flac' },
    hires_available: true,
    hires_granted: true,
};

/** The streams of a video without one. */
const WITHOUT_HIRES = {
    success: true,
    title: '视频',
    audio: { url: 'http://127.0.0.1/a.m4s', mime: 'audio/mp4', codecs: 'mp4a.40.2' },
    video: { url: 'http://127.0.0.1/v.m4s', mime: 'video/mp4', codecs: 'avc1.640032' },
    flac: null,
    hires_available: false,
    hires_granted: false,
};

/** The streams of a video that has one, but the account may not have it. */
const HIRES_WITHOUT_PERMISSION = {
    success: true,
    title: '视频',
    audio: { url: 'http://127.0.0.1/a.m4s', mime: 'audio/mp4', codecs: 'mp4a.40.2' },
    video: { url: 'http://127.0.0.1/v.m4s', mime: 'video/mp4', codecs: 'avc1.640032' },
    flac: null,
    hires_available: true,
    hires_granted: false,
};

async function playWith(streams, options) {
    const { loader, played } = load(streams);
    const outcome = await loader.load({
        api: makeApi(streams),
        bvid: 'BV1xx411c7mD',
        quality: 0,
        videoEl: { currentTime: 0 },
        ...options,
    });
    return { outcome, played };
}

async function main() {
    console.log('bili hires');

    /* 1. Wanted, and the video has it. */
    {
        const { outcome, played } = await playWith(WITH_HIRES, { hires: true });
        check('a video with a track plays it',
            outcome.ok === true && outcome.hires === true,
            JSON.stringify(outcome));
        check('the player is told to use the lossless track',
            played.length === 1 && played[0].options.hires === true,
            JSON.stringify(played[0] && played[0].options));
    }

    /* 2. Wanted, but the video has no such track. */
    {
        const { outcome, played } = await playWith(WITHOUT_HIRES, { hires: true });
        check('a video without a track does not use one',
            outcome.ok === true && outcome.hires === false,
            JSON.stringify(outcome));
        check('and the player is told not to',
            played.length === 1 && played[0].options.hires === false,
            JSON.stringify(played[0] && played[0].options));
    }

    /* 3. Wanted, the video has one, but the account may not have it. */
    {
        const { outcome, played } = await playWith(HIRES_WITHOUT_PERMISSION, { hires: true });
        check('a track the account may not have is not used',
            outcome.ok === true && outcome.hires === false,
            JSON.stringify(outcome));
        check('and the player is told not to',
            played.length === 1 && played[0].options.hires === false,
            JSON.stringify(played[0] && played[0].options));
    }

    /* 4. Not wanted at all. */
    {
        const { outcome, played } = await playWith(WITH_HIRES, { hires: false });
        check('a track that is not wanted is left alone',
            outcome.ok === true && outcome.hires === false,
            JSON.stringify(outcome));
        check('and the player is told not to',
            played.length === 1 && played[0].options.hires === false,
            JSON.stringify(played[0] && played[0].options));
    }

    /* 5. The flag has to reach the player, not just the answer. */
    {
        const { played } = await playWith(WITH_HIRES, { hires: true });
        const options = played[0] && played[0].options;
        check('the player gets the flag as a boolean',
            options && typeof options.hires === 'boolean',
            JSON.stringify(options));
        check('the player gets the streams it has to play',
            played[0] && played[0].answer === WITH_HIRES,
            'the answer was not handed over');
    }

    /* 6. The two players have a switch, and it is wired to the loader. */
    {
        const appJs = fs.readFileSync(path.join(assets, 'app.js'), 'utf8');
        const windowJs = fs.readFileSync(path.join(assets, 'bili-player-window.js'), 'utf8');
        const indexHtml = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
        const playerHtml = fs.readFileSync(path.join(assets, 'bili-player.html'), 'utf8');

        check('the inline player has the switch',
            indexHtml.includes('id="bili-hires-on"'));
        check('the standalone window has the switch',
            playerHtml.includes('id="bp-hires-on"'));
        check('the inline player reads the switch',
            appJs.includes("getElementById('bili-hires-on')"));
        check('the standalone window reads the switch',
            windowJs.includes("el('bp-hires-on')"));
        check('the inline player passes the flag to the loader',
            appJs.includes('hires: biliHiresWanted()'));
        check('the standalone window passes the flag to the loader',
            windowJs.includes('hires: hiresWanted()'));
        // The switch may only appear when the video has the track, so the
        // field starts hidden and the code decides.
        check('the switch starts hidden',
            indexHtml.includes('id="bili-hires-field" hidden')
            && playerHtml.includes('id="bp-hires-field" hidden'));
    }

    /* 7. The setting is read and handed over. */
    {
        const appJs = fs.readFileSync(path.join(assets, 'app.js'), 'utf8');
        check('the setting is read back into the form',
            appJs.includes("setChecked('bili-hires-check', bili.hires_enabled === true)"));
        check('the setting is written back to the backend',
            appJs.includes("hires_enabled: document.getElementById('bili-hires-check').checked"));
    }

    console.log('');
    if (failures) {
        console.error(`${failures} check(s) failed`);
        process.exit(1);
    }
    console.log('all checks passed');
}

await main();
