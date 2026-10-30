import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Writable } from 'svelte/store';
import { activeSession, type ActiveSessionState } from '$lib/stores/active-session';
import { runningTasksOpen } from '$lib/stores/expansion-preferences';
import { openLightbox, closeLightbox } from '$lib/stores/lightbox';
import Harness from './test-fixtures/TranscriptSafetyHarness.svelte';

const mocks = vi.hoisted(() => ({ rewind: vi.fn(), report: vi.fn(), stop: vi.fn(), save: vi.fn(), send: vi.fn() }));
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	return { activeSession: Object.assign(writable({}), { sendMessage: mocks.send }) };
});
vi.mock('$lib/api/client', async (original) => ({ ...(await original<object>()), executeRewind: mocks.rewind }));
vi.mock('$lib/stores/support-report.svelte', () => ({ canReportAiContent: () => true, reportAiContent: mocks.report }));
vi.mock('$lib/services/preferences', () => ({ preferenceSync: { register: vi.fn(), registerPrefix: vi.fn(), save: vi.fn() } }));
vi.mock('$lib/stores/swarms', async () => {
	const { readable } = await import('svelte/store');
	return { swarms: { subscribe: readable({ bySession: {}, errors: {} }).subscribe, hydrate: vi.fn(async () => {}) } };
});
vi.mock('$lib/stores/usage-target', async () => {
	const { readable } = await import('svelte/store');
	return { usageTargetDeviceName: readable('Test worker') };
});
vi.mock('$lib/api/sessions', () => ({ stopSessionTask: mocks.stop }));
vi.mock('$lib/api/watches', () => ({ listWatches: vi.fn(async () => ({ watches: [], preferredWorkerId: null })) }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: vi.fn(), openSessionLink: vi.fn() }));
vi.mock('$lib/stores/generation-choices.svelte', () => ({
	generationChoices: {
		load: vi.fn(async () => {}), remembered: {}, worker: null, save: mocks.save,
		quote: vi.fn(async () => ({ currency: 'USD', unit: 'item', amount: '0', total: '0', status: 'included' }))
	}
}));

const live = activeSession as unknown as Writable<ActiveSessionState>;
function state(id: string, loading = false, taskCount = 0): ActiveSessionState {
	return {
		sessionId: id, messages: [{ id: `${id}-prompt`, role: 'user', type: 'text', content: `${id} prompt` }],
		pendingPeerMessages: [], pendingPermissions: [], isLoadingSession: loading, isRestoringSession: false,
		isStreaming: taskCount > 0,
		activeTasks: Array.from({ length: taskCount }, (_, i) => ({
			id: `shell-${i}`, kind: 'shell', status: 'running', subject: `Retained shell task ${i}`,
			startedAt: new Date().toISOString(), updatedAt: new Date().toISOString()
		}))
	} as unknown as ActiveSessionState;
}
beforeEach(() => {
	vi.clearAllMocks(); mocks.rewind.mockReset(); mocks.save.mockReset(); mocks.send.mockReset();
	live.set(state('A')); runningTasksOpen.set(true); closeLightbox();
});
afterEach(() => { cleanup(); closeLightbox(); });

async function openRewind() {
	await fireEvent.click(screen.getByTitle('Rewind to before this message'));
	return screen.getByRole('button', { name: /^Rewind$/ });
}

describe('retained transcript action lifetime', () => {
	it('closes a body-portaled rewind confirmation on divergence and never reopens it over B', async () => {
		render(Harness);
		const oldConfirm = await openRewind();
		expect(screen.getByRole('dialog')).toBeTruthy();
		live.set(state('B', true)); await tick();
		expect(screen.queryByRole('dialog')).toBeNull();
		await fireEvent.click(oldConfirm);
		expect(mocks.rewind).not.toHaveBeenCalled();
		live.set(state('B')); await tick();
		expect(screen.queryByRole('dialog')).toBeNull();
		await openRewind();
		expect(screen.getByRole('dialog')).toBeTruthy();
	});

	it('does not deliver a late outgoing rewind result to the incoming composer callback', async () => {
		let finish!: (value: unknown) => void;
		mocks.rewind.mockReturnValue(new Promise(resolve => { finish = resolve; }));
		const complete = vi.fn();
		render(Harness, { onRewindComplete: complete });
		await fireEvent.click(await openRewind());
		expect(mocks.rewind).toHaveBeenCalledWith('A', 'checkpoint-a', false, 'user');
		live.set(state('B', true)); await tick();
		live.set(state('B')); await tick();
		finish({ success: true, warning: 'A warning', unsent_restored: true, restored_prompt: 'A prompt' });
		await tick(); await tick();
		expect(complete).not.toHaveBeenCalled();
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('closes the body-portaled message menu without reporting an outgoing reply', async () => {
		render(Harness, { role: 'assistant' });
		await fireEvent.click(screen.getByRole('button', { name: 'More actions' }));
		expect(screen.getByRole('menu', { name: 'Message actions' })).toBeTruthy();
		live.set(state('B', true)); await tick();
		expect(screen.queryByRole('menu', { name: 'Message actions' })).toBeNull();
		expect(mocks.report).not.toHaveBeenCalled();
	});

	it('closes the portaled media/copy-export gallery when the target changes', async () => {
		render(Harness, { mode: 'media' });
		openLightbox('image', '/test-image.png'); await tick();
		expect(screen.getByRole('dialog', { name: 'Media preview' })).toBeTruthy();
		live.set(state('B', true)); await tick();
		expect(screen.queryByRole('dialog', { name: 'Media preview' })).toBeNull();
	});

	it('does not send A’s generation pick into B after its asynchronous save completes', async () => {
		let finish!: (value: unknown) => void;
		mocks.save.mockReturnValue(new Promise(resolve => { finish = resolve; }));
		render(Harness, { mode: 'generation-save' });
		const use = screen.getByRole('button', { name: /^Use this/ }) as HTMLButtonElement;
		await waitFor(() => expect(use.disabled).toBe(false));
		await fireEvent.click(use);
		expect(mocks.save).toHaveBeenCalledOnce();
		live.set(state('B', true)); await tick();
		live.set(state('B')); await tick();
		finish(null); await tick(); await tick();
		expect(mocks.send).not.toHaveBeenCalled();
	});

	it('keeps an answered generation card collapsed using A’s presentation history while B is loading', async () => {
		const outgoing = state('A');
		outgoing.messages = [
			{ id: 'tool-a', role: 'assistant', type: 'tool_use', toolId: 'generation-a', content: '' },
			{ id: 'reply-a', role: 'user', type: 'text', content: 'Use the fast model' }
		] as ActiveSessionState['messages'];
		live.set(outgoing);
		render(Harness, { mode: 'generation' });
		const answered = screen.getByText('Answered in the chat');
		live.set(state('B', true)); await tick();
		expect(answered.isConnected).toBe(true);
		expect(screen.getByText('Answered in the chat')).toBe(answered);
		live.set(state('B')); await tick();
		expect(screen.queryByText('Answered in the chat')).toBeNull();
	});

	it('keeps a streaming A’s shell rows mounted while the live B has no tasks', async () => {
		live.set(state('A', false, 1));
		render(Harness, { mode: 'tasks' });
		const row = screen.getByText('Retained shell task 0');
		live.set(state('B', true)); await tick();
		expect(row.isConnected).toBe(true);
		expect(screen.getByText('Retained shell task 0')).toBe(row);
		expect(screen.getByTestId('tasks-toggle').textContent).toContain('1 running');
		live.set(state('B')); await tick();
		expect(screen.queryByText('Retained shell task 0')).toBeNull();
	});

	it('closes the portaled task sheet while preserving the inline running rows', async () => {
		live.set(state('A', false, 7));
		render(Harness, { mode: 'tasks' });
		await fireEvent.click(screen.getByRole('button', { name: 'Show all' }));
		expect(screen.getByRole('dialog', { name: 'Running tasks' })).toBeTruthy();
		live.set(state('B', true)); await tick();
		expect(screen.queryByRole('dialog', { name: 'Running tasks' })).toBeNull();
		expect(screen.getByText('Retained shell task 0')).toBeTruthy();
		expect(mocks.stop).not.toHaveBeenCalled();
	});
});
