/**
 * How the source side learns a hand-off finished (#2304 on the #2308 contract).
 * The `moved` record is written on the destination, so the source never sees
 * it. Two separate paths end the hand-off row there, and each is tested end to
 * end here, from what the server sends to what the row shows:
 *
 * - Live: the source worker publishes `user_session_updated` with the new
 *   `owner_device_id` on the GLOBAL feed (app/network/handoff.py
 *   `_announce_owner`). It is patched into the persisted session row, and the
 *   row now owned by the target ends the move.
 * - Reload: no frame was seen. The persisted session row (sessions list / GET
 *   session; `/live` carries no `owner_device_id`) names the target, and the
 *   routed `GET /sessions/{id}/handoff` reports nothing pending.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import type { ChatMessage } from './types';

const mocks = vi.hoisted(() => ({ rows: [] as Record<string, unknown>[], getQueuedMove: vi.fn() }));

vi.mock('./session/ws-connection', () => ({
	createWsConnection: () => ({
		connect: vi.fn(),
		disconnect: vi.fn(),
		send: vi.fn(() => true),
		isConnected: vi.fn(() => false),
		resetLastMessageTime: vi.fn(),
		resetReconnectAttempts: vi.fn(),
		destroy: vi.fn(),
	}),
}));

vi.mock('$lib/api/client', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/client')>();
	return {
		...actual,
		api: {
			...actual.api,
			get: vi.fn(async (url: string) => {
				if (url.startsWith('/sessions/cursor')) {
					return { items: mocks.rows, next_cursor: null, next_cursor_id: null, has_more: false };
				}
				if (url === '/cluster/projects') return { projects: [] };
				return [];
			}),
		},
	};
});

vi.mock('$lib/api/sessions', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/sessions')>()),
	getQueuedMove: mocks.getQueuedMove,
}));

import { handleGlobalUserEvent } from './active-session';
import { appData, sessions } from './app-data.svelte';
import { handoffPending, refreshHandoffPending, resetHandoffPending } from './handoff-pending';
import { resetUserEventDedupe } from '$lib/services/user-event-dedupe';
import { handoffEpisodes, resetOwnerTransitions, settleByOwner } from '$lib/utils/handoff-episodes';
import { pendingHandoff } from '$lib/components/chat/pending-handoff';

const SID = 'chat-moving';

/** The hand-off X to DesktopWorker, already sending, as the source transcript holds it. */
const transcript: ChatMessage[] = [
	{
		id: 'q', role: 'system', type: 'system', content: 'Move to DesktopWorker queued for when this turn ends.', systemSubtype: 'handoff',
		systemData: { status: 'queued', worker: 'DesktopWorker', by: 'owner', handoff_id: 'X' }
	},
	{
		id: 's', role: 'system', type: 'system', content: 'Moving this chat to DesktopWorker.', systemSubtype: 'handoff',
		systemData: { status: 'started', worker: 'DesktopWorker', handoff_id: 'X' }
	},
];

/** What MessageArea, ContextPillBar and ChatInput each compute from the stores. */
function rowView() {
	const row = get(sessions).find((s) => s.id === SID) ?? null;
	const owner = row ? { deviceId: row.owner_device_id, deviceName: row.device_name } : null;
	const view = get(handoffPending)[SID];
	return {
		row,
		phase: settleByOwner(handoffEpisodes(transcript), owner, SID, view).byAnchor.get(transcript[0])?.phase,
		chip: pendingHandoff(transcript, owner, SID, view),
	};
}

beforeEach(() => {
	resetOwnerTransitions();
	resetHandoffPending();
	resetUserEventDedupe();
	mocks.getQueuedMove.mockReset();
});

afterEach(() => {
	vi.useRealTimers();
});

describe('hand-off completion on the source side', () => {
	it('live: the global user_session_updated frame patches the row and settles the hand-off row', async () => {
		mocks.rows = [{ id: SID, title: 'Moving', owner_device_id: 'dev_unraid', instance_id: 'dev_unraid', device_name: 'ServerWorker', is_remote: false, updated_at: '2026-10-11T10:00:00Z' }];
		await appData.loadSessions();
		// While sending, the owning worker (still this one) reports X pending.
		mocks.getQueuedMove.mockResolvedValueOnce({
			busy: { running: false, tasks: [] },
			pending: { handoff_id: 'X', worker: 'DesktopWorker', target_id: 'dev_gaming', by: 'owner', moving: true },
		});
		await refreshHandoffPending(SID, 'sending');
		expect(rowView().phase).toBe('sending');
		expect(rowView().chip).toEqual({ worker: 'DesktopWorker', phase: 'moving' });

		vi.useFakeTimers();
		handleGlobalUserEvent({
			type: 'user_session_updated', session_id: SID,
			updates: { owner_device_id: 'dev_gaming', instance_id: 'dev_gaming', placement: { where: 'runner:dev_gaming' } },
		});

		const after = rowView();
		expect(after.row).toMatchObject({ owner_device_id: 'dev_gaming', instance_id: 'dev_gaming', placement: { where: 'runner:dev_gaming' } });
		expect(after.phase).toBe('done');
		expect(after.chip).toBeNull();
	});

	it('reload: the persisted row owned by the target and nothing pending settles the hand-off row', async () => {
		// A fresh page after the move: the row already names the target; no frame.
		mocks.rows = [{ id: SID, title: 'Moving', owner_device_id: 'dev_gaming', instance_id: 'dev_gaming', device_name: 'DesktopWorker', is_remote: true, updated_at: '2026-10-11T10:01:00Z' }];
		await appData.loadSessions();
		// Before the routed pending read answers, an owner equal to the target proves nothing.
		expect(rowView().phase).toBe('sending');

		mocks.getQueuedMove.mockResolvedValueOnce({ busy: { running: false, tasks: [] }, pending: null });
		await refreshHandoffPending(SID, 'reload');

		const after = rowView();
		expect(after.phase).toBe('done');
		expect(after.chip).toBeNull();
	});

	it('reload: the move still pending on the target keeps the row sending', async () => {
		mocks.rows = [{ id: SID, title: 'Moving', owner_device_id: 'dev_gaming', instance_id: 'dev_gaming', device_name: 'DesktopWorker', is_remote: true, updated_at: '2026-10-11T10:01:00Z' }];
		await appData.loadSessions();
		mocks.getQueuedMove.mockResolvedValueOnce({
			busy: { running: false, tasks: [] },
			pending: { handoff_id: 'X', worker: 'DesktopWorker', target_id: 'dev_gaming', by: 'owner', moving: true },
		});
		await refreshHandoffPending(SID, 'reload-pending');
		expect(rowView().phase).toBe('sending');
	});
});
