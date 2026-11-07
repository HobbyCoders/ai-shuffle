// Journey: cross-worker project sync. The Projects page shows which workers
// hold each project and sets it up on the ones that lack it; a new chat on a
// worker without the project sets it up there before it sends.
//
// The dev instance is one worker, so the other workers and their states are
// simulated in the page (`simulateWorkers`): the installs list gains three
// peers (Studio, pop-os, an offline Laptop) and /projects/overview,
// /projects/workers, /projects/setup and its cancel answer from an in-page
// model that moves a setup forward on every read. In the page, not
// page.route: the built app's service worker makes the requests page.route
// would see (see withoutSavedAgentPick in lib/app.mjs).
//
// Cases:
//   fail-open           an older worker (the overview answers 404): the plain
//                       list it always was, no chips, no filter.
//   chip-sets-up        the dashed pop-os chip on "Dev sandbox" starts a setup
//                       there; the chip spins, then reads ready.
//   remote-only-detail  "couch-cash" is on other workers only: still listed,
//                       opens on the Workers tab with the local tabs disabled;
//                       "Set up on all missing" sets it up here.
//   filter-before-overview  the overview answers late (it asks every online
//                       peer): the worker filter is already above the list
//                       when the Projects page shows, so the list does not
//                       jump down when the overview lands (#1210).
//   filter-before-registry, filter-space-unsupported, remote-only-bootstrap,
//   registry-fails-then-overview, revisit-after-failed-overview,
//   stalled-requests, late-long-worker-name
//                       the same rule through a slow or failing registry, an
//                       older worker, a worker with no projects of its own, a
//                       revisit and stalled requests (#1210 review).
//   new-chat-fails      a new chat on pop-os: the Runs-on row says "not set up
//                       · set up" (selectable), offline Laptop is blocked; the
//                       callout shows, Send reads "Set up & send", the setup
//                       fails and the reason shows with the draft kept.
import { runJourney } from './runner.mjs';
import { PROJECT, expect, gotoApp, holdBootLoad, isPhone, openNewChat, showSidebar, uniq, waitChat, withChatSetup } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

/**
 * Simulate three peer workers and the projects overview. Call before the
 * page loads. `failOn` names `<project_v2_id>:<install_id>` setups that fail.
 * `holdOverview` keeps every /projects/overview answer back until the case
 * calls `window.__releaseOverview()`, like a peer that is slow to answer.
 * `holdInstalls` does the same for the install registry until
 * `window.__releaseInstalls()` (a cold registry). `overviewStatus` answers the
 * overview with that error status while the peers stay in the registry (an
 * older worker that has approved peers). `noLocalProjects` leaves this
 * worker with no projects of its own, so every project is on a peer.
 * `installsStatus` answers the install registry with that error status.
 * `overviewFailFirst` answers the first overview request with a 500 at once
 * and holds the later ones (with `holdOverview`), like a peer that was down
 * on the first visit. `selfName` is this worker's name in the overview.
 * `holdDeadline` hands the Projects page's filter deadline to the case, which
 * passes it with `window.__passFilterDeadline()` (startFilterDeadline), so a
 * slow runner cannot reach it before the case measures.
 */
async function simulateWorkers(page, { overview404 = false, failOn = {}, holdOverview = false, holdInstalls = false, overviewStatus = 0, noLocalProjects = false, installsStatus = 0, overviewFailFirst = false, selfName = 'This worker', holdDeadline = false } = {}) {
	await page.addInitScript(
		({ overview404, failOn, holdOverview, holdInstalls, overviewStatus, noLocalProjects, installsStatus, overviewFailFirst, selfName, holdDeadline }) => {
			if (holdDeadline) {
				const passes = [];
				window.__projectsFilterDeadline = (pass) => passes.push(pass);
				window.__passFilterDeadline = () => { for (const pass of passes.splice(0)) pass(); };
			}
			const original = window.fetch.bind(window);
			const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
			const pathOf = (input) => {
				try {
					return new URL(typeof input === 'string' ? input : input.url, location.href);
				} catch {
					return null;
				}
			};
			if (overview404) {
				window.fetch = async (input, init) => {
					const url = pathOf(input);
					if (url?.pathname === '/api/v1/projects/overview') return json({ detail: 'Not Found' }, 404);
					return original(input, init);
				};
				return;
			}
			let selfId = null;
			const peers = [
				{ install_id: 'studio', device_name: 'Studio', online: true },
				{ install_id: 'pop-os', device_name: 'pop-os', online: true },
				{ install_id: 'laptop', device_name: 'Laptop', online: false }
			];
			const workers = () => [
				{ install_id: selfId, device_name: selfName, is_self: true, online: true, approved: true },
				...peers.map((p) => ({ ...p, is_self: false, approved: true }))
			];
			const P = [
				{ id: 'pv2-sandbox', local: 'dev-sandbox', name: 'Dev sandbox', kind: 'git', remote: 'github.com/example/dev-sandbox',
					st: { self: 'ready', studio: 'ready', 'pop-os': 'missing', laptop: 'offline_missing' } },
				{ id: 'pv2-couch', local: null, name: 'couch-cash', kind: 'git', remote: 'github.com/example/couch-cash',
					st: { self: 'missing', studio: 'ready', 'pop-os': 'failed', laptop: 'offline_has' } }
			];
			if (noLocalProjects) for (const p of P) { p.local = null; p.st.self = 'missing'; }
			const setup = { 'pv2-couch:pop-os': { op_id: 'op-0', status: 'error', progress: null, error_code: 'network', error: "pop-os couldn't reach the repository." } };
			let ops = 0;
			window.__projectSetups = [];
			const overviewGate = new Promise((resolve) => { window.__releaseOverview = resolve; });
			const installsGate = new Promise((resolve) => { window.__releaseInstalls = resolve; });
			const key = (id) => (id === selfId ? 'self' : id);
			const advance = () => {
				for (const p of P) for (const id of Object.keys(p.st)) {
					const s = setup[`${p.id}:${id}`];
					if (!s || p.st[id] !== 'setting_up') continue;
					if (s.status === 'queued') { s.status = 'running'; s.progress = 0; continue; }
					const fail = failOn[`${p.id}:${id === 'self' ? 'self' : id}`];
					if (fail) { p.st[id] = 'failed'; Object.assign(s, { status: 'error', progress: null, error_code: fail[0], error: fail[1] }); continue; }
					s.progress = Math.min(100, s.progress + 50);
					if (s.progress >= 100) { p.st[id] = 'ready'; s.status = 'done'; }
				}
			};
			const row = (p, w, detail) => {
				const state = p.st[key(w.install_id)];
				const out = { ...w, state, setup: setup[`${p.id}:${key(w.install_id)}`] ?? null };
				if (detail) Object.assign(out, {
					folder: state === 'ready' || state === 'offline_has' ? `/workspace/${p.name}` : null,
					suggested_folder: `/workspace/${p.name}`, branch: state === 'ready' ? 'main' : null,
					dirty: false, changed_files: 0, last_seen: new Date().toISOString()
				});
				return out;
			};
			window.fetch = async (input, init) => {
				const url = pathOf(input);
				const path = url?.pathname ?? '';
				const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
				if (noLocalProjects && path === '/api/v1/projects' && method === 'GET') return json([]);
				if (path === '/api/v1/cluster/installs' && method === 'GET') {
					if (holdInstalls) await installsGate;
					window.__installsAnswers = (window.__installsAnswers ?? 0) + 1;
					if (installsStatus) return json({ detail: 'Internal Server Error' }, installsStatus);
					const res = await original(input, init);
					const body = await res.clone().json().catch(() => null);
					const self = body?.installs?.find((i) => i.is_self);
					if (!self) return res;
					selfId = self.id;
					return json({
						...body,
						installs: [self, ...peers.map((p) => ({ ...self, id: p.install_id, device_id: p.install_id, legacy_instance_id: null, device_name: p.device_name, is_self: false, online: p.online, approved: true }))]
					});
				}
				if (!selfId && path.startsWith('/api/v1/projects/')) {
					// The overview names this worker by its install id: learn it first.
					const res = await original('/api/v1/cluster/installs', { credentials: 'include' }).catch(() => null);
					const body = await res?.json().catch(() => null);
					selfId = body?.installs?.find((i) => i.is_self)?.id ?? 'self-install';
				}
				if (path === '/api/v1/projects/overview' && method === 'GET') {
					if (overviewFailFirst && !window.__overviewFailed) {
						window.__overviewFailed = true;
						window.__overviewAnswers = (window.__overviewAnswers ?? 0) + 1;
						return json({ detail: 'Internal Server Error' }, 500);
					}
					if (holdOverview) await overviewGate;
					window.__overviewAnswers = (window.__overviewAnswers ?? 0) + 1;
					if (overviewStatus) return json({ detail: 'Not Found' }, overviewStatus);
					advance();
					return json({ projects: P.map((p) => ({ project_v2_id: p.id, local_project_id: p.local, name: p.name, kind: p.kind, remote: p.remote, default_branch: 'main', session_count: 1, workers: workers().map((w) => row(p, w, false)) })) });
				}
				if (path === '/api/v1/projects/workers' && method === 'GET') {
					advance();
					const id = url.searchParams.get('project_v2_id');
					const lid = url.searchParams.get('project_id');
					const p = P.find((x) => x.id === id || (lid && x.local === lid));
					if (!p) return json({ detail: 'Project not found' }, 404);
					return json({ project_v2_id: p.id, name: p.name, kind: p.kind, remote: p.remote, default_folder: p.name, workers: workers().map((w) => row(p, w, true)) });
				}
				if (path === '/api/v1/projects/setup' && method === 'POST') {
					const body = JSON.parse(init.body);
					window.__projectSetups.push(body);
					const p = P.find((x) => x.id === body.project_v2_id || (body.project_id && x.local === body.project_id));
					const k = key(body.install_id);
					const op = `op-${++ops}`;
					if (!p) return json({ op_id: op, install_id: body.install_id, status: 'queued' }, 202);
					const peer = peers.find((x) => x.install_id === body.install_id);
					if (peer && !peer.online) {
						p.st[k] = 'queued_offline';
						setup[`${p.id}:${k}`] = { op_id: op, status: 'queued_offline', progress: null };
						return json({ op_id: op, install_id: body.install_id, status: 'queued_offline' }, 202);
					}
					p.st[k] = 'setting_up';
					setup[`${p.id}:${k}`] = { op_id: op, status: 'queued', progress: null };
					return json({ op_id: op, install_id: body.install_id, status: 'queued' }, 202);
				}
				if (/^\/api\/v1\/projects\/setup\/[^/]+\/cancel$/.test(path) && method === 'POST') return json({ status: 'cancelled' });
				return original(input, init);
			};
		},
		{ overview404, failOn, holdOverview, holdInstalls, overviewStatus, noLocalProjects, installsStatus, overviewFailFirst, selfName, holdDeadline }
	);
}

/** Open the Projects page from the workspace rail (the drawer on a phone). */
async function openProjects(page, variant) {
	if (isPhone(variant)) await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Projects', exact: true }).click();
	// K1 moves the phone title/create action into the top bar, without a list heading.
	await page.locator('[data-view-shell="Projects"]').filter({ visible: true }).waitFor({ timeout: 30_000 });
	await page.getByRole('button', { name: 'New project', exact: true }).filter({ visible: true }).waitFor();
}

/** Open another rail page (under More when the phone's tab row has no seat for it). */
async function openPage(page, variant, label) {
	if (isPhone(variant)) await showSidebar(page);
	const workspace = page.getByRole('navigation', { name: 'Workspace' });
	const target = workspace.getByRole('button', { name: label, exact: true });
	if (await target.count()) await target.click();
	else {
		await workspace.getByRole('button', { name: 'More', exact: true }).click();
		await page.getByRole('menuitem', { name: label, exact: true }).click();
	}
	await page.locator(`[data-view-shell="${label}"]`).filter({ visible: true }).waitFor({ timeout: 30_000 });
}

/** The Projects page has decided to hold the worker filter row's space (blank until the overview fills it). */
async function waitReservedFilter(page) {
	await page.locator('section.view-panel[aria-label="Projects"]').filter({ visible: true }).locator('.worker-filter.reserved').waitFor({ state: 'attached', timeout: 20_000 });
}

/** The visible Projects list region: its top edge and whether it shows an empty state. */
async function listGeometry(page) {
	const body = page.locator('section.view-panel[aria-label="Projects"]').filter({ visible: true }).locator('.panel-body');
	const box = await body.boundingBox();
	return { top: Math.round(box?.y ?? -1), empty: (await body.locator('.st-empty-detail').count()) > 0 };
}

/** A worker name longer than the Projects panel is wide (#1210 review). */
const LONG_WORKER_NAME = 'My Very Long Workstation Name In The Upstairs Office';

const projectRow = (page, name) => page.getByRole('button', { name: new RegExp(`^Open ${name}( \\(|$)`) });
const listMode = (page) => page.locator('[data-ui-projects]').getAttribute('data-ui-projects');

await runJourney({
	name: 'project-workers',
	// Rendering and polling, not a streaming race: one throttle is enough.
	throttles: ['fast'],
	cases: [
		{
			name: 'master-detail',
			async setup(ctx) { await gotoApp(ctx.page, ctx.status); },
			async act({ page, mark, variant }) {
				await mark('open Projects master/detail');
				await openProjects(page, variant);
				if (isPhone(variant)) {
					expect(await projectRow(page, PROJECT).isVisible(), 'phone starts on the project list');
				} else {
					await page.getByRole('heading', { level: 2 }).filter({ hasText: /^Dev / }).waitFor();
					expect(await projectRow(page, PROJECT).isVisible(), 'desktop detail keeps the project list visible');
				}
				await projectRow(page, PROJECT).click();
				await page.getByRole('heading', { level: 2, name: PROJECT, exact: true }).waitFor();
				await mark('open Memory and create or edit project instructions');
				await page.getByRole('tab', { name: 'Memory', exact: true }).click();
				const memory = page.getByRole('region', { name: 'Project memory', exact: true });
				await memory.waitFor();
				const create = memory.getByRole('button', { name: 'Create', exact: true });
				if (await create.count()) await create.click();
				else await memory.getByRole('button', { name: 'Edit', exact: true }).click();
				const text = 'Keep changes focused and run the affected tests.';
				await memory.getByRole('textbox', { name: 'Edit Project memory' }).fill(text);
				await memory.getByRole('button', { name: 'Save', exact: true }).click();
				await memory.getByRole('button', { name: text, exact: true }).waitFor();
				await mark('verify saved project memory through its read API');
				const files = await page.evaluate(async () => (await fetch('/api/v1/projects/dev-sandbox/memory')).json());
				expect(files.find(f => f.location === 'project')?.content === text, 'the UI saved the project memory on the worker');
				if (isPhone(variant)) {
					await mark('Back returns to Projects without opening the drawer');
					await page.getByRole('button', { name: 'Back to Projects', exact: true }).click();
					await projectRow(page, PROJECT).waitFor();
				}
				await mark('New project launches the shared modal');
				await page.getByRole('button', { name: 'New project', exact: true }).click();
				await page.getByRole('dialog').waitFor();
				await page.keyboard.press('Escape');
			}
		},
		{
			name: 'fail-open',
			async setup(ctx) {
				await simulateWorkers(ctx.page, { overview404: true });
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects on a worker without the overview');
				await openProjects(page, variant);
				await projectRow(page, PROJECT).waitFor({ timeout: 30_000 });
				await page.waitForTimeout(1500);
				expect((await listMode(page)) === 'local', 'an older worker shows the plain project list');
				expect((await page.getByRole('group', { name: /^Workers with / }).count()) === 0, 'no worker chips without the overview');
				expect((await page.getByRole('group', { name: 'Show projects' }).count()) === 0, 'no worker filter without the overview');
			},
			allow: [{ kind: 'failed_requests', status: 404, url: /\/projects\/overview(\?|$)/, reason: 'the older worker this case simulates has no overview route (the fail-open path under test)' }]
		},
		{
			name: 'chip-sets-up',
			async setup(ctx) {
				await simulateWorkers(ctx.page);
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects');
				await openProjects(page, variant);
				await page.locator('[data-ui-projects="overview"]').waitFor({ timeout: 30_000 });
				await projectRow(page, PROJECT).click();
				const chips = page.getByRole('group', { name: `Workers with ${PROJECT}` });
				await chips.getByRole('button', { name: /^Studio: / }).waitFor();
				if (!isPhone(variant)) expect(await page.getByRole('group', { name: 'Show projects' }).isVisible(), 'the All workers / On this worker filter shows');

				await mark('click the dashed pop-os chip');
				await chips.getByRole('button', { name: `pop-os: not set up — set ${PROJECT} up on pop-os` }).click();
				await chips.getByRole('button', { name: /^pop-os · cloning/ }).waitFor({ timeout: 10_000 });
				const started = await page.evaluate(() => window.__projectSetups.map((s) => s.install_id));
				expect(started.length === 1 && started[0] === 'pop-os', `one setup started on pop-os, got ${JSON.stringify(started)}`);
				// The 2 s poll carries it to ready.
				await chips.getByRole('button', { name: 'pop-os: pop-os has this project' }).waitFor({ timeout: 20_000 });
				await page.waitForTimeout(1500);
			}
		},
		{
			// #975: bootstrap used the user-facing New Chat handler, dismissing a
			// phone drawer opened while its startup data was still loading.
			name: 'drawer-survives-bootstrap',
			viewports: ['phone'],
			recorder: { selectors: ['sessions=[aria-label="Chats"]', 'workspace=[aria-label="Workspace"]'] },
			async setup({ page, status }) {
				await simulateWorkers(page);
				await holdBootLoad(page, '/api/v1/tags', 30_000, { releaseOnSignal: true });
				await gotoApp(page, status);
				await page.waitForFunction(() => typeof window.__journeyBootHold?.release === 'function');
			},
			async act({ page, mark }) {
				await mark('open drawer before bootstrap settles');
				const sessions = await showSidebar(page);
				const before = await page.evaluate(() => ({
					held: window.__journeyBootHold?.releasedAt === null,
					view: JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView
				}));
				expect(before.held && before.view == null, 'the fresh landing is still awaiting its startup data');

				await mark('finish bootstrap with the drawer open');
				await page.evaluate(() => window.__journeyBootHold.release());
				await page.waitForFunction(() => JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView != null);
				expect(await sessions.isVisible(), 'bootstrap must not dismiss the drawer the user opened');
				// Click directly: reopening with showSidebar here would hide the bug.
				await mark('navigate to Projects');
				await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Projects', exact: true }).click();
				await page.locator('[data-ui-projects="overview"]').waitFor({ timeout: 30_000 });
			}
		},
		{
			name: 'remote-only-detail',
			async setup(ctx) {
				await simulateWorkers(ctx.page);
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects');
				await openProjects(page, variant);
				await mark('open a project that is only on other workers');
				await projectRow(page, 'couch-cash').click();
				const workersTab = page.getByRole('tab', { name: /^Workers/ });
				await workersTab.waitFor({ timeout: 20_000 });
				expect((await workersTab.getAttribute('aria-selected')) === 'true', 'it opens on the Workers tab');
				expect((await page.getByRole('tab', { name: 'General' }).getAttribute('aria-disabled')) === 'true', 'the local tabs are disabled');
				await page.locator('[data-ui-project-workers="ready"]').waitFor({ timeout: 20_000 });
				await page.getByRole('button', { name: 'Retry' }).waitFor();

				await mark('Set up on all missing');
				await page.getByRole('button', { name: 'Set up on all missing' }).click();
				await page.locator('article[data-worker-state="ready"]', { has: page.getByText('This worker · Online', { exact: true }) }).waitFor({ timeout: 20_000 });
				await page.waitForTimeout(1500);
			}
		},
		{
			// #1210: the filter row appeared only once the overview answered and
			// pushed the whole list down 62px (layout shift 0.101 on a phone).
			// Fast hosted runs hid it inside the 500ms after the tap; a slow
			// overview always shows it. Hold the overview past that window.
			name: 'filter-before-overview',
			viewports: ['phone'],
			async setup(ctx) {
				await simulateWorkers(ctx.page, { holdOverview: true });
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects while the overview is still out');
				await openProjects(page, variant);
				await projectRow(page, PROJECT).waitFor({ timeout: 30_000 });
				expect((await listMode(page)) === 'local', 'the overview has not answered yet');
				await page.waitForTimeout(800);
				await mark('the overview answers');
				await page.evaluate(() => window.__releaseOverview());
				await page.locator('[data-ui-projects="overview"]').waitFor({ timeout: 20_000 });
				expect(await page.getByRole('group', { name: 'Show projects' }).isVisible(), 'the All workers / On this worker filter shows');
				await page.waitForTimeout(1000);
			}
		},
		{
			// #1210 review: with the registry also slow, the list painted before
			// the registry hinted at peers, then gained the filter row's space.
			// The list waits for the registry instead of moving afterwards.
			name: 'filter-before-registry',
			viewports: ['desktop', 'phone'],
			async setup(ctx) {
				await simulateWorkers(ctx.page, { holdOverview: true, holdInstalls: true });
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects while the registry and the overview are still out');
				await openProjects(page, variant);
				await page.waitForTimeout(800);
				await mark('the registry answers');
				await page.evaluate(() => window.__releaseInstalls());
				await projectRow(page, PROJECT).waitFor({ timeout: 30_000 });
				expect((await listMode(page)) === 'local', 'the overview has not answered yet');
				await page.waitForTimeout(800);
				await mark('the overview answers');
				await page.evaluate(() => window.__releaseOverview());
				await page.locator('[data-ui-projects="overview"]').waitFor({ timeout: 20_000 });
				expect(await page.getByRole('group', { name: 'Show projects' }).isVisible(), 'the All workers / On this worker filter shows');
				await page.waitForTimeout(1000);
			}
		},
		{
			// #1210 review: an older worker with approved peers answers the
			// overview late with 404. The space held for the filter stays (blank)
			// for this visit rather than pulling the list up, and no control shows.
			name: 'filter-space-unsupported',
			viewports: ['desktop', 'phone'],
			async setup(ctx) {
				await simulateWorkers(ctx.page, { holdOverview: true, overviewStatus: 404 });
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects while the overview is still out');
				await openProjects(page, variant);
				await projectRow(page, PROJECT).waitFor({ timeout: 30_000 });
				expect((await page.getByRole('group', { name: 'Show projects' }).count()) === 0, 'no filter control before the overview answers');
				await page.waitForTimeout(800);
				await mark('the overview answers 404');
				await page.evaluate(() => window.__releaseOverview());
				await page.waitForFunction(() => window.__overviewAnswers > 0, null, { timeout: 20_000 });
				await page.waitForTimeout(1500);
				expect((await listMode(page)) === 'local', 'an older worker shows the plain project list');
				expect((await page.getByRole('group', { name: 'Show projects' }).count()) === 0, 'no worker filter without the overview');
			},
			allow: [{ kind: 'failed_requests', status: 404, url: /\/projects\/overview(\?|$)/, reason: 'the older worker this case simulates has no overview route (the fail-open path under test)' }]
		},
		{
			// #1210 review: this worker has no projects but a peer does. The empty
			// state hid the filter region until the peer's projects arrived, then
			// the row appeared above the list. The list waits for the overview,
			// up to the filter deadline; the case holds that deadline itself, so
			// a slow registry or runner cannot pass it before the measurement.
			name: 'remote-only-bootstrap',
			viewports: ['desktop', 'phone'],
			async setup(ctx) {
				await simulateWorkers(ctx.page, { holdOverview: true, holdInstalls: true, noLocalProjects: true, holdDeadline: true });
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects with no local projects while the registry and the overview are still out');
				// Not openProjects: an empty list adds its own "New project" button.
				if (isPhone(variant)) await showSidebar(page);
				await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Projects', exact: true }).click();
				await page.locator('[data-view-shell="Projects"]').filter({ visible: true }).waitFor({ timeout: 30_000 });
				// Longer than the 2 s default deadline: a slow registry.
				await page.waitForTimeout(2500);
				await mark('the registry answers');
				await page.evaluate(() => window.__releaseInstalls());
				await page.waitForFunction(() => window.__installsAnswers > 0, null, { timeout: 20_000 });
				await waitReservedFilter(page);
				// Nothing painted moves here (the empty state is replaced, which the
				// recorder does not count), so measure the list region itself.
				const before = await listGeometry(page);
				expect(!before.empty, 'no "No projects" state while the peers may still bring projects');
				await mark('the filter deadline passes');
				await page.evaluate(() => window.__passFilterDeadline());
				await page.locator('section.view-panel[aria-label="Projects"]').filter({ visible: true }).locator('.st-empty-detail').waitFor({ timeout: 20_000 });
				const empty = await listGeometry(page);
				expect(empty.empty, 'after the deadline the list shows what is known: no local projects');
				expect(empty.top === before.top, `"No projects" keeps the held row (list top ${before.top}px, then ${empty.top}px)`);
				await page.waitForTimeout(800);
				await mark('the overview answers with the peers\' projects');
				await page.evaluate(() => window.__releaseOverview());
				await projectRow(page, 'couch-cash').waitFor({ timeout: 20_000 });
				expect((await listMode(page)) === 'overview', 'the peers\' projects are listed');
				expect(await page.getByRole('group', { name: 'Show projects' }).isVisible(), 'the All workers / On this worker filter shows');
				const after = await listGeometry(page);
				expect(after.top === before.top, `the list stays where it was painted (top ${before.top}px, then ${after.top}px)`);
				await page.waitForTimeout(1000);
			}
		},
		{
			// #1210 review: the row's space was held for "On Local" (the registry
			// failed, so no name yet) and local projects painted under it. The
			// overview then named this worker with a long name; the filter wrapped
			// onto a second line and pushed the list down. The row is one line.
			name: 'late-long-worker-name',
			viewports: ['desktop', 'phone'],
			async setup(ctx) {
				await simulateWorkers(ctx.page, { holdOverview: true, installsStatus: 500, selfName: LONG_WORKER_NAME });
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects: the registry fails, the overview is still out');
				await openProjects(page, variant);
				await projectRow(page, PROJECT).waitFor({ timeout: 30_000 });
				await waitReservedFilter(page);
				const panel = page.locator('section.view-panel[aria-label="Projects"]').filter({ visible: true });
				const heldRow = await panel.locator('.worker-filter').boundingBox();
				const before = await listGeometry(page);
				await page.waitForTimeout(800);
				await mark('the overview answers with a long name for this worker');
				await page.evaluate(() => window.__releaseOverview());
				await page.locator('[data-ui-projects="overview"]').waitFor({ timeout: 20_000 });
				const group = page.getByRole('group', { name: 'Show projects' });
				expect(await group.isVisible(), 'the All workers / On this worker filter shows');
				const here = group.getByRole('button', { name: `On ${LONG_WORKER_NAME}`, exact: true });
				expect(await here.isVisible(), 'the filter names this worker in full to assistive technology');
				expect((await here.getAttribute('title')) === `Show only projects on ${LONG_WORKER_NAME}`, 'and in its tooltip');
				expect(await group.getByRole('button', { name: 'All workers', exact: true }).isVisible(), 'both choices stay on screen');
				const filledRow = await panel.locator('.worker-filter').boundingBox();
				expect(Math.round(filledRow?.height ?? -1) === Math.round(heldRow?.height ?? -2), `the filled row is as tall as the held one (${heldRow?.height}px, then ${filledRow?.height}px)`);
				const after = await listGeometry(page);
				expect(after.top === before.top, `the list stays where it was painted (top ${before.top}px, then ${after.top}px)`);
				await page.waitForTimeout(1000);
			},
			allow: [
				{ kind: 'failed_requests', status: 500, url: /\/cluster\/installs(\?|$)/, reason: 'the failing install registry this case simulates' }
			]
		},
		{
			// #1210 review: the registry fails while the overview is still out. The
			// list painted without the row, then the overview inserted it. A failed
			// registry gives no answer, so the row's space is held instead.
			name: 'registry-fails-then-overview',
			viewports: ['desktop', 'phone'],
			async setup(ctx) {
				await simulateWorkers(ctx.page, { holdOverview: true, installsStatus: 500 });
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects: the registry fails, the overview is still out');
				await openProjects(page, variant);
				await projectRow(page, PROJECT).waitFor({ timeout: 30_000 });
				expect((await listMode(page)) === 'local', 'the overview has not answered yet');
				await page.waitForTimeout(800);
				await mark('the overview answers');
				await page.evaluate(() => window.__releaseOverview());
				await page.locator('[data-ui-projects="overview"]').waitFor({ timeout: 20_000 });
				expect(await page.getByRole('group', { name: 'Show projects' }).isVisible(), 'the All workers / On this worker filter shows');
				await page.waitForTimeout(1000);
			},
			allow: [
				{ kind: 'failed_requests', status: 500, url: /\/cluster\/installs(\?|$)/, reason: 'the failing install registry this case simulates' }
			]
		},
		{
			// #1210 review: the overview failed on the first visit. On the next
			// visit the old "settled" flag still said answered, so the list painted
			// without the row and the overview then inserted it. Each visit decides
			// from its own answers.
			name: 'revisit-after-failed-overview',
			viewports: ['desktop', 'phone'],
			async setup(ctx) {
				await simulateWorkers(ctx.page, { holdOverview: true, overviewFailFirst: true });
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('first visit: the overview fails');
				await openProjects(page, variant);
				await projectRow(page, PROJECT).waitFor({ timeout: 30_000 });
				await page.waitForFunction(() => window.__overviewAnswers > 0, null, { timeout: 20_000 });
				await mark('leave Projects');
				await openPage(page, variant, 'Notes');
				await mark('come back while the overview is still out');
				await openProjects(page, variant);
				await projectRow(page, PROJECT).waitFor({ timeout: 30_000 });
				expect((await listMode(page)) === 'local', 'the overview has not answered yet');
				await page.waitForTimeout(800);
				await mark('the overview answers');
				await page.evaluate(() => window.__releaseOverview());
				await page.locator('[data-ui-projects="overview"]').waitFor({ timeout: 20_000 });
				expect(await page.getByRole('group', { name: 'Show projects' }).isVisible(), 'the All workers / On this worker filter shows');
				await page.waitForTimeout(1000);
			},
			allow: [
				{ kind: 'failed_requests', status: 500, url: /\/projects\/overview(\?|$)/, reason: 'the first overview failure this case simulates' },
				{ kind: 'errors', message: /Failed to load projects overview: .*"status":500/, reason: 'the console line of that same simulated overview failure' }
			]
		},
		{
			// #1210 review: with the registry and the overview both stalled, the
			// list waited for them for ever. It shows local projects after a short
			// wait, holding the row's space, so the late answers move nothing.
			name: 'stalled-requests',
			viewports: ['desktop', 'phone'],
			async setup(ctx) {
				await simulateWorkers(ctx.page, { holdOverview: true, holdInstalls: true });
				await gotoApp(ctx.page, ctx.status);
			},
			async act({ page, mark, variant }) {
				await mark('open Projects while the registry and the overview stall');
				await openProjects(page, variant);
				await projectRow(page, PROJECT).waitFor({ timeout: 10_000 });
				await waitReservedFilter(page);
				expect((await page.getByRole('group', { name: 'Show projects' }).count()) === 0, 'no filter control before an answer');
				await page.waitForTimeout(800);
				await mark('the registry answers');
				await page.evaluate(() => window.__releaseInstalls());
				await page.waitForFunction(() => window.__installsAnswers > 0, null, { timeout: 20_000 });
				await page.waitForTimeout(800);
				await mark('the overview answers');
				await page.evaluate(() => window.__releaseOverview());
				await page.locator('[data-ui-projects="overview"]').waitFor({ timeout: 20_000 });
				expect(await page.getByRole('group', { name: 'Show projects' }).isVisible(), 'the All workers / On this worker filter shows');
				await page.waitForTimeout(1000);
			}
		},
		{
			name: 'new-chat-fails',
			async setup(ctx) {
				await simulateWorkers(ctx.page, { failOn: { 'pv2-sandbox:pop-os': ['network', "pop-os couldn't reach the repository."] } });
				await openNewChat(ctx, { project: PROJECT });
			},
			async act({ page, mark }) {
				const hero = page.getByRole('region', { name: 'New chat' });
				await mark('open Runs on');
				await withChatSetup(page, async () => {
					// With a project, Chat controls uses the runner chip's name for the same control.
					await hero.getByRole('button', { name: /^(Runs on |Runner: )/ }).click();
					const menu = page.getByRole('menu', { name: /^(Where this session runs|Runner selection)$/ });
					const pop = menu.getByRole('menuitemradio', { name: 'pop-os not set up · set up' });
					await pop.waitFor({ timeout: 20_000 });
					expect(!(await pop.isDisabled()), 'a worker without the project stays selectable');
					expect(await menu.getByRole('menuitemradio', { name: 'Laptop offline' }).isDisabled(), 'an offline worker is blocked');

					await mark('pick pop-os');
					await pop.click();
					await page.locator('[data-ui-setup="needed"]').waitFor({ state: 'attached', timeout: 20_000 });
				});

				const text = `Summarise the README ${uniq()}`;
				const box = page.getByRole('textbox', { name: 'Message' });
				await box.click();
				await box.fill(text);
				await mark('Set up & send');
				await page.getByRole('button', { name: 'Set up & send' }).click();
				await page.locator('[data-ui-setup="failed"]').waitFor({ timeout: 20_000 });
				await page.getByText("pop-os couldn't reach the repository.").waitFor();
				await waitChat(page, { surface: 'new' });
				const kept = (await box.innerText()).trim();
				expect(kept === text, `the draft is kept after a failed setup, got ${JSON.stringify(kept)}`);
				await page.waitForTimeout(1500);
			},
			allow: [
				{
					kind: 'failed_requests',
					status: 500,
					url: /\/git\/status(\?|$)/,
					reason: 'the simulated peers make the branch badge ask for git status across a worker network this single dev instance never joined ("This worker has not joined a network"); the badge is outside this journey'
				},
				{
					kind: 'errors',
					message: /Failed to load git repository: .*not joined a network/,
					reason: 'the console line of that same git status refusal (simulated peers on a single dev instance)'
				}
			]
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
