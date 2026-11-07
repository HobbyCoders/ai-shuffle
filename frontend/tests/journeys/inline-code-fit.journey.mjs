// Short inline code (the renderer's `code-short`, up to 40 characters) stays
// one whole chip, but a character count can't prove the chip fits: 40
// full-width characters are wider than a phone column. Measured on the app's
// real built .prose CSS, in narrow columns under an overflow-hidden ancestor
// like the transcript's: the chip never clips, never overflows the column,
// stays one box, and a short ASCII span still keeps to one line.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const CJK = '設定ファイルの場所は設定ファイルの場所は設定ファイルの場所は設定ファイルの場所は';
const PATH = 'src/lib/components/chat/phone/compose.ts';

await runJourney({ name: 'inline-code-fit', allow: DEV_INSTANCE_NOISE, cases: [{
	name: 'short-code-never-clips-in-a-narrow-column',
	async setup({ page, status }) { await gotoApp(page, status); },
	async act({ page, mark }) {
		expect(CJK.length === 40 && PATH.length <= 40, 'both spans are code-short by the renderer\'s 40-character rule');
		for (const width of [160, 240, 358]) {
			await mark(`short inline code in a ${width}px prose column`);
			const m = await page.evaluate(({ width, cjk, path }) => {
				const host = document.createElement('div');
				host.style.cssText = `position:fixed;left:0;top:0;width:${width}px;overflow:hidden;z-index:99999;background:var(--background)`;
				host.innerHTML = `<div class="prose" data-fit><p>See <code class="code-short" data-k="cjk">${cjk}</code> then</p>`
					+ `<p>Then run the long words here <code class="code-short" data-k="path">${path}</code> now</p>`
					+ `<p>Run <code class="code-short" data-k="short">npm run check</code> once</p></div>`;
				document.body.append(host);
				const col = host.querySelector('[data-fit]').getBoundingClientRect();
				const out = {};
				for (const code of host.querySelectorAll('code')) {
					const r = code.getBoundingClientRect();
					const cs = getComputedStyle(code);
					// Its natural one-line width: the same chip, uncapped and unwrapped.
					const twin = code.cloneNode(true);
					twin.style.cssText = 'max-width:none;white-space:nowrap;position:absolute;visibility:hidden';
					host.append(twin);
					const natural = twin.getBoundingClientRect().width;
					twin.remove();
					out[code.dataset.k] = { left: r.left, right: r.right, height: r.height, rects: code.getClientRects().length,
						scroll: code.scrollWidth, client: code.clientWidth, line: parseFloat(cs.lineHeight) + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom),
						natural, display: cs.display, whiteSpace: cs.whiteSpace, maxWidth: cs.maxWidth };
				}
				host.remove();
				return { col: { left: col.left, right: col.right, width: col.width }, codes: out };
			}, { width, cjk: CJK, path: PATH });
			for (const [k, c] of Object.entries(m.codes)) {
				expect(c.display === 'inline-block' && c.maxWidth === '100%', `G8: ${k} code is one inline box capped at the column ${JSON.stringify(c)}`);
				expect(c.rects === 1, `G8: ${k} code stays one chip, never two pills ${JSON.stringify(c)}`);
				expect(c.left >= m.col.left - 0.5 && c.right <= m.col.right + 0.5, `G8: ${k} code stays inside the ${width}px column ${JSON.stringify({ c, col: m.col })}`);
				expect(c.scroll <= c.client + 1, `G8: ${k} code text is not clipped inside its chip ${JSON.stringify(c)}`);
			}
			// 40 full-width characters: wider than the column, they wrap inside the one
			// chip; when they fit it (about 312px in a 358px column), they keep to one line.
			const cjk = m.codes.cjk;
			if (cjk.natural > m.col.width + 0.5) expect(cjk.height > cjk.line * 1.5, `G8: 40 wide characters wider than the ${width}px column wrap inside the chip ${JSON.stringify(cjk)}`);
			else expect(cjk.height <= cjk.line + 1, `G8: 40 wide characters that fit the ${width}px column stay on one line ${JSON.stringify(cjk)}`);
			// A short ASCII span that fits the column keeps to one line.
			expect(m.codes.short.height <= m.codes.short.line + 1, `G8: a short span stays on one line at ${width}px ${JSON.stringify(m.codes.short)}`);
			if (width >= 358) expect(m.codes.path.height <= m.codes.path.line + 1, `G8: a 40-character path fits one line in a phone column ${JSON.stringify(m.codes.path)}`);
		}
	}
}]});
