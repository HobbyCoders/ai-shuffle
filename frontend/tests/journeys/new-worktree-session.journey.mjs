// Journey: start a new worktree session from the composer and send its first
// message. The chat surface must only move forward (new → active, with an
// optional loading step); it must never fall back to the centred new/cleared
// composer once the transcript has shown, and nothing may remount.
//
// Owner report this guards: "creating a new worktree session loads to full
// page, then shows the composer back in the middle as if it went back to new
// chat, then loads back to the proper full screen."
//
//   python -m scripts.devloop up            # in another terminal / a Monitor
//   node tests/journeys/new-worktree-session.journey.mjs [--only=auto@phone] [--list]
//
// Entry points covered (every way the composer binds a new worktree session):
//   auto            worktree toggle on, branch Auto (named from the message)
//                   → autoCreateWorktree (POST /sessions/with-worktree + prompt)
//   new-branch      "New branch..." in the branch menu (creates the git branch),
//                   then send → createWorktree (POST without prompt, then query)
//   existing-branch an existing branch from the branch menu → createWorktree
//   main-checkout   control: the plain new-chat send in the main checkout
//                   → WebSocket query path
import { fakeAiTag, runJourney } from './runner.mjs';

const PROJECT = 'Dev sandbox';
const SEEDED_BRANCH = 'feature/second';
const AGENT = 'Dev agent';

let counter = 0;
const uniq = () => `${Date.now().toString(36)}${(counter++).toString(36)}`;

async function openNewChat({ page, status }) {
	await page.goto(status.urls.ui);
	await page.locator('[data-ui-state="chat"][data-ui-surface="new"]').waitFor({ timeout: 90_000 });
	const hero = page.getByRole('region', { name: 'New session' });
	await hero.waitFor();
	// The seed's Dev agent (the preview cookie cannot persist a composer default).
	await page.getByRole('button', { name: /^Model: / }).waitFor({ timeout: 60_000 });
	const agentMenu = page.getByRole('menu', { name: 'Agent selection' });
	await pick(
		hero.getByRole('button', { name: /^Agent:/ }),
		agentMenu,
		agentMenu.getByRole('button', { name: AGENT, exact: true }),
		hero.getByRole('button', { name: `Agent: ${AGENT}` })
	);
	return hero;
}

/**
 * Open `trigger`'s popup and click `item` in it until `done` shows. The app
 * may re-render the hero while it hydrates (stores loading in), which closes a
 * popup opened too early; retry instead of failing on that setup noise.
 */
async function pick(trigger, popup, item, done) {
	for (let attempt = 0; attempt < 5; attempt++) {
		if (await done.isVisible()) return;
		try {
			if (!(await popup.isVisible())) await trigger.click();
			await item.click({ timeout: 5000 });
			await done.waitFor({ timeout: 5000 });
			return;
		} catch {
			/* retry */
		}
	}
	await done.waitFor({ timeout: 1000 });
}

/** Click a trigger until its popup shows (an early click can land before hydration). */
async function openPopup(trigger, popup) {
	for (let attempt = 0; attempt < 3; attempt++) {
		if (await popup.isVisible()) return popup;
		await trigger.click();
		try {
			await popup.waitFor({ timeout: 10_000 });
			return popup;
		} catch {
			/* retry */
		}
	}
	throw new Error('popup never opened');
}

async function pickProject(page, hero) {
	const item = page.getByRole('button', { name: PROJECT, exact: true });
	await pick(hero.getByRole('button', { name: /^No project/ }), item, item, hero.getByRole('button', { name: `Workspace folders: ${PROJECT}` }));
}

async function worktreeOn(page, hero) {
	await hero.getByRole('button', { name: /Switch to a worktree$/ }).click();
	await hero.getByRole('button', { name: /^Worktree branch:/ }).waitFor();
}

async function openBranchMenu(page, hero) {
	return openPopup(hero.getByRole('button', { name: /^Worktree branch:/ }), page.getByRole('dialog', { name: 'Worktree branch' }));
}

/** Type the first message, send it and wait for the fake AI's reply to settle. */
async function sendFirstMessage({ page, variant, mark, log }, label) {
	const text = `${label} journey ${uniq()} ${fakeAiTag(variant)}`.trim();
	const box = page.getByRole('textbox', { name: 'Message' });
	await mark('type the first message');
	await box.click();
	await box.fill(text);
	await mark('send');
	const t0 = Date.now();
	await page.getByRole('button', { name: 'Send', exact: true }).click();
	// Settled: a bound session showing its transcript (the user's message and
	// the reply), no longer streaming, with the fake AI's text on screen.
	const chat = page.locator(
		'[data-ui-state="chat"][data-ui-surface="active"][data-ui-streaming="false"]:not([data-ui-session="none"])'
	);
	await page.getByText('Fake AI here', { exact: false }).first().waitFor({ timeout: 60_000 });
	await chat.waitFor({ timeout: 30_000 });
	await page.waitForFunction(
		() => Number(document.querySelector('[data-ui-state="chat"]')?.getAttribute('data-ui-messages') || 0) >= 2,
		null,
		{ timeout: 30_000 }
	);
	log(`reply settled ${Date.now() - t0}ms after send`);
	// Hold a moment: a late fold or a feed restart is exactly the kind of thing
	// that sends the surface backwards after it first looked done.
	await page.waitForTimeout(1500);
}

/**
 * Let the staged composer settle before recording, the way a user's typing
 * would: the picks start their own loads (branches, git status of the staged
 * branch) and those belong to setup, not to the send the step judges.
 */
const settled = (setup) => async (ctx) => {
	await setup(ctx);
	await ctx.page.waitForTimeout(1000);
};

await runJourney({
	name: 'new-worktree-session',
	cases: [
		{
			name: 'auto',
			setup: settled(async (ctx) => {
				const hero = await openNewChat(ctx);
				await pickProject(ctx.page, hero);
				await worktreeOn(ctx.page, hero);
				await hero.getByRole('button', { name: /^Worktree branch: a new branch from/ }).waitFor();
			}),
			act: (ctx) => sendFirstMessage(ctx, 'auto')
		},
		{
			name: 'new-branch',
			setup: settled(async (ctx) => {
				const { page } = ctx;
				const hero = await openNewChat(ctx);
				await pickProject(page, hero);
				await worktreeOn(page, hero);
				const menu = await openBranchMenu(page, hero);
				await menu.getByRole('button', { name: /New branch/ }).click();
				const branch = `journey/${uniq()}`;
				await page.getByPlaceholder('Enter a branch name').fill(branch);
				await page.getByRole('button', { name: 'Continue', exact: true }).click();
				await hero.getByRole('button', { name: `Worktree branch: ${branch}` }).waitFor({ timeout: 20_000 });
			}),
			act: (ctx) => sendFirstMessage(ctx, 'new-branch')
		},
		{
			name: 'existing-branch',
			setup: settled(async (ctx) => {
				const { page } = ctx;
				const hero = await openNewChat(ctx);
				await pickProject(page, hero);
				await worktreeOn(page, hero);
				const menu = await openBranchMenu(page, hero);
				await menu.getByRole('button', { name: SEEDED_BRANCH, exact: true }).click();
				await hero.getByRole('button', { name: `Worktree branch: ${SEEDED_BRANCH}` }).waitFor({ timeout: 20_000 });
			}),
			act: (ctx) => sendFirstMessage(ctx, 'existing-branch')
		},
		{
			name: 'main-checkout',
			setup: settled(async (ctx) => {
				const hero = await openNewChat(ctx);
				await pickProject(ctx.page, hero);
			}),
			act: (ctx) => sendFirstMessage(ctx, 'main-checkout')
		}
	],
	allow: [
		{
			kind: 'failed_requests',
			status: 403,
			url: /\/preferences(\/|$)/,
			reason: 'the preview cookie may not write preferences (devloop safety rule)'
		},
		{
			kind: 'failed_requests',
			status: 403,
			url: /\/model-providers\/refresh-capabilities(\?|$)/,
			reason: 'the preview cookie may not change providers (devloop safety rule)'
		},
		{
			kind: 'failed_requests',
			status: 502,
			url: /\/git\/fetch(\?|$)/,
			reason: 'background fetch: the seeded repository has no remote to fetch from'
		},
		{
			kind: 'failed_requests',
			status: 409,
			url: /\/git\/(fetch|status)(\?|$)/,
			reason:
				'known bug #803: the git header asks for a brand-new worktree chat\'s Git state before its checkout exists; a background request, nothing on screen'
		},
		{
			kind: 'errors',
			message: /Failed to load git repository: No matching Git checkout exists/,
			reason: 'known bug #803: the same early git/status request, logged as a console error'
		},
		{
			kind: 'failed_requests',
			status: 401,
			url: /\/github\/pulls(\?|$)/,
			reason: 'the dev instance has no GitHub sign-in'
		}
	]
});
