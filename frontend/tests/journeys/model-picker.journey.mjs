// Journey: the model picker panel opens where every row can be seen and
// reached, takes the keyboard at once, and does not stay open behind a hidden
// Settings.
//
// - #1338: on the API user form the panel grew left past the Settings detail
//   pane and its rows were cut off. Every row must sit inside the pane and be
//   the element under its own centre.
// - #1337: that form offered "Custom…" with nowhere to type a model id.
// - #1339: Down and Up did nothing right after opening. With a mouse and
//   keyboard (desktop) opening focuses the search box; with touch (phone, an
//   emulated coarse pointer) focus stays put so no on-screen keyboard opens.
//   Escape hands focus back to whatever opened the panel.
// - #1345: leaving Settings with the keyboard left the picker open in the
//   hidden page, and it was still open on return.
// - #1407: Tab or Shift+Tab out of an open picker left it open over the page.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, isPhone, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const SEARCH = 'Search models';

async function openSettings(page, variant) {
	if (isPhone(variant)) await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
}

/** Settings > Integrations > Add API user > Run: the form with the Model picker. */
async function openApiUserForm(page, variant) {
	await openSettings(page, variant);
	await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Integrations', exact: true }).click();
	await page.getByRole('button', { name: 'Add API user', exact: true }).first().click();
	await page.getByRole('tab', { name: 'Run', exact: true }).click();
	await page.locator('#api-user-model').waitFor();
}

/** Click the trigger and wait for the panel's list. */
async function openPicker(page, trigger) {
	await trigger.click();
	await page.locator('.mpk-panel [role="listbox"]').waitFor();
	// The panel measures again on the next tick, after the row settles.
	await page.waitForTimeout(300);
}

/** What has focus: its accessible name, id and tag. */
function focused(page) {
	return page.evaluate(() => {
		const el = document.activeElement;
		return { label: el?.getAttribute('aria-label') ?? '', id: el?.id ?? '', tag: el?.tagName ?? '' };
	});
}

function highlightedRow(page) {
	return page.evaluate(() => document.querySelector('[data-model-picker-panel] .mpp-row.highlighted')?.textContent?.trim() ?? null);
}

/** Down, Down, Up from the search box: each press moves the highlight. */
async function arrowsMoveHighlight(page) {
	await page.keyboard.press('ArrowDown');
	const first = await highlightedRow(page);
	expect(first !== null, 'ArrowDown right after opening highlights a row');
	await page.keyboard.press('ArrowDown');
	const second = await highlightedRow(page);
	expect(second !== null && second !== first, `a second ArrowDown moves on: ${first} -> ${second}`);
	await page.keyboard.press('ArrowUp');
	const back = await highlightedRow(page);
	expect(back === first, `ArrowUp moves back: ${second} -> ${back}`);
}

/**
 * From the focused search box, Shift+Tab once lands on the trigger with the
 * panel still open; once more leaves the picker, which closes it and leaves
 * focus on the control Shift+Tab moved to (#1407).
 */
async function shiftTabOut(page, trigger, panelSelector, what) {
	await page.keyboard.press('Shift+Tab');
	expect((await trigger.getAttribute('aria-expanded')) === 'true', `${what}: Shift+Tab to the trigger keeps it open`);
	await page.keyboard.press('Shift+Tab');
	await page.locator(panelSelector).waitFor({ state: 'detached', timeout: 5000 });
	expect((await trigger.getAttribute('aria-expanded')) === 'false', `${what}: the trigger reads closed`);
	const now = await focused(page);
	const onTrigger = await trigger.evaluate((el) => el === document.activeElement);
	expect(now.tag !== 'BODY' && !onTrigger, `${what}: focus stays where Shift+Tab sent it: ${JSON.stringify(now)}`);
}

/**
 * Bring each option row into view (the list scrolls) and check its whole box
 * against the detail pane and the window, and that the row is what a click on
 * its centre would hit.
 */
function rowsAgainstPane(page) {
	return page.evaluate(async () => {
		const trigger = document.querySelector('#api-user-model');
		const pane = trigger.closest('.st-dpane');
		const rows = [...document.querySelectorAll('.mpk-panel [role="option"]')];
		const out = [];
		for (const row of rows) {
			row.scrollIntoView({ block: 'nearest' });
			await new Promise((r) => requestAnimationFrame(() => r()));
			const p = pane.getBoundingClientRect();
			const b = row.getBoundingClientRect();
			const inside =
				b.left >= p.left - 0.5 && b.right <= p.right + 0.5 && b.top >= p.top - 0.5 && b.bottom <= p.bottom + 0.5 &&
				b.left >= 0 && b.right <= innerWidth && b.top >= 0 && b.bottom <= innerHeight;
			const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
			out.push({
				label: row.textContent.trim().slice(0, 30),
				row: [Math.round(b.left), Math.round(b.right), Math.round(b.top), Math.round(b.bottom)],
				pane: [Math.round(p.left), Math.round(p.right), Math.round(p.top), Math.round(p.bottom)],
				inside,
				hit: !!hit && row.contains(hit)
			});
		}
		return out;
	});
}

await runJourney({
	name: 'model-picker',
	// Where the panel opens and what has focus do not depend on CPU speed.
	throttles: ['fast'],
	cases: [
		{
			name: 'api-user-rows-inside-pane',
			async setup({ page, status, variant }) {
				// The width the issue was measured at; the phone variant is 390 wide.
				if (!isPhone(variant)) await page.setViewportSize({ width: 1280, height: 800 });
				await gotoApp(page, status);
				await openApiUserForm(page, variant);
			},
			async act({ page, mark, variant, log }) {
				const trigger = page.locator('#api-user-model');
				await mark('open the API user Model picker');
				await openPicker(page, trigger);

				const rows = await rowsAgainstPane(page);
				expect(rows.length >= 2, `the picker lists models: ${rows.length}`);
				const outside = rows.filter((r) => !r.inside || !r.hit);
				expect(outside.length === 0, `every row is inside the pane and clickable: ${JSON.stringify(outside)}`);
				expect(!rows.some((r) => /Custom/.test(r.label)), 'no "Custom…" row with nowhere to type the id');
				log(`${rows.length} rows inside pane ${JSON.stringify(rows[0].pane)}, first row ${JSON.stringify(rows[0].row)}`);

				const now = await focused(page);
				if (isPhone(variant)) {
					expect(now.id === 'api-user-model', `touch: focus stays on the trigger, no on-screen keyboard: ${JSON.stringify(now)}`);
				} else {
					expect(now.label === SEARCH, `mouse and keyboard: the search box has focus: ${JSON.stringify(now)}`);
					await mark('arrow keys right after opening');
					await arrowsMoveHighlight(page);
				}

				await mark('Escape closes the picker');
				await page.keyboard.press('Escape');
				await page.locator('.mpk-panel').waitFor({ state: 'detached' });
				expect((await trigger.getAttribute('aria-expanded')) === 'false', 'the trigger reads closed');
				const after = await focused(page);
				expect(after.id === 'api-user-model', `Escape returns focus to the trigger: ${JSON.stringify(after)}`);
				// Escape closed only the picker, not the form around it.
				expect(await trigger.isVisible(), 'the API user form is still open');
			}
		},
		{
			name: 'closes-when-settings-is-left-by-keyboard',
			// "Back to chats" leaves Settings in place at desktop width.
			viewports: ['desktop'],
			async setup({ page, status, variant }) {
				await gotoApp(page, status);
				await openApiUserForm(page, variant);
			},
			async act({ page, mark, variant }) {
				const trigger = page.locator('#api-user-model');
				await mark('open the picker');
				await openPicker(page, trigger);

				await mark('leave Settings with the keyboard');
				await page.getByRole('button', { name: 'Back to chats', exact: true }).focus();
				await page.keyboard.press('Enter');
				await page.getByRole('button', { name: /^Model: / }).first().waitFor();
				await page.waitForFunction(() => document.querySelector('#api-user-model')?.getAttribute('aria-expanded') === 'false', null, { timeout: 5000 });
				expect((await page.locator('.mpk-panel').count()) === 0, 'no panel left in the hidden Settings');

				await mark('come back to Settings');
				await openSettings(page, variant);
				await trigger.waitFor();
				expect((await trigger.getAttribute('aria-expanded')) === 'false', 'the picker is closed on return');
				expect((await page.locator('.mpk-panel').count()) === 0, 'no panel on return');
			}
		},
		{
			name: 'closes-when-focus-tabs-out',
			// Opening puts focus in the search box only with a mouse and keyboard.
			viewports: ['desktop'],
			async setup({ page, status, variant }) {
				await page.setViewportSize({ width: 1280, height: 800 });
				await gotoApp(page, status);
				await openApiUserForm(page, variant);
			},
			async act({ page, mark }) {
				const trigger = page.locator('#api-user-model');
				await mark('open the API user Model picker');
				await openPicker(page, trigger);
				await shiftTabOut(page, trigger, '.mpk-panel', 'the Settings picker');

				await mark('open the composer Model chip');
				await page.getByRole('button', { name: 'Back to chats', exact: true }).click();
				const chip = page.getByRole('button', { name: /^Model: / }).first();
				await chip.click();
				await page.getByRole('textbox', { name: SEARCH }).waitFor();
				await page.waitForTimeout(300);
				await shiftTabOut(page, chip, '.model-menu', 'the composer Model list');
			}
		},
		{
			name: 'composer-model-chip',
			async setup({ page, status }) {
				await gotoApp(page, status);
			},
			async act({ page, mark, variant }) {
				const chip = page.getByRole('button', { name: /^Model: / }).first();
				await mark('open the composer Model chip');
				await chip.click();
				const search = page.getByRole('textbox', { name: SEARCH });
				await search.waitFor();
				await page.waitForTimeout(300);

				const now = await focused(page);
				if (isPhone(variant)) {
					expect(now.label !== SEARCH && now.tag !== 'INPUT', `touch: no text field takes focus: ${JSON.stringify(now)}`);
				} else {
					expect(now.label === SEARCH, `mouse and keyboard: the search box has focus: ${JSON.stringify(now)}`);
					await mark('arrow keys right after opening');
					await arrowsMoveHighlight(page);
				}

				await mark('Escape closes the model list');
				await page.keyboard.press('Escape');
				await search.waitFor({ state: 'hidden' });
				const after = await focused(page);
				expect(/^Model: /.test(after.label), `Escape returns focus to the Model control: ${JSON.stringify(after)}`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
