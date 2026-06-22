/**
 * Tests for activeTasks threading through the session-poller (Task 1).
 *
 * The /live response carries a top-level `activeTasks: Task[]` field. The poller
 * must parse it (camelCase, tolerant of a missing field / snake_case fallback)
 * and surface it on the onPollApplied callback + loadSession result.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionPoller } from './session-poller';
import { sessionStateManager } from './session-state.svelte';
import type { PollAppliedData } from './session-poller';

const SESSION_ID = 'session-active-tasks';

function makeResponse(body: Record<string, unknown>): Response {
	return {
		ok: true,
		status: 200,
		json: async () => ({
			last_update_id: 1,
			is_streaming: false,
			messages: [],
			initial_message_count: 0,
			usage: {},
			...body,
		}),
	} as unknown as Response;
}

describe('session-poller · activeTasks threading', () => {
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
	});

	afterEach(() => {
		sessionPoller.stopAll();
		sessionStateManager.removeSession(SESSION_ID);
		sessionPoller.onPollApplied(() => {});
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	it('parses and surfaces activeTasks on loadSession', async () => {
		fetchMock.mockResolvedValueOnce(
			makeResponse({
				activeTasks: [
					{
						id: 't1',
						subject: 'Build the project',
						status: 'running',
						kind: 'shell',
						summary: 'npm run build',
						startedAt: '2026-06-22T10:00:00.000Z',
						updatedAt: '2026-06-22T10:00:05.000Z',
					},
					{
						id: 't2',
						subject: 'Explore the codebase',
						status: 'running',
						kind: 'agent',
						summary: null,
						startedAt: '2026-06-22T10:00:01.000Z',
						updatedAt: '2026-06-22T10:00:06.000Z',
					},
				],
			})
		);

		const result = await sessionPoller.loadSession(SESSION_ID);
		expect(result).not.toBeNull();
		expect(result!.activeTasks).toHaveLength(2);
		expect(result!.activeTasks[0]).toMatchObject({ id: 't1', kind: 'shell', subject: 'Build the project' });
		expect(result!.activeTasks[1]).toMatchObject({ id: 't2', kind: 'agent', summary: null });
	});

	it('defaults to [] when activeTasks is absent', async () => {
		fetchMock.mockResolvedValueOnce(makeResponse({}));
		const result = await sessionPoller.loadSession(SESSION_ID);
		expect(result).not.toBeNull();
		expect(result!.activeTasks).toEqual([]);
	});

	it('drops malformed task rows (missing id) and normalizes unknown status/kind', async () => {
		fetchMock.mockResolvedValueOnce(
			makeResponse({
				activeTasks: [
					{ subject: 'no id — dropped', status: 'running', kind: 'shell' },
					{ id: 't3', subject: 'odd values', status: 'weird', kind: 'spaceship', startedAt: '', updatedAt: '' },
				],
			})
		);
		const result = await sessionPoller.loadSession(SESSION_ID);
		expect(result!.activeTasks).toHaveLength(1);
		// Unknown status/kind normalize to safe defaults rather than rendering junk.
		expect(result!.activeTasks[0]).toMatchObject({ id: 't3', status: 'running', kind: 'shell' });
	});

	it('surfaces activeTasks on the onPollApplied callback', async () => {
		let applied: PollAppliedData | null = null;
		sessionPoller.onPollApplied((sid, data) => {
			if (sid === SESSION_ID) applied = data;
		});

		fetchMock.mockResolvedValueOnce(
			makeResponse({
				last_update_id: 5,
				activeTasks: [
					{ id: 'tt', subject: 's', status: 'running', kind: 'agent', summary: null, startedAt: '', updatedAt: '' },
				],
			})
		);
		await sessionPoller.loadSession(SESSION_ID);
		expect(applied).not.toBeNull();
		expect(applied!.activeTasks).toHaveLength(1);
		expect(applied!.activeTasks[0].id).toBe('tt');
	});
});
