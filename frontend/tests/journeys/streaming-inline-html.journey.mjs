// #1627: real provider -> harness -> streamed transcript, at desktop and phone sizes.
// Initial tail-to-prefix promotions and the final gallery fold may recreate a
// player. Identity is promised here only once its prefix stops changing.
import { fileURLToPath } from 'node:url';
import { runJourney } from './runner.mjs';
import { expect, openNewChat, scriptTag, typeAndSend, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { frames } from './lib/timeline.mjs';

const movie = '/__journey__/streaming-inline-html.mp4';
const fixture = fileURLToPath(new URL('../../../android/app/src/androidTest/assets/native-media.mp4', import.meta.url));
const ghost = 'https://example.com/reasoning-ghost.mp4';

async function waitForVideo(page, playing = false) {
	const status = await page.waitForFunction((playing) => {
		const video = document.querySelector('video');
		if (video?.error) {
			return `Video MediaError ${video.error.code}: ${video.error.message} (${video.currentSrc || video.src})`;
		}
		return playing ? video?.currentTime > 0.1 : video?.readyState >= 2;
	}, playing);
	const result = await status.jsonValue();
	await status.dispose();
	expect(result === true, String(result));
}

async function observe(page, selector) {
	await page.evaluate((selector) => {
		const q = window.__splitQA = { added: 0, max: 0 };
		q.observer = new MutationObserver((changes) => {
			for (const change of changes) for (const node of change.addedNodes) {
				if (node.nodeType === 1) q.added += Number(node.matches(selector)) + node.querySelectorAll(selector).length;
			}
			q.max = Math.max(q.max, document.querySelectorAll(selector).length);
		});
		q.observer.observe(document.body, { childList: true, subtree: true });
	}, selector);
}

async function expectAbsent(page, selector, log) {
	const q = await page.evaluate(() => ({ added: window.__splitQA.added, max: window.__splitQA.max }));
	expect(q.added === 0 && q.max === 0, `no transient ${selector}: ${JSON.stringify(q)}`);
	expect(await page.locator(selector).count() === 0, `no settled ${selector}`);
	log(JSON.stringify(q));
}

await runJourney({
	name: 'streaming-inline-html',
	// Like composer-no-empty-seat: a controlling SW bypasses page.route and
	// returns the real server's JSON instead of our video fixture on localhost.
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	allow: DEV_INSTANCE_NOISE,
	recorder: { selectors: ['videos=video', 'thinking=.thinking-block.streaming'] },
	cases: [
		{
			name: 'unclosed-anchor',
			async setup(ctx) { await openNewChat(ctx); },
			async act({ page, variant, mark, log }) {
				await observe(page, 'video');
				await mark('stream the unclosed raw anchor and media URL');
				await typeAndSend(page, scriptTag(variant, 'stream-inline-html', { tps: 1 }));
				await waitChat(page, { streaming: 'true', messagesAtLeast: 2 });
				await waitTurnSettled(page, { text: 'https://example.com/clip.mp4' });
				await expectAbsent(page, 'video', log);
			},
			verify({ recording }) {
				expect(frames(recording).some(({ state }) => state.streaming === 'true'), 'observed the actual stream');
				expect(frames(recording).every(({ state }) => !state.videos), 'no player on any recorded frame');
			}
		},
		{
			name: 'image-alt-stable-player',
			async setup(ctx) {
				// The built app (unlike Vite) has media-src 'self' blob:. Keep the
				// scenario's URL same-origin: CSP blocks external media before a route
				// can fulfil it. Real MP4 bytes/decoder, never the external network.
				ctx.mediaRequests = [];
				await ctx.page.route((url) => url.pathname === movie, (route) => {
					ctx.mediaRequests.push(route.request().headers().range || 'no Range');
					return route.fulfill({ path: fixture, contentType: 'video/mp4' });
				});
				await openNewChat(ctx);
			},
			async act({ page, variant, mark, log, mediaRequests }) {
				await mark('stream image alt HTML before a real media URL');
				await typeAndSend(page, scriptTag(variant, 'stream-image-alt-html', { tps: 4 }));
				await page.locator('[role="article"]:has(video)').filter({ hasText: 'later03' }).waitFor();
				expect(mediaRequests.length > 0, 'the fixture route served the actual media request (not a service-worker fetch)');
				await waitForVideo(page);
				expect(await page.evaluate(() => new URL(document.querySelector('video').currentSrc).origin === location.origin), 'media is same-origin under the built app CSP');
				log(`fixture requests: ${JSON.stringify(mediaRequests)}`);
				await page.evaluate(async () => {
					const video = document.querySelector('video');
					video.muted = true;
					await video.play();
				});
				await waitForVideo(page, true);
				await mark('stable prefix: preserve player and playback position during later deltas');
				await page.evaluate(() => {
					const video = document.querySelector('video');
					video.pause();
					const q = window.__playerQA = { node: video, time: video.currentTime, added: 0, removed: 0, samples: 0, replaced: 0, reset: 0 };
					q.observer = new MutationObserver((changes) => {
						for (const change of changes) for (const [key, nodes] of [['added', change.addedNodes], ['removed', change.removedNodes]]) {
							for (const node of nodes) if (node.nodeType === 1) q[key] += Number(node.matches('video')) + node.querySelectorAll('video').length;
						}
					});
					q.observer.observe(document.body, { childList: true, subtree: true });
					function sample() {
						if (q.stopped) return;
						q.samples++;
						if (document.querySelector('video') !== video) q.replaced++;
						if (Math.abs(video.currentTime - q.time) > 0.05) q.reset++;
						requestAnimationFrame(sample);
					}
					requestAnimationFrame(sample);
				});
				await page.locator('[role="article"]:has(video)').filter({ hasText: 'later40' }).waitFor();
				await waitChat(page, { streaming: 'true' });
				const q = await page.evaluate(() => {
					const q = window.__playerQA;
					q.stopped = true;
					q.observer.disconnect();
					return { added: q.added, removed: q.removed, replaced: q.replaced, reset: q.reset, samples: q.samples, time: q.time, same: q.node === document.querySelector('video') };
				});
				expect(q.samples > 10 && q.same && q.time > 0.1, `played and sampled the stable player: ${JSON.stringify(q)}`);
				expect(q.added === 0 && q.removed === 0 && q.replaced === 0 && q.reset === 0, `no per-delta reset: ${JSON.stringify(q)}`);
				log(`stable player: ${JSON.stringify(q)}`);
				// End the recording before the designed settle/gallery fold. It is
				// outside the stable-prefix identity promise, not an allowed anomaly.
			},
			async verify({ page, recording }) {
				expect(frames(recording).some(({ state }) => state.videos === 1 && state.streaming === 'true'), 'player was visible while streaming');
				await waitTurnSettled(page, { text: 'later64' });
			}
		},
		{
			name: 'reasoning-unclosed-anchor',
			async setup(ctx) { await openNewChat(ctx); },
			async act({ page, variant, mark, log }) {
				const selector = `.thinking-content a[href="${ghost}"]`;
				await observe(page, selector);
				await mark('stream actual provider reasoning into ThinkingBlock');
				await typeAndSend(page, scriptTag(variant, 'stream-thinking-inline-html', { tps: 2 }));
				const header = page.locator('.thinking-block.streaming .thinking-header');
				await header.waitFor({ timeout: 30000 });
				if (await header.getAttribute('aria-expanded') !== 'true') await header.click();
				await page.locator('.thinking-block.streaming .thinking-content').filter({ hasText: ghost }).waitFor();
				expect(await page.locator(selector).count() === 0, 'no fresh autolink in the streamed reasoning tail');
				await waitTurnSettled(page, { text: 'Reasoning complete.' });
				// Settling folds the work log, retaining this already-expanded
				// ThinkingBlock under a hidden parent. Open the real disclosure.
				await mark('reopen the settled work log to inspect reasoning');
				await page.locator('.worklog[data-work-log-mode="settled"]:has(.thinking-block) > .worklog-measure > button[aria-expanded="false"]').click();
				await page.locator('.thinking-content').filter({ hasText: ghost }).waitFor();
				await expectAbsent(page, selector, log);
			},
			verify({ recording }) {
				expect(frames(recording).some(({ state }) => state.thinking > 0), 'real streaming ThinkingBlock recorded');
			}
		}
	]
});
