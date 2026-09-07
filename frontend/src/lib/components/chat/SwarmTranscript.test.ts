import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { reduceAll, type WireEvent } from '$lib/stores/session/v2/reducer';
import { swarms } from '$lib/stores/swarms';
import { buildDisplayItems } from '$lib/utils/work-log';
import type { SwarmSnapshot } from '$lib/utils/swarm';
import SystemMessage from '../SystemMessage.svelte';
import ToolUseMessage from './ToolUseMessage.svelte';

// Recorded transport shape from the real ten-child acceptance run: a nested
// code-mode Swarm call, a 3,000-character result preview, eleven worker receipts
// sharing that launch id, then a shell task-notification with the full snapshot.
// Worker identities below are synthetic; no provider or live worker is needed.
function transcript(sessionId: string, name = 'Swarm') {
	const launchId = 'call_075834a3c013';
	const ids = Array.from({ length: 10 }, (_, index) => `c${String(index).padStart(2, '0')}`);
	const input = {
		tasks: [...ids, 'summary'].map((id) => ({
			id, title: id, agent: 'Swarm probe helper',
			prompt: id === 'summary' ? 'Confirm all ten children are ready.' : `Return exactly READY ${id}. Do not call tools, read files, or do any additional work.`,
			depends_on: id === 'summary' ? ids : [],
		})),
	};
	const queued: SwarmSnapshot = {
		id: 'swarm_3a855e7b69874c588d1884150ff9f608', session_id: sessionId,
		title: 'Swarm', status: 'queued',
		created_at: '2026-09-07T10:00:00Z', updated_at: '2026-09-07T10:00:00Z',
		tasks: input.tasks.map((task) => ({
			...task, status: 'queued', task_id: null, worker_session_id: null,
			result: null, error: null, started_at: null, finished_at: null,
		})),
	};
	const completed: SwarmSnapshot = {
		...queued, status: 'completed', updated_at: '2026-09-07T10:02:00Z',
		tasks: queued.tasks.map((task) => ({
			...task, status: 'completed', task_id: `worker-${task.id}`, worker_session_id: `worker-${task.id}`,
			result: task.id === 'summary' ? 'ALL_TEN_READY' : `READY ${task.id}`,
		})),
	};
	let seq = 0;
	const ev = (type: string, fields: Record<string, unknown> = {}): WireEvent => ({
		type, id: `event-${++seq}`, seq, session_id: sessionId,
		created_at: '2026-09-07T10:02:00Z', ...fields,
	});
	const preview = JSON.stringify({ swarm: queued }).slice(0, 3000);
	const launch = [
		ev('message.user', { content: 'Run ten independent workers, then summarize.' }),
		ev('turn.started'),
		ev('tool.use', { tool_use_id: 'outer', name: 'run_python', input: { code: 'await tools.Swarm(...)' } }),
		ev('tool.use', { tool_use_id: launchId, name, input }),
		ev('swarm.updated', { swarm: queued }),
		ev('task.updated', { task_id: queued.id, tool_use_id: queued.id, kind: 'shell', state: 'started', description: 'Swarm' }),
		ev('tool.result', { tool_use_id: launchId, content_preview: preview, is_error: false }),
		ev('tool.result', { tool_use_id: 'outer', content_preview: preview, is_error: false }),
		...queued.tasks.map((task) => ev('subagent.started', {
			task_id: `worker-${task.id}`, agent_id: `worker-${task.id}`, agent_name: task.agent,
			agent_type: 'gpt-5.6-terra', launch_tool_use_id: launchId, description: task.title,
		})),
	];
	const finish = [
		...completed.tasks.map((task) => ev('subagent.completed', {
			task_id: task.task_id, status: 'completed', result_preview: task.result,
		})),
		ev('swarm.updated', { swarm: completed }),
		ev('message.user', { content: `<task-notification><kind>own</kind><task-id>${queued.id}</task-id><tool-use-id>${queued.id}</tool-use-id><status>completed</status><summary>${JSON.stringify({ swarm: completed })}</summary></task-notification>` }),
		ev('task.updated', { task_id: queued.id, tool_use_id: queued.id, kind: 'shell', state: 'completed', summary: JSON.stringify({ swarm: completed }) }),
		ev('assistant.text_delta', { text: 'ALL_TEN_READY' }),
		ev('turn.completed'),
	];
	return { launch, finish, queued, completed, preview, launchId };
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Swarm transcript acceptance', () => {
	it.each(['Swarm', 'mcp__fluid__Swarm'])('keeps %s visible through eleven child receipts and replay with a truncated result', async (name) => {
		const fixture = transcript(`acceptance-${name}`, name);
		expect(() => JSON.parse(fixture.preview)).toThrow();
		const running = reduceAll(fixture.launch);
		const row = running.messages.find((message) => message.type === 'tool_use' && message.toolId === fixture.launchId);
		expect(row).toBeDefined();
		expect(running.messages.filter((message) => message.type === 'subagent')).toHaveLength(11);
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ swarms: [fixture.queued] }), { status: 200 })));
		swarms.receive(fixture.queued, fixture.queued.session_id);
		render(ToolUseMessage, {
			name: row!.toolName!, sessionId: fixture.queued.session_id, input: row!.toolInput,
			result: row!.toolResult, status: row!.toolStatus!,
		});
		expect(screen.getAllByTestId('swarm-card')).toHaveLength(1);
		expect(screen.getByText('Stage 1')).toBeTruthy();
		expect(screen.getByText('Stage 2')).toBeTruthy();
		expect(screen.getByText('0 of 11 tasks completed')).toBeTruthy();
		swarms.receive(fixture.completed, fixture.completed.session_id);
		await waitFor(() => expect(screen.getByText('11 of 11 tasks completed')).toBeTruthy());
		await fireEvent.click(screen.getByText('c00', { selector: 'strong' }));
		expect(screen.getAllByRole('link', { name: 'Open Swarm probe helper session' }).some((link) => link.getAttribute('href') === '/?session=worker-c00')).toBe(true);
		expect(screen.queryByRole('button', { name: 'Cancel workflow' })).toBeNull();

		const replay = reduceAll([...fixture.launch, ...fixture.finish]);
		const folded = buildDisplayItems(replay.messages, { groupingEnabled: true, isStreaming: false, latestTurnUnsettled: false });
		const work = folded.find((item) => item.kind === 'work_log');
		expect(work?.kind).toBe('work_log');
		if (work?.kind === 'work_log') {
			expect(work.sequence.find((entry) => entry.message.toolId === fixture.launchId)?.hiddenWhenCollapsed).toBe(false);
		}
	});

	it('formats the aggregate wake notification without dumping its snapshot JSON', () => {
		const fixture = transcript('acceptance-notification');
		const replay = reduceAll([...fixture.launch, ...fixture.finish]);
		const message = replay.messages.find((row) => row.systemSubtype === 'task_notification')!;
		const { container } = render(SystemMessage, { message });
		expect(screen.getByText('Swarm')).toBeTruthy();
		expect(screen.getByText('11 of 11 tasks completed')).toBeTruthy();
		expect(screen.getByText('completed')).toBeTruthy();
		expect(container.textContent).not.toContain('"swarm"');
		expect(container.textContent).not.toContain('worker-c00');
	});
});
