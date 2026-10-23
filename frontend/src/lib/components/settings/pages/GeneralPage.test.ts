import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: transport, getErrorMessage: (_e: unknown, fallback: string) => fallback }));
vi.mock('$lib/stores/theme.svelte', async () => {
	const { writable } = await import('svelte/store');
	return {
		theme: { setTheme: vi.fn(), setPreset: vi.fn() },
		themePreference: writable('system'),
		currentPresetId: writable(null),
		themePresets: []
	};
});
vi.mock('$lib/stores/timezonePreference.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { timezonePreference: { ...writable('local'), load: vi.fn(async () => {}), setTimezone: vi.fn() } };
});

import GeneralPage from './GeneralPage.svelte';
import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
import { saveStatus } from '$lib/settings/save-status.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';

const switchFor = (name: string) => screen.getByRole('switch', { name }) as HTMLButtonElement;

beforeEach(() => {
	transport.get.mockResolvedValue({});
	transport.put.mockResolvedValue({});
	chatPreferences.reset();
	saveStatus.reset();
});
afterEach(cleanup);

describe('General page', () => {
	it('renders a row for every General entry', async () => {
		await assertCategoryCoverage('general', () => render(GeneralPage).container);
	});

	it('groups the chat preferences under Conversation and Thinking and tools', () => {
		render(GeneralPage);
		const groups = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent?.trim());
		expect(groups).toEqual(['Appearance', 'Conversation', 'Thinking and tools', 'Region']);
		expect(screen.getByText('Uses an extra model call')).toBeTruthy();
	});

	it('saves a switch through the chat preferences store and shows the Saved tick', async () => {
		render(GeneralPage);
		const stamps = switchFor('Show timestamps');
		expect(stamps.getAttribute('aria-checked')).toBe('false');
		await fireEvent.click(stamps);
		expect(chatPreferences.current.showMessageTimestamps).toBe(true);
		expect(stamps.getAttribute('aria-checked')).toBe('true');
		expect(saveStatus.visible).toBe(true);
	});

	it('marks a changed row and resets it to the default', async () => {
		render(GeneralPage);
		await fireEvent.click(switchFor('Group tool calls'));
		expect(chatPreferences.current.groupToolCalls).toBe(false);
		await fireEvent.click(screen.getByRole('button', { name: 'Reset Group tool calls to default' }));
		expect(chatPreferences.current.groupToolCalls).toBe(true);
	});

	it('disables the thinking options while thinking blocks are hidden', async () => {
		render(GeneralPage);
		expect(switchFor('Expand thinking by default').disabled).toBe(false);
		await fireEvent.click(switchFor('Show thinking blocks'));
		expect(chatPreferences.current.showThinkingBlocks).toBe(false);
		expect(switchFor('Expand thinking by default').disabled).toBe(true);
		expect(switchFor('Show thinking summary').disabled).toBe(true);
		expect(switchFor('Expand tool calls by default').disabled).toBe(false);
	});

	it('follows a change that arrives through the store', async () => {
		render(GeneralPage);
		chatPreferences.updatePreference('compactMessageView', true);
		await Promise.resolve();
		expect(switchFor('Compact message view').getAttribute('aria-checked')).toBe('true');
	});
});
