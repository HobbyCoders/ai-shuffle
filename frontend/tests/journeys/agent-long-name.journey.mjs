// Journey: an agent whose name is one long unbroken word, at the 100-character
// name limit. It saves from the Agents page and duplicates (#1616: the id built
// from the name went past the server's 50-character id limit and every save
// was a 422), and the name ends in an ellipsis instead of painting past its row
// in the Subagents tab and the new-chat agent word (#1617).
//
// Desktop only: #1617 is the desktop layout (the phone row already truncates),
// and #1616 does not depend on the viewport.
//
// The two agents stay on the instance: the preview cookie may create and edit
// agents on a throwaway instance but never delete one (owner-only by design,
// tests/api/test_preview_project_management.py).
import { runJourney } from './runner.mjs';
import { api, expect, gotoApp, showSidebar, uniq, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const NAME_MAX = 100;
const ID_MAX = 50;

let name;
let created = [];

const workspace = (page) => page.getByRole('navigation', { name: 'Workspace', exact: true });
/** The agent and its copy share this start (the copy's name is cut to fit " copy"). */
const stem = () => name.slice(0, 40);

async function agentsNamed(page, prefix) {
	const all = await api(page, 'GET', '/agents');
	return all.filter((a) => a.name.startsWith(prefix));
}

async function waitAgents(page, prefix, count) {
	for (let i = 0; i < 60; i += 1) {
		const found = await agentsNamed(page, prefix);
		if (found.length >= count) return found;
		await page.waitForTimeout(500);
	}
	throw new Error(`expected ${count} saved agent(s) named ${prefix}…`);
}

/** Matches ExecutionModeBar's --branch-slot-max: the agent word is no wider than the branch word. */
const CHIP_MAX = 232;

let chip;
let row;

async function steps(page, mark) {
	await mark('name a new agent with a 100-character unbroken word');
	await workspace(page).getByRole('button', { name: 'Agents', exact: true }).click();
	await page.getByRole('button', { name: 'New agent', exact: true }).filter({ visible: true }).first().click();
	await page.getByRole('dialog', { name: 'New agent' }).getByRole('button', { name: 'Create', exact: true }).click();
	await page.getByRole('textbox', { name: 'Name', exact: true }).fill(name);
	created = await waitAgents(page, name, 1);

	await mark('duplicate it');
	await page.getByRole('heading', { level: 2, name, exact: true }).waitFor();
	let creates = 0;
	const countCreate = (request) => {
		if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/agents') creates++;
	};
	page.on('request', countCreate);
	try {
		// Both clicks can arrive before a response (or even a UI render).
		await page.getByRole('button', { name: 'Duplicate', exact: true }).evaluate((button) => {
			button.click();
			button.click();
		});
		await page.getByRole('heading', { level: 2, name: / copy$/ }).waitFor();
		expect(creates === 1, `exactly one create for two rapid clicks: ${creates}`);
	} finally {
		page.off('request', countCreate);
	}
	created = await waitAgents(page, stem(), 2);
	const copyName = created.find((a) => a.name !== name)?.name;

	await mark('pick the agent for a new chat');
	await workspace(page).getByRole('button', { name: 'New chat', exact: true }).click();
	await waitChat(page, { surface: 'new' }, { timeout: 60_000 });
	const hero = page.getByRole('region', { name: 'New chat' });
	await hero.getByRole('button', { name: /^Agent:/ }).click();
	await page.getByRole('menu', { name: 'Agent selection' }).getByRole('button', { name, exact: true }).click();
	await hero.getByRole('button', { name: `Agent: ${name}` }).waitFor();
	chip = await hero.evaluate((region) => {
		const word = region.querySelector('.card-name');
		const label = word.querySelector('.card-name-text');
		const next = region.querySelector('[role=group] button');
		const r = region.getBoundingClientRect();
		const w = word.getBoundingClientRect();
		return {
			clipped: label.scrollWidth > label.clientWidth && getComputedStyle(label).textOverflow === 'ellipsis',
			inside: w.right <= r.right + 0.5,
			oneLine: Math.abs(next.getBoundingClientRect().top - w.top) < 1,
			width: Math.round(w.width),
			row: Math.round(r.width)
		};
	});

	await mark("open the copy's Subagents tab");
	await workspace(page).getByRole('button', { name: 'Agents', exact: true }).click();
	await page.locator('[data-settings-item]').filter({ hasText: copyName }).first().click();
	await page.getByRole('tab', { name: /^Subagents/ }).click();
	const toggle = page.getByRole('switch', { name: `Allow ${name}`, exact: true });
	await toggle.waitFor();
	row = await toggle.evaluate((sw) => {
		const nm = sw.closest('.ra').querySelector('.nm');
		return {
			clipped: nm.scrollWidth > nm.clientWidth && getComputedStyle(nm).textOverflow === 'ellipsis',
			beforeSwitch: nm.getBoundingClientRect().right <= sw.getBoundingClientRect().left
		};
	});
}

async function check(page, log) {
	// #1616: both agents saved, each id within the server's limit and distinct.
	const ids = created.map((a) => a.id);
	expect(created.length === 2, `the agent and its copy saved: ${created.length}`);
	expect(ids.every((id) => id.length <= ID_MAX), `ids fit ${ID_MAX} characters: ${ids.join(', ')}`);
	expect(new Set(ids).size === 2, `ids stay unique after the cut: ${ids.join(', ')}`);
	const copy = created.find((a) => a.name !== name);
	expect(copy.name.length <= NAME_MAX && copy.name.endsWith(' copy'), `the copy's name fits ${NAME_MAX}: ${copy.name.length}`);

	// #1617: the new-chat agent word ends in an ellipsis, no wider than the
	// branch word, and keeps the row on one line.
	expect(chip.clipped && chip.inside && chip.oneLine, `new-chat agent word truncates inside its row: ${JSON.stringify(chip)}`);
	expect(chip.width <= CHIP_MAX, `new-chat agent word fits ${CHIP_MAX}px: ${chip.width}px`);

	// #1617: in the copy's Subagents tab, the original's name stops before its switch.
	expect(row.clipped && row.beforeSwitch, `Subagents row name truncates before its switch: ${JSON.stringify(row)}`);
	log(`saved ${ids.join(' and ')}; new-chat word ${chip.width}px in a ${chip.row}px row`);
}

await runJourney({
	name: 'agent-long-name',
	viewports: ['desktop'],
	// A slower CPU does not change whether the id fits or the name truncates.
	throttles: ['fast'],
	cases: [{
		name: 'long-name-saves-and-truncates',
		async setup({ page, status }) {
			// Lowercase "zz" first: the list sorts by name, so these stay below the
			// seeded agents other journeys open with the first row.
			const head = `zzunbroken${uniq()}`;
			name = head + 'w'.repeat(NAME_MAX - head.length);
			created = [];
			await gotoApp(page, status);
		},
		act: ({ page, mark }) => steps(page, mark),
		verify: ({ page, log }) => check(page, log)
	}, ...['phone', 'desktop'].map((viewport) => ({
		name: viewport === 'phone' ? 'duplicate-after-back-keeps-phone-list-open' : 'duplicate-after-leaving-keeps-source-selected',
		viewports: [viewport],
		async setup({ page, status }) { await gotoApp(page, status); },
		async act({ page, mark }) {
			const key = uniq().slice(-5);
			const source = `Source ${key}`;
			await api(page, 'POST', '/agents', { id: `back-test-${key}`, name: source, config: {} });
			await showSidebar(page);
			await workspace(page).getByRole('button', { name: 'Agents', exact: true }).click();
			await page.locator('[data-settings-item]').filter({ hasText: source }).click();
			const path = '**/api/v1/agents';
			const held = Promise.withResolvers();
			const release = Promise.withResolvers();
			await page.route(path, async (route) => {
				if (route.request().method() !== 'POST') return route.fallback();
				const result = await route.fetch();
				held.resolve(result.status());
				await release.promise;
				await route.fulfill({ response: result });
			});
			try {
				await mark('duplicate and hold its actual response');
				if (viewport === 'phone') {
					await page.locator('summary[aria-label="More"]').filter({ visible: true }).click({ timeout: 5_000 });
				}
				await page.getByRole('button', { name: 'Duplicate', exact: true }).click();
				const back = page.getByRole('button', { name: 'Back to Agents', exact: true });
				const projects = page.locator('section.view-panel[aria-label="Projects"]').filter({ visible: true });
				await mark('leave the agent detail before letting the response reach the editor');
				if (viewport === 'phone') await back.click();
				else {
					await workspace(page).getByRole('button', { name: 'Projects', exact: true }).click();
					await projects.waitFor({ timeout: 5_000 });
				}
				expect(await held.promise === 201, 'the one real create succeeded');
				// Wait for the success notification request: selection, if any, precedes it.
				const completion = page.waitForRequest((request) => request.method() === 'POST' &&
					new URL(request.url()).pathname === '/api/v1/notifications' &&
					(request.postData() ?? '').includes(`Duplicated as ${source} copy.`), { timeout: 5_000 });
				release.resolve();
				await completion;
				if (viewport === 'phone') {
					expect(!(await back.isVisible()), 'a finished duplicate does not reopen detail');
				} else {
					expect(await projects.isVisible(), 'the duplicate does not pull focus back to Agents');
					await workspace(page).getByRole('button', { name: 'Agents', exact: true }).click();
					const name = page.getByRole('textbox', { name: 'Name', exact: true });
					expect(await name.inputValue() === source, 'the original selection survives leaving and returning');
				}
				const rows = await agentsNamed(page, source);
				expect(rows.length === 2 && rows.some(({ name }) => name === `${source} copy`), 'source and exactly one copy persisted');
			} finally {
				release.resolve();
				await page.unroute(path);
			}
		},
		verify: () => {}
	}))],
	allow: [...DEV_INSTANCE_NOISE]
});
