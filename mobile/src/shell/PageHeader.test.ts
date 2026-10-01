import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet, tick } from 'svelte';
import type { Writable } from 'svelte/store';

vi.mock('$lib/stores/hotkeyPreferences.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { hotkeyPreferences: writable({ globalSttEnabled: false }) };
});
vi.mock('$lib/services/globalStt', async () => {
	const { writable } = await import('svelte/store');
	return {
		globalSttState: writable({ isRecording: false, isTranscribing: false, isPreparing: false }),
		triggerToggle: vi.fn()
	};
});

import PageHeader from './PageHeader.svelte';
import * as prefs from '$lib/stores/hotkeyPreferences.svelte';
import * as stt from '$lib/services/globalStt';

const hotkeyPreferences = prefs.hotkeyPreferences as unknown as Writable<{ globalSttEnabled: boolean }>;
const globalSttState = stt.globalSttState as unknown as Writable<Record<string, boolean>>;
const triggerToggle = stt.triggerToggle as unknown as ReturnType<typeof vi.fn>;

const button = (label: string) => createRawSnippet(() => ({ render: () => `<button type="button">${label}</button>` }));
const micButton = () => screen.queryByRole('button', { name: /voice input|Stop recording/ });

beforeEach(() => {
	hotkeyPreferences.set({ globalSttEnabled: false });
	globalSttState.set({ isRecording: false, isTranscribing: false, isPreparing: false });
	triggerToggle.mockClear();
});

afterEach(() => cleanup());

describe('PageHeader voice input', () => {
	it('hides the mic until global voice input is enabled', async () => {
		render(PageHeader);
		expect(micButton()).toBeNull();
		hotkeyPreferences.set({ globalSttEnabled: true });
		await tick();
		expect(screen.getByRole('button', { name: 'Start voice input' })).toBeTruthy();
	});

	it('hides the mic in a session view, where the composer owns voice input', () => {
		hotkeyPreferences.set({ globalSttEnabled: true });
		render(PageHeader, { props: { hideGlobalStt: true } });
		expect(micButton()).toBeNull();
	});

	it('toggles recording on tap and relabels itself while recording', async () => {
		hotkeyPreferences.set({ globalSttEnabled: true });
		render(PageHeader);
		await fireEvent.click(screen.getByRole('button', { name: 'Start voice input' }));
		expect(triggerToggle).toHaveBeenCalledTimes(1);
		globalSttState.set({ isRecording: true, isTranscribing: false, isPreparing: false });
		await tick();
		const stop = screen.getByRole('button', { name: 'Stop recording' });
		expect(stop.classList.contains('recording')).toBe(true);
	});

	it('marks the transcribing and preparing states on the mic', async () => {
		hotkeyPreferences.set({ globalSttEnabled: true });
		render(PageHeader);
		globalSttState.set({ isRecording: false, isTranscribing: true, isPreparing: false });
		await tick();
		expect(micButton()!.classList.contains('transcribing')).toBe(true);
		globalSttState.set({ isRecording: false, isTranscribing: false, isPreparing: true });
		await tick();
		expect(micButton()!.classList.contains('preparing')).toBe(true);
	});
});

describe('PageHeader action slots', () => {
	it('renders download in the left region and Git in the right region', () => {
		const { container } = render(PageHeader, {
			props: { leftActions: button('Download chat'), actions: button('Commit') }
		});
		expect(container.querySelector('.hdr-meta')!.textContent).toContain('Download chat');
		expect(container.querySelector('.hdr-right')!.textContent).toContain('Commit');
	});

	it('renders an empty header row with no actions', () => {
		const { container } = render(PageHeader);
		expect(container.querySelector('header.page-header')).toBeTruthy();
		expect(container.querySelectorAll('button')).toHaveLength(0);
	});
});
