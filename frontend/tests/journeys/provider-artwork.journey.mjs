// Provider settings must show the built-in marks, not initials or generic tiles.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const imageBrands = ['minimax', 'google', 'antigravity'];
const vectorBrands = ['openai', 'grok', 'claude', 'openrouter', 'kimi', 'opencode', 'fal'];

async function assertImage(logo, label) {
	await logo.waitFor();
	await logo.evaluate(async (element, name) => {
		if (!(element instanceof HTMLImageElement)) throw new Error(`${name}: expected a provider image`);
		try {
			await element.decode();
		} catch (error) {
			throw new Error(`${name}: provider image did not decode`, { cause: error });
		}
	}, label);
}

runJourney({
	name: 'provider-artwork',
	allow: DEV_INSTANCE_NOISE,
	recorder: { selectors: ['providers=button[data-settings-item^="provider:"]', 'logo=.st-identity [data-provider-logo]'] },
	cases: [{
		name: 'built-in-marks-list-and-antigravity-detail',
		async setup({ page, status }) {
			await gotoApp(page, status);
			await showSidebar(page);
			await page.getByRole('navigation', { name: 'Workspace', exact: true })
				.getByRole('button', { name: 'Settings', exact: true }).click();
		},
		async act({ page, mark, log }) {
			await mark('open Providers');
			await page.getByRole('navigation', { name: 'Settings', exact: true })
				.getByRole('button', { name: 'Providers', exact: true }).click();
			const row = (id) => page.locator(`button[data-settings-item="provider:${id}"]`).filter({ visible: true });
			const logo = (id) => row(id).locator(`[data-provider-logo="${id}"]`);
			for (const id of vectorBrands) {
				const vector = logo(id);
				await vector.waitFor();
				const isBrandMark = await vector.evaluate((element) => {
					if (!(element instanceof SVGSVGElement) || element.querySelector('text, rect')) return false;
					const paths = [...element.querySelectorAll('path[d]')];
					// These marks are filled silhouettes; the generic cube is a stroked outline.
					return paths.length > 0 && paths.every(path => {
						const { fill, stroke } = getComputedStyle(path);
						return fill !== 'none' && stroke === 'none' && path.getTotalLength() > 0;
					});
				});
				expect(isBrandMark, `${id} list: expected a filled vector mark, not the generic cube or an initial`);
			}
			for (const id of imageBrands) await assertImage(logo(id), `${id} list`);
			log(`Provider list: ${vectorBrands.join(', ')} vector marks; ${imageBrands.join(', ')} images decoded`);

			await mark('open Antigravity');
			await row('antigravity').click();
			const detail = page.locator('.st-identity [data-provider-logo="antigravity"]').filter({ visible: true });
			await assertImage(detail, 'antigravity detail');
			log('Antigravity detail: image decoded');
		}
	}]
});
