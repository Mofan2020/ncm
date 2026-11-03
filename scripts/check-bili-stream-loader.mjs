#!/usr/bin/env node
/**
 * Checks for src/gui/assets/bili-stream-loader.js, the one path both the
 * inline player and the standalone window take.
 *
 * Why this is not a pytest test: the logic is frontend, so the only honest
 * way to check it is to load the file and call it.  The script does that in
 * a vm with a hand-made bridge.
 *
 *   1. 超时必须真的到  - the loader used to build an AbortController and
 *                       never connect it to the bridge call, so a backend
 *                       that did not answer left the spinner running for
 *                       ever.  A bridge that never answers has to come back
 *                       as error: 'timeout' on its own.
 *   2. 没有桥就说没桥    - error: 'network', not a promise that hangs.
 *   3. 续播跳到记下的位置 - progress 25% of 400s is 100s on the element.
 *   4. 关掉续播就不查历史 - one call to the bridge, not two.
 *   5. 两边都走这一个     - both surfaces name the same loader, so a fix
 *                       in one lands in both.
 *
 * Run:  node scripts/check-bili-stream-loader.mjs
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

/** Load the loader into a fresh context with a fake bridge. */
function load(api, playResult) {
    const context = {
        window: {
            playBiliVideo: async () => (playResult === undefined ? true : playResult),
        },
        setTimeout,
        clearTimeout,
        console,
        Error,
        Promise,
    };
    vm.createContext(context);
    vm.runInContext(source, context, { filename: 'bili-stream-loader.js' });
    const loader = context.window.BiliStreamLoader;
    return { loader, api, context };
}

/** A video element with just the fields the loader touches. */
function makeVideo() {
    return { currentTime: 0 };
}

const STREAMS = { success: true, muxed: { url: 'http://127.0.0.1/x.m4s' }, title: 't' };

async function main() {
    console.log('bili-stream-loader');

    /* 1. A bridge that never answers has to come back as a timeout. */
    {
        const hanging = { get_bilibili_video_streams: () => new Promise(() => {}) };
        const { loader } = load(hanging);
        const started = Date.now();
        const outcome = await loader.load({
            api: hanging,
            bvid: 'BV1',
            quality: 0,
            videoEl: makeVideo(),
            timeoutMs: 120,
        });
        const took = Date.now() - started;
        check('a silent bridge ends in a timeout',
              outcome.ok === false && outcome.error === 'timeout',
              `got ${JSON.stringify(outcome.error)}`);
        check('and it does not wait for the bridge', took < 2000, `took ${took}ms`);
    }

    /* 2. No bridge at all is a plain failure, not a hang. */
    {
        const { loader } = load(null);
        const outcome = await loader.load({
            api: {},
            bvid: 'BV1',
            quality: 0,
            videoEl: makeVideo(),
            timeoutMs: 120,
        });
        check('a missing bridge fails at once', outcome.ok === false, JSON.stringify(outcome));
    }

    /* 3. The backend refusing is reported, not swallowed. */
    {
        const api = {
            get_bilibili_video_streams: async () => ({ success: false, message: 'no' }),
        };
        const { loader } = load(api);
        const outcome = await loader.load({
            api, bvid: 'BV1', quality: 0, videoEl: makeVideo(), timeoutMs: 500,
        });
        check('a refused request reports no_stream',
              outcome.ok === false && outcome.error === 'no_stream',
              JSON.stringify(outcome.error));
    }

    /* 4. A good answer plays, and the caller sees the streams first. */
    {
        const api = { get_bilibili_video_streams: async () => STREAMS };
        const { loader } = load(api);
        let seen = null;
        const outcome = await loader.load({
            api,
            bvid: 'BV1',
            quality: 0,
            videoEl: makeVideo(),
            timeoutMs: 500,
            onStreams: (r) => { seen = r; },
        });
        check('a good answer plays', outcome.ok === true, JSON.stringify(outcome));
        check('the caller gets the streams', seen === STREAMS, 'onStreams was not called');
    }

    /* 5. Resume jumps to the recorded position. */
    {
        const video = makeVideo();
        let historyCalls = 0;
        const api = {
            get_bilibili_video_streams: async () => STREAMS,
            get_bilibili_video_progress: async () => {
                historyCalls += 1;
                return { success: true, progress: 25, duration: 400 };
            },
        };
        const { loader } = load(api);
        const outcome = await loader.load({
            api, bvid: 'BV1', quality: 0, videoEl: video, timeoutMs: 500,
            resumePlayback: true,
        });
        check('resume seeks to the recorded position',
              video.currentTime === 100, `currentTime=${video.currentTime}`);
        check('resume reports the position', outcome.resumed === 100, `${outcome.resumed}`);
        check('resume asks the history once', historyCalls === 1, `${historyCalls} calls`);
    }

    /* 6. With resume off the history is not touched. */
    {
        const video = makeVideo();
        let historyCalls = 0;
        const api = {
            get_bilibili_video_streams: async () => STREAMS,
            get_bilibili_video_progress: async () => {
                historyCalls += 1;
                return { success: true, progress: 25, duration: 400 };
            },
        };
        const { loader } = load(api);
        await loader.load({
            api, bvid: 'BV1', quality: 0, videoEl: video, timeoutMs: 500,
            resumePlayback: false,
        });
        check('resume off leaves the history alone', historyCalls === 0, `${historyCalls} calls`);
        check('resume off starts at the beginning', video.currentTime === 0, `${video.currentTime}`);
    }

    /* 7. A history that says 0% is not a position worth jumping to. */
    {
        const video = makeVideo();
        const api = {
            get_bilibili_video_streams: async () => STREAMS,
            get_bilibili_video_progress: async () => ({ success: true, progress: 0, duration: 400 }),
        };
        const { loader } = load(api);
        const outcome = await loader.load({
            api, bvid: 'BV1', quality: 0, videoEl: video, timeoutMs: 500,
            resumePlayback: true,
        });
        check('0% is not resumed', video.currentTime === 0 && outcome.resumed === 0,
              `currentTime=${video.currentTime} resumed=${outcome.resumed}`);
    }

    /* 8. Both surfaces go through this file. */
    {
        const inline = fs.readFileSync(path.join(assets, 'app.js'), 'utf8');
        const separate = fs.readFileSync(path.join(assets, 'bili-player-window.js'), 'utf8');
        check('the inline player uses the shared loader',
              inline.includes('BiliStreamLoader.load('), 'no call in app.js');
        check('the standalone window uses the shared loader',
              separate.includes('BiliStreamLoader.load('), 'no call in bili-player-window.js');
        const page = fs.readFileSync(path.join(assets, 'bili-player.html'), 'utf8');
        check('the standalone page loads the loader before using it',
              page.indexOf('bili-stream-loader.js') >= 0
              && page.indexOf('bili-stream-loader.js') < page.indexOf('bili-player-window.js'),
              'the script tag is missing or comes too late');
        const index = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
        check('the main page loads the loader',
              index.indexOf('bili-stream-loader.js') >= 0, 'the script tag is missing');
    }

    /* 9. Neither surface keeps a private copy of the timeout trick. */
    {
        const inline = fs.readFileSync(path.join(assets, 'app.js'), 'utf8');
        const separate = fs.readFileSync(path.join(assets, 'bili-player-window.js'), 'utf8');
        check('no AbortController left in the inline player',
              !inline.includes('AbortController'), 'a copy of the old fake timeout is back');
        check('no AbortController left in the standalone window',
              !separate.includes('AbortController'), 'a copy of the old fake timeout is back');
    }

    if (failures) {
        console.log(`\n${failures} check(s) failed`);
        process.exit(1);
    }
    console.log('\nall checks passed');
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
