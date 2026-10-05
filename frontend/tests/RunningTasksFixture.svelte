<script lang="ts">
	// Mounts the production RunningTasksList against running-tasks-stubs.ts.
	// URL: ?tasks=2|17 &swarm=1 &shell=mobile &variant=sheet &theme=vercel &mode=light
	import RunningTasksList from '../src/lib/components/chat/RunningTasksList.svelte';
	import { activeSession, swarms, stopped, opened } from './running-tasks-stubs';
	import type { Task } from '../src/lib/stores/types';
	import type { SwarmSnapshot } from '../src/lib/utils/swarm';

	const params = new URLSearchParams(location.search);
	const count = Number(params.get('tasks') ?? '2');
	const withSwarm = params.get('swarm') === '1';
	const startSheet = params.get('variant') === 'sheet';
	let sheetOpen = $state(startSheet);

	const TASKS17: [Task['kind'], string, string, number][] = [
		['agent', 'Fix: memory health signals and running revisions', 'DesktopWorker', 252],
		['shell', 'pytest -q tests/test_memory_health.py', 'DesktopWorker', 58],
		['agent', 'Docs: Linux desktop install guide screenshots', 'DesktopWorker', 1730],
		['agent', 'Review: worker transfer queue edge cases', 'Unraid', 845],
		['agent', 'Refactor: composer controls dock props', 'DesktopWorker', 2410],
		['agent', 'Investigate: usage ring stale after reconnect', 'Unraid', 512],
		['agent', 'Test: Android WebView keyboard insets', 'DesktopWorker', 96],
		['agent', 'Fix: settled chats reopening on another worker', 'Unraid', 3305],
		['agent', 'Audit: session cache hit rate, last 24 hours', 'Unraid', 1288],
		['agent', 'Port: model picker favorites to mobile', 'DesktopWorker', 640],
		['agent', 'Fix: hub rate limit on live checks', 'Unraid', 177],
		['agent', 'Draft: release notes for 0.8.24', 'DesktopWorker', 410],
		['agent', 'General Purpose: AppImage auto-update feed', 'DesktopWorker', 1902],
		['shell', 'npm run dist:linux', 'DesktopWorker', 212],
		['shell', 'npm ci --prefix frontend', 'DesktopWorker', 31],
		['shell', 'docker logs -f ai-shuffle', 'Unraid', 4020],
		['shell', 'ruff check app/', 'Unraid', 12]
	];
	const now = Date.now();
	const iso = (agoSec: number) => new Date(now - agoSec * 1000).toISOString();
	const source = count === 2 ? TASKS17.slice(0, 2) : TASKS17.slice(0, count);
	const tasks: Task[] = source.map(([kind, name, worker, ago], i) => ({
		id: `t${i}`,
		subject: name,
		status: 'running',
		kind,
		summary: null,
		toolUseId: `tool-t${i}`,
		startedAt: iso(ago),
		updatedAt: iso(0),
		...(worker !== 'DesktopWorker' ? { worker: { name: worker } } : {})
	}));
	// Agent rows link to their subagent chat through their Task card.
	const messages = tasks
		.filter((t) => t.kind === 'agent')
		.map((t) => ({ id: `card-${t.id}`, role: 'assistant', type: 'subagent', content: '', toolId: t.toolUseId, agentSessionId: `child-${t.id}` }));

	if (withSwarm) {
		const swarm: SwarmSnapshot = {
			id: 'swarm_release',
			session_id: 'fixture-session',
			title: 'Release 0.8.24 readiness review',
			status: 'running',
			created_at: iso(900),
			updated_at: iso(5),
			tasks: [
				{ id: 'scan', title: 'Scan routes for auth gaps', agent: 'General Purpose', depends_on: [], status: 'completed', task_id: 'sw-m0', worker_session_id: 'child-scan', started_at: iso(900), finished_at: iso(600) },
				{ id: 'tests', title: 'Run the full backend suite', agent: 'Tester', depends_on: [], status: 'running', task_id: 'sw-m1', worker_session_id: 'child-tests', started_at: iso(580) },
				{ id: 'ui', title: 'Check the phone composer at 390px', agent: 'Frontend Designer', depends_on: [], status: 'running', task_id: 'sw-m2', worker_session_id: 'child-ui', started_at: iso(300) },
				{ id: 'notes', title: 'Write the release notes', agent: 'Writer', depends_on: ['scan', 'tests', 'ui'], status: 'blocked' }
			]
		} as SwarmSnapshot;
		const finished: SwarmSnapshot = {
			...swarm,
			id: 'swarm_old',
			title: 'Dependency upgrade sweep',
			status: 'completed',
			created_at: iso(7200),
			updated_at: iso(5400),
			tasks: swarm.tasks.map((t) => ({ ...t, status: 'completed', task_id: null }))
		} as SwarmSnapshot;
		swarms.set([swarm, finished]);
		tasks.push(
			{ id: 'swarm_release', subject: swarm.title, status: 'running', kind: 'agent', summary: null, toolUseId: 'swarm_release', startedAt: iso(900), updatedAt: iso(0) },
			{ id: 'sw-m1', subject: 'Run the full backend suite', status: 'running', kind: 'agent', summary: null, toolUseId: null, startedAt: iso(580), updatedAt: iso(0) },
			{ id: 'sw-m2', subject: 'Check the phone composer at 390px', status: 'running', kind: 'agent', summary: null, toolUseId: null, startedAt: iso(300), updatedAt: iso(0) }
		);
	}
	activeSession.set({ sessionId: 'fixture-session', activeTasks: tasks, messages });

	(window as unknown as Record<string, unknown>).runningTasksFixture = { stopped, opened, openSheet: () => (sheetOpen = true) };
</script>

<main class="chat">
	<div class="column">
		<div class="bubble user">Run the release checks and fix what fails.</div>
		<div class="reply">
			<p>I started the checks. The backend suite and the phone layout review run in the background; I will report when they finish.</p>
		</div>
		<RunningTasksList sessionId="fixture-session" variant="inline" />
		{#if !startSheet}
			<button type="button" class="open-sheet" onclick={() => (sheetOpen = true)}>Open as sheet (controls panel row)</button>
		{/if}
	</div>
</main>
{#if sheetOpen}
	<RunningTasksList sessionId="fixture-session" variant="sheet" onClose={() => (sheetOpen = false)} />
{/if}

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: var(--font-sans); }
	.chat { min-height: 100dvh; display: flex; justify-content: center; padding: 24px 16px; box-sizing: border-box; }
	.column { width: 100%; max-width: 48rem; display: flex; flex-direction: column; gap: 14px; }
	.bubble { align-self: flex-end; max-width: 80%; padding: 10px 14px; border-radius: 16px; background: var(--muted); font-size: 0.9375rem; }
	.reply p { margin: 0; font-size: 0.9375rem; line-height: 1.6; }
	.open-sheet { align-self: flex-start; margin-top: 24px; padding: 8px 12px; border: 1px dashed var(--border); border-radius: 8px; background: transparent; color: var(--muted-foreground); font: inherit; font-size: 0.75rem; cursor: pointer; }
</style>
