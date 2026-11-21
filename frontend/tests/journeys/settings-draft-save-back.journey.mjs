// Adding an item from a Settings draft form on a phone (#1895): the saved
// item's page takes the draft's place, so the page's Back and the system Back
// both return to that collection's list (with the new row), and one more Back
// reaches the Settings index. Covers MCP servers, hooks and API users. A save
// the server refuses keeps the draft open. A desktop guard keeps the split
// layout's behaviour: the saved item opens beside the list.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, showSidebar, uniq } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const settingsNav = page => page.getByRole('navigation', { name: 'Settings', exact: true });
const visible = locator => locator.filter({ visible: true }).first();
const button = (page, name) => visible(page.getByRole('button', { name, exact: true }));
const listShown = page => page.evaluate(() => !!document.querySelector('.settings-shell .st-coll .st-lcol')?.checkVisibility());
const listRow = (page, text) => visible(page.locator('.st-lcol [data-settings-item]').filter({ hasText: text }));
// What the stacked Settings page shows, for failure messages.
const where = page => page.evaluate(() => {
	const shell = [...document.querySelectorAll('.settings-shell[data-view]')].find(el => el.checkVisibility());
	const heading = [...document.querySelectorAll('[data-settings-heading]')].find(el => el.checkVisibility());
	return JSON.stringify({ view: shell?.dataset.view ?? null, heading: heading?.textContent?.trim().slice(0, 60) ?? null });
});
const detailTitle =(page, text) => visible(page.locator('[data-settings-heading="detail"]').filter({ hasText: text }));

// Owner-only writes: a preview grant may not make them, so each save is
// answered here and the created item joins the real list from then on.
async function keepCreated(page, url, toItem) {
	const created = [];
	await page.route(url, async route => {
		const request = route.request();
		if (request.method() === 'POST') {
			const item = toItem(request.postDataJSON());
			created.push(item);
			return route.fulfill({ json: item });
		}
		if (request.method() !== 'GET' || !created.length) return route.fallback();
		const response = await route.fetch();
		const json = await response.json();
		return route.fulfill({ response, json: Array.isArray(json) ? [...json, ...created] : json });
	});
}

const STAMP = '2026-11-01T00:00:00Z';
const KINDS = {
	mcp: {
		category: 'MCP servers', add: 'Add server', save: 'Add server', draft: 'Not added yet', idle: 'Select a server, or add one.',
		stub: page => keepCreated(page, /\/api\/v1\/mcp\/servers(\?.*)?$/, body => ({ ...body, scope: 'user', enabled: true })),
		name: () => `jr-${uniq()}`,
		async fill(page, name) {
			await visible(page.getByRole('textbox', { name: 'Name', exact: true })).fill(name);
			await visible(page.getByRole('textbox', { name: 'Command', exact: true })).fill('npx');
		}
	},
	hooks: {
		category: 'Hooks', add: 'Add hook', save: 'Add hook', draft: 'Not added yet',
		stub: page => keepCreated(page, /\/api\/v1\/hooks\/app(\?.*)?$/, body => ({
			id: `hk-${uniq()}`, project_id: null, name: body.name, event: body.event, hook_type: body.hook_type,
			command: body.command ?? null, timeout: body.timeout ?? 30, url: body.url ?? null, has_secret: false,
			enabled: body.enabled ?? true, tool_matcher: body.tool_matcher ?? null,
			created_at: STAMP, updated_at: STAMP, last_triggered_at: null, failure_count: 0
		})),
		name: () => `Journey hook ${uniq()}`,
		async fill(page, name) {
			await visible(page.getByRole('textbox', { name: 'Name', exact: true })).fill(name);
			await visible(page.getByRole('textbox', { name: 'Command', exact: true })).fill('echo journey');
		}
	},
	api: {
		category: 'Integrations', add: 'Add API user', save: 'Create API user', draft: 'Not created yet',
		stub: page => keepCreated(page, /\/api\/v1\/api-users(\?.*)?$/, body => ({
			id: `u-${uniq()}`, name: body.name, description: body.description ?? '', project_id: body.project_id ?? null,
			profile_id: null, model: body.model ?? null, effort: body.effort ?? null, permission_mode: null,
			allowed_agents: [], is_active: true, created_at: STAMP, updated_at: STAMP, last_used_at: null,
			api_key: 'journey-not-a-real-key'
		})),
		name: () => `Journey bot ${uniq()}`,
		async fill(page, name) {
			await visible(page.getByRole('textbox', { name: 'Name', exact: true })).fill(name);
		}
	}
};

async function openCategory(page, status, label) {
	await gotoApp(page, status);
	const nav = (await showSidebar(page)).getByRole('navigation', { name: 'Workspace', exact: true });
	await nav.getByRole('button', { name: 'Settings', exact: true }).click();
	await settingsNav(page).waitFor();
	await visible(settingsNav(page).getByRole('button', { name: label, exact: true })).click();
	await visible(page.getByRole('heading', { name: label, exact: true, level: 1 })).waitFor();
}

// The draft's own action: in the page, or under the phone's More menu.
async function pressSave(page, label) {
	// Not the list's own Add button of the same name.
	const direct = visible(page.locator('.st-dwrap, .phone-detail-actions').getByRole('button', { name: label, exact: true }));
	if (!(await direct.isVisible())) await visible(page.locator('summary[aria-label="More"]')).click();
	await direct.click();
}

async function addItem(page, kind, name) {
	await button(page, kind.add).click();
	await visible(page.locator('[data-settings-heading="detail"]')).waitFor();
	await kind.fill(page, name);
	await pressSave(page, kind.save);
	// The draft already carries the name: saved is when its draft status goes.
	await page.getByText(kind.draft, { exact: true }).filter({ visible: true }).first().waitFor({ state: 'hidden', timeout: 15_000 });
	await detailTitle(page, name).waitFor({ timeout: 15_000 });
}

function phoneCase(key, back) {
	const kind = KINDS[key];
	return {
		name: `phone-${key}-saved-${back}-back`, viewports: ['phone'], throttles: ['fast'],
		async setup({ page, status }) {
			await kind.stub(page);
			await openCategory(page, status, kind.category);
		},
		async act({ page, mark }) {
			const name = kind.name();
			await mark(`add ${name} from the draft form`);
			await addItem(page, kind, name);
			expect(await button(page, `Back to ${kind.category}`).isVisible(), 'the saved item offers Back to its list');

			await mark(`${back} Back from the saved item`);
			if (back === 'page') await button(page, `Back to ${kind.category}`).click();
			else await page.goBack();
			await listRow(page, name).waitFor({ timeout: 10_000 }).catch(() => {});
			if (kind.idle) expect(!(await visible(page.getByText(kind.idle, { exact: true })).isVisible()), `not the idle "${kind.idle}" page`);
			expect(await listShown(page), `Back shows the ${kind.category} list (${await where(page)})`);
			expect(await listRow(page, name).isVisible(), `the list holds ${name}`);

			await mark('one more Back reaches the Settings index');
			if (back === 'page') await button(page, 'Back to Settings').click();
			else await page.goBack();
			await visible(settingsNav(page).getByRole('button', { name: kind.category, exact: true })).waitFor({ timeout: 10_000 });
		}
	};
}

// The service worker would answer the stubbed requests before page.route sees them.
await runJourney({ name: 'settings-draft-save-back', browser: async () => ({ context: { serviceWorkers: 'block' } }), allow: DEV_INSTANCE_NOISE, cases: [
	...['mcp', 'hooks', 'api'].flatMap(key => [phoneCase(key, 'page'), phoneCase(key, 'system')]),
	{
		name: 'phone-mcp-failed-save-keeps-draft', viewports: ['phone'], throttles: ['fast'],
		allow: [{ kind: 'failed_requests', status: 500, url: /\/api\/v1\/mcp\/servers$/, reason: 'the journey refuses this save on purpose' }],
		async setup({ page, status }) {
			await page.route(/\/api\/v1\/mcp\/servers$/, route => (route.request().method() === 'POST'
				? route.fulfill({ status: 500, json: { detail: 'Journey refused this save' } })
				: route.fallback()));
			await openCategory(page, status, KINDS.mcp.category);
		},
		async act({ page, mark }) {
			const name = KINDS.mcp.name();
			await mark('a refused save keeps the draft');
			await button(page, 'Add server').click();
			await KINDS.mcp.fill(page, name);
			await pressSave(page, 'Add server');
			await visible(page.getByText('Journey refused this save')).waitFor({ timeout: 10_000 });
			expect(await visible(page.getByText('Not added yet', { exact: true })).isVisible(), 'the draft is still open');
			expect(await visible(page.getByRole('textbox', { name: 'Name', exact: true })).inputValue() === name, 'the draft keeps its name');

			await mark('Back from the draft reaches the list');
			await button(page, 'Back to MCP servers').click();
			await page.waitForFunction(() => !!document.querySelector('.settings-shell .st-coll .st-lcol')?.checkVisibility(), null, { timeout: 10_000 });
		}
	},
	{
		name: 'desktop-mcp-saved-beside-list', viewports: ['desktop'], throttles: ['fast'],
		async setup({ page, status }) {
			await KINDS.mcp.stub(page);
			await openCategory(page, status, KINDS.mcp.category);
		},
		async act({ page, mark }) {
			const name = KINDS.mcp.name();
			const entries = await page.evaluate(() => history.length);
			await mark('add a server beside the list');
			await addItem(page, KINDS.mcp, name);
			await listRow(page, name).waitFor({ timeout: 10_000 });
			expect(await listShown(page), 'the list stays beside the saved server');
			expect(await page.evaluate(() => history.length) === entries, 'no history entries in the split layout');
		}
	}
] });
