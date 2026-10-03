/**
 * Tests for the one-shot deep-link itemId added to the layout store.
 *
 * Spotlight can deep-link to an agent editor via
 * openFeaturePage(type, itemId). Because activeView persists to localStorage,
 * the itemId must be one-shot: never written to storage (so a reload doesn't
 * re-open the editor) and clearable once the target page consumes it.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';

const STORAGE_KEY = 'ai-shuffle-layout';

describe('layout store — one-shot deep-link itemId', () => {
	beforeEach(() => {
		localStorage.clear();
		vi.resetModules();
	});

	it('openFeaturePage stores the itemId in memory', async () => {
		const { layout } = await import('./layout.svelte');
		layout.openFeaturePage('profile', 'prof-1');
		expect(layout.getState().activeView).toMatchObject({
			type: 'profile',
			itemId: 'prof-1'
		});
	});

	it('does not persist the itemId to localStorage', async () => {
		const { layout } = await import('./layout.svelte');
		layout.openFeaturePage('profile', 'agent-9');

		const raw = localStorage.getItem(STORAGE_KEY);
		expect(raw).toBeTruthy();
		const parsed = JSON.parse(raw!);
		expect(parsed.activeView.type).toBe('profile');
		expect('itemId' in parsed.activeView).toBe(false);
	});

	it('clearFeatureItemId consumes the itemId exactly once', async () => {
		const { layout } = await import('./layout.svelte');
		layout.openFeaturePage('profile', 'prof-2');
		expect(layout.getState().activeView?.itemId).toBe('prof-2');

		layout.clearFeatureItemId();
		expect(layout.getState().activeView?.itemId).toBeNull();

		// Idempotent: a second clear is a no-op (no throw, still null).
		layout.clearFeatureItemId();
		expect(layout.getState().activeView?.itemId).toBeNull();
	});

	it('a reload from persisted storage does not restore the itemId', async () => {
		const first = await import('./layout.svelte');
		first.layout.openFeaturePage('profile', 'prof-3');

		// Fresh module evaluation reads the persisted (itemId-stripped) storage.
		vi.resetModules();
		const second = await import('./layout.svelte');
		expect(second.layout.getState().activeView?.type).toBe('profile');
		expect(second.layout.getState().activeView?.itemId ?? null).toBeNull();
	});

	it('openFeaturePage without an itemId leaves it null', async () => {
		const { layout } = await import('./layout.svelte');
		layout.openFeaturePage('profile');
		expect(layout.getState().activeView?.itemId ?? null).toBeNull();
	});

	it("migrates persisted ViewType 'chat' to 'session' on load", async () => {
		localStorage.setItem(
			STORAGE_KEY,
			JSON.stringify({
				sidebarCollapsed: false,
				activeView: { type: 'chat', sessionId: 'sess-1' },
				sidebarWidth: 390
			})
		);
		const { layout } = await import('./layout.svelte');
		expect(layout.getState().activeView).toMatchObject({
			type: 'session',
			sessionId: 'sess-1'
		});
	});

	it("openChat writes ViewType 'session'", async () => {
		const { layout } = await import('./layout.svelte');
		layout.openChat('sess-2');
		expect(layout.getState().activeView).toMatchObject({
			type: 'session',
			sessionId: 'sess-2'
		});
	});
});
