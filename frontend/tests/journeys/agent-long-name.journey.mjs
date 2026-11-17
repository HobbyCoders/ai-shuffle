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
	}, {
		name: 'failed-save-stays-in-header-until-success',
		viewports: ['desktop', 'phone'],
		allow: [{ kind: 'failed_requests', status: 422, url: /\/api\/v1\/agents$/, reason: 'intentional one-shot rejection of the draft create in this case' }],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await showSidebar(page);
			await workspace(page).getByRole('button', { name: 'Agents', exact: true }).click();
			await page.getByRole('button', { name: 'New agent', exact: true }).filter({ visible: true }).first().click();
			await page.getByRole('dialog', { name: 'New agent' }).getByRole('button', { name: 'Create', exact: true }).click();
		},
		async act({ page, mark }) {
			await mark('show the reason for a rejected create in the header');
			await page.route('**/api/v1/agents', (route) => route.request().method() === 'POST'
				? route.fulfill({ status: 422, json: { detail: 'Test: rejected agent save' } })
				: route.fallback());
			const field = page.getByRole('textbox', { name: 'Name', exact: true });
			const key = uniq().slice(-5);
			const candidate = `Try ${key}`;
			const corrected = `Fix ${key}`;
			await field.fill(candidate);
			const status = page.getByRole('status').filter({ hasText: /^Save failed: Test: rejected agent save$/ });
			await status.waitFor();
			await page.unroute('**/api/v1/agents');

			await mark('save corrected draft through API and clear the error');
			await field.fill(corrected);
			await page.getByRole('status').filter({ hasText: /^Saved$/ }).waitFor();
			await status.waitFor({ state: 'hidden' });
			await waitAgents(page, corrected, 1);
		},
		verify: () => {}
	}],
	allow: [...DEV_INSTANCE_NOISE]
});
