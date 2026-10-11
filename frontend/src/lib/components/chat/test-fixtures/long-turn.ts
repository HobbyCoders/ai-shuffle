/**
 * The long turn from the transcript-organization mockup scenario (spec §10):
 * two steers, two workers, one question, one failed tool and a Monitor that
 * keeps running while the turn is live. Shared by the builder tests
 * (work-log.test.ts) and the rendered-transcript tests, so both assert on the
 * same turn.
 */
import type { ChatMessage } from '$lib/stores/types';

export function longTurn() {
	let seq = 0;
	const msg = (over: Partial<ChatMessage>): ChatMessage =>
		({ id: `lt-${seq++}`, role: 'assistant', content: '', ...over }) as ChatMessage;
	const user = (content: string, timestamp: string, turnId: string) => msg({ role: 'user', content, timestamp, turnId });
	const text = (content: string, timestamp: string) => msg({ type: 'text', content, timestamp });
	const tool = (over: Partial<ChatMessage>) =>
		msg({ type: 'tool_use', toolName: 'Bash', toolStatus: 'complete', toolInput: { command: 'ls' }, ...over });
	const subagent = (over: Partial<ChatMessage>) =>
		msg({ type: 'subagent', agentName: 'General Purpose', agentStatus: 'completed', ...over });

	const prompt = user('Audit the deploy pipeline', '2026-10-10T20:00:00.000Z', 'turn-1');
	const read = tool({ toolName: 'Read', toolInput: { file_path: 'deploy.yml' }, timestamp: '2026-10-10T20:00:10.000Z' });
	const finding = text('Staging and prod share one token:\n- `CF_API_TOKEN` in both jobs', '2026-10-10T20:00:15.000Z');
	const grep = tool({ toolName: 'Grep', toolInput: { pattern: 'wrangler' }, timestamp: '2026-10-10T20:00:20.000Z' });
	const interim = text('Now checking the workflow.', '2026-10-10T20:01:00.000Z');
	const monitor = tool({ toolName: 'Monitor', toolId: 'monitor-1', toolInput: { command: 'tail -f deploy.log' }, timestamp: '2026-10-10T20:01:30.000Z' });
	const worker = subagent({ toolId: 'task-1', agentStatus: 'running', timestamp: '2026-10-10T20:02:00.000Z' });
	const question = tool({ toolName: 'AskUserQuestion', toolId: 'ask-1', toolInput: { questions: [] }, timestamp: '2026-10-10T20:03:00.000Z' });
	const steer = user('Skip staging, prod only', '2026-10-10T20:19:00.000Z', 'turn-1');
	const ack = text('Understood, prod only.', '2026-10-10T20:19:05.000Z');
	const report = msg({
		role: 'system', type: 'system', systemSubtype: 'agent_message',
		content: 'Worker report', systemData: { sender: 'worker', content: 'Pipeline uses wrangler 3.' },
		timestamp: '2026-10-10T20:25:00.000Z'
	});
	const build = tool({ toolName: 'Bash', toolInput: { command: 'npm run build' }, timestamp: '2026-10-10T20:20:00.000Z' });
	const failed = tool({ toolName: 'Edit', toolStatus: 'error', toolInput: { file_path: 'deploy.yml' }, timestamp: '2026-10-10T20:21:00.000Z' });
	const retry = tool({ toolName: 'Edit', toolInput: { file_path: 'deploy.yml' }, timestamp: '2026-10-10T20:21:30.000Z' });
	const worker2 = subagent({ toolId: 'task-2', agentName: 'Reviewer', agentStatus: 'running', timestamp: '2026-10-10T20:22:00.000Z' });
	const steer2 = user('Also note the token rotation date', '2026-10-10T20:23:00.000Z', 'turn-1');
	const grep2 = tool({ toolName: 'Grep', toolInput: { pattern: 'rotate' }, timestamp: '2026-10-10T20:23:30.000Z' });
	const final = text('Prod deploy is wired through wrangler 3.', '2026-10-10T20:30:00.000Z');
	const messages = [prompt, read, finding, grep, interim, monitor, worker, question, steer, ack, build, failed, retry, worker2, steer2, grep2, report, final];
	return { messages, prompt, read, finding, grep, interim, monitor, worker, question, steer, ack, build, failed, retry, worker2, steer2, grep2, report, final };
}
