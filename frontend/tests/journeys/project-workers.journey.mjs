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
//   new-chat-fails      a new chat on pop-os: the Runs-on row says "not set up
//                       · set up" (selectable), offline Laptop is blocked; the
//                       callout shows, Send reads "Set up & send", the setup
//                       fails and the reason shows with the draft kept.
import { runJourney } from './runner.mjs';
import { PROJECT, expect, gotoApp, isPhone, openNewChat, showSidebar, uniq, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

/**
 * Simulate three peer workers and the projects overview. Call before the
 * page loads. `failOn` names `<project_v2_id>:<install_id>` setups that fail.
 */
async function simulateWorkers(page, { overview404 = false, failOn = {} } = {}) {
	await page.addInitScript(
		({ overview404, failOn }) => {
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
				{ install_id: selfId, device_name: 'This worker', is_self: true, online: true, approved: true },
				...peers.map((p) => ({ ...p, is_self: false, approved: true }))
			];
			const P = [
				{ id: 'pv2-sandbox', local: 'dev-sandbox', name: 'Dev sandbox', kind: 'git', remote: 'github.com/example/dev-sandbox',
					st: { self: 'ready', studio: 'ready', 'pop-os': 'missing', laptop: 'offline_missing' } },
				{ id: 'pv2-couch', local: null, name: 'couch-cash', kind: 'git', remote: 'github.com/example/couch-cash',
					st: { self: 'missing', studio: 'ready', 'pop-os': 'failed', laptop: 'offline_has' } }
			];
			const setup = { 'pv2-couch:pop-os': { op_id: 'op-0', status: 'error', progress: null, error_code: 'network', error: "pop-os couldn't reach the repository." } };
			let ops = 0;
			window.__projectSetups = [];
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
				if (path === '/api/v1/cluster/installs' && method === 'GET') {
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
		{ overview404, failOn }
	);
}

/** Open the Projects page from the workspace rail (the drawer on a phone). */
async function openProjects(page, variant) {
	if (isPhone(variant)) await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Projects', exact: true }).click();
	await page.getByRole('heading', { name: /projects/i, level: 1 }).waitFor({ timeout: 30_000 });
}

const projectRow = (page, name) => page.getByRole('button', { name: new RegExp(`^Open ${name}( \\(|$)`) });
const listMode = (page) => page.locator('[data-ui-projects]').getAttribute('data-ui-projects');

await runJourney({
	name: 'project-workers',
	// Rendering and polling, not a streaming race: one throttle is enough.
	throttles: ['fast'],
	cases: [
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
				expect((await page.getByRole('radiogroup', { name: 'Show projects' }).count()) === 0, 'no worker filter without the overview');
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
				const chips = page.getByRole('group', { name: `Workers with ${PROJECT}` });
				await chips.getByRole('button', { name: /^Studio: / }).waitFor();
				expect(await page.getByRole('radiogroup', { name: 'Show projects' }).isVisible(), 'the All workers / On this worker filter shows');

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
				await page.locator('article[data-worker-state="ready"]', { has: page.getByText('this worker', { exact: true }) }).waitFor({ timeout: 20_000 });
				await page.waitForTimeout(1500);
			}
		},
		{
			name: 'new-chat-fails',
			async setup(ctx) {
				await simulateWorkers(ctx.page, { failOn: { 'pv2-sandbox:pop-os': ['network', "pop-os couldn't reach the repository."] } });
				await openNewChat(ctx, { project: PROJECT });
			},
			async act({ page, mark }) {
				const hero = page.getByRole('region', { name: 'New session' });
				await mark('open Runs on');
				await hero.getByRole('button', { name: /^Runs on / }).click();
				const menu = page.getByRole('menu', { name: 'Where this session runs' });
				const pop = menu.getByRole('menuitemradio', { name: 'pop-os not set up · set up' });
				await pop.waitFor({ timeout: 20_000 });
				expect(!(await pop.isDisabled()), 'a worker without the project stays selectable');
				expect(await menu.getByRole('menuitemradio', { name: 'Laptop offline' }).isDisabled(), 'an offline worker is blocked');

				await mark('pick pop-os');
				await pop.click();
				await page.locator('[data-ui-setup="needed"]').waitFor({ timeout: 20_000 });

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
