/**
 * The phone controls panel keeps Tab inside itself (#1407). Its "Context and
 * usage" page renders the real ContextUsageBar, whose "Hidden providers" list
 * is a <details>: the summary is a native tab stop, and its restore buttons are
 * not while it is closed. The wrap must see the page the way the browser does.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import type { ComposerSheetProps } from '../composer-shell';
import type { PreferenceAdapter } from '$lib/services/preference-sync';
import { EFFORT_OPTIONS } from '$lib/constants/model-effort';
import { rateLimit } from '$lib/stores/rate-limit';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import '$lib/test-stubs/composer-dom';

const mocks = vi.hoisted(() => {
	let val = {
		sessionId: null, isStreaming: false, executionMode: null, modelOverride: null, contextWindowChoice: null,
		profile: null, contextUsed: null, totalTokensIn: 0, totalTokensOut: 0, totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0, liveContextUsage: null, messages: [], activeTasks: []
	};
	return {
		adapters: new Map<string, PreferenceAdapter>(),
		activeSession: {
			get current() { return val; },
			subscribe(fn: (v: typeof val) => void) { fn(val); return () => {}; },
			set(v: typeof val) { val = v; }
		}
	};
});

vi.mock('$lib/services/preferences', () => ({ preferenceSync: {
	register: (key: string, adapter: PreferenceAdapter) => mocks.adapters.set(key, adapter), registerPrefix: vi.fn(), save: vi.fn()
} }));
vi.mock('$lib/stores/active-session', async (importOriginal) => ({
	...(await importOriginal<Record<string, unknown>>()), activeSession: mocks.activeSession
}));
vi.mock('$lib/api/sessions', async (importOriginal) => ({
	...(await importOriginal<Record<string, unknown>>()), getContextUsage: vi.fn().mockResolvedValue({ available: false, reason: 'not_connected' })
}));
vi.mock('$lib/stores/watches', async () => {
	const { writable } = await import('svelte/store');
	return { watches: { ...writable({ list: [], preferredWorkerId: null }), track: () => () => {} } };
});
vi.mock('$app/navigation', () => ({ pushState: () => { throw new Error('No router in this component test'); } }));
// Only the usage page is under test; the other pages' hosted components are not.
vi.mock('$lib/components/chat/ContextPillBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/RunningTasksList.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/layout/ChatExportActions.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/settings/ModelPickerPanel.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));

function props(): ComposerSheetProps {
	return {
		open: true, onClose: vi.fn(), modelOptions: [], modelValue: 'sonnet', modelLabel: 'Sonnet', onModelSelect: vi.fn(),
		contextChoice: 'standard', onContextChoiceSelect: vi.fn(),
		effortOptions: EFFORT_OPTIONS.filter((o) => ['low', 'high'].includes(o.value)), effortValue: null,
		effortKnown: true, effortDefaultValue: 'high', onEffortSelect: vi.fn(),
		permissionOptions: [{ value: 'default', label: 'Ask', description: 'Ask first' }],
		permissionValue: 'default', onPermissionSelect: vi.fn()
	};
}

const panel = () => document.querySelector<HTMLElement>('.mcp')!;
function tab(shiftKey = false) {
	const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey, bubbles: true, cancelable: true });
	(document.activeElement as HTMLElement).dispatchEvent(event);
	return event.defaultPrevented;
}

beforeEach(() => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
	rateLimit.__resetForTests();
	rateLimit.applySnapshot({ available: true, windows: [], providers: [
		{ provider_id: 'api', provider_type: 'fal', provider_name: 'fal.ai', windows: [] }
	] });
	mocks.adapters.get('context_usage_hidden_providers')!.apply({ 'provider:api': true });
});
afterEach(() => {
	cleanup();
	mocks.adapters.get('context_usage_hidden_providers')!.apply(undefined);
	vi.unstubAllGlobals();
});

describe('phone controls panel: Tab on the usage page with a hidden provider (#1407)', () => {
	async function openUsagePage() {
		render(MobileComposerControlsSheet, props());
		await fireEvent.click(screen.getByRole('button', { name: /^Context and usage:/ }));
		await tick();
		const page = panel().querySelector<HTMLElement>('[data-page="usage"]')!;
		expect(page.inert).toBe(false);
		const details = page.querySelector<HTMLDetailsElement>('details.hidden-providers')!;
		expect(details.open).toBe(false);
		return { details, summary: details.querySelector<HTMLElement>('summary')!, back: panel().querySelector<HTMLElement>('.mcp-back')! };
	}

	it('wraps Tab from the summary, the last stop, and Shift+Tab from Back onto the summary, not a hidden restore button', async () => {
		const { summary, back } = await openUsagePage();
		expect(screen.getByRole('button', { name: 'Show fal.ai usage', hidden: true }).closest('details')).toBe(summary.parentElement);

		summary.focus();
		expect(tab()).toBe(true);
		expect(document.activeElement).toBe(back);

		expect(tab(true)).toBe(true);
		expect(document.activeElement).toBe(summary);
	});

	it('treats the restore buttons as the last stops once Hidden providers is open', async () => {
		const { details, summary, back } = await openUsagePage();
		details.open = true;
		const restore = screen.getByRole('button', { name: 'Show fal.ai usage' });

		summary.focus();
		expect(tab()).toBe(false);
		restore.focus();
		expect(tab()).toBe(true);
		expect(document.activeElement).toBe(back);
		expect(tab(true)).toBe(true);
		expect(document.activeElement).toBe(restore);
	});
});
