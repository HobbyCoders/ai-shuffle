// Provider settings must show the built-in marks, not initials or generic tiles.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const builtInBrands = ['openai', 'grok', 'claude', 'openrouter', 'kimi', 'minimax', 'opencode', 'google', 'antigravity', 'fal'];

async function assertVectorMark(logo, label) {
	await logo.waitFor();
	const isBrandMark = await logo.evaluate((element) => {
		if (!(element instanceof SVGSVGElement) || element.querySelector('text, rect')) return false;
		if (element.getAttribute('fill') !== 'currentColor') return false;
		const inherited = getComputedStyle(element).color;
		const paths = [...element.querySelectorAll('path[d]')];
		// These marks are filled monochrome silhouettes inheriting currentColor; the generic cube is a stroked outline.
		return paths.length > 0 && paths.every((path) => {
			const { fill, stroke } = getComputedStyle(path);
			return fill !== 'none' && fill === inherited && stroke === 'none' && path.getTotalLength() > 0;
		});
	});
	expect(isBrandMark, `${label}: expected a filled monochrome currentColor vector mark, not an image, cube, or initial`);
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
			for (const id of builtInBrands) await assertVectorMark(logo(id), `${id} list`);
			log(`Provider list: ${builtInBrands.join(', ')} monochrome vector marks verified`);

			await mark('open Antigravity');
			await row('antigravity').click();
			const detail = page.locator('.st-identity [data-provider-logo="antigravity"]').filter({ visible: true });
			await assertVectorMark(detail, 'antigravity detail');
			log('Antigravity detail: monochrome vector mark verified');
		}
	}]
});
