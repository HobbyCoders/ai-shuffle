import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

// jsdom has no matchMedia; the theme store reads it at import.
vi.hoisted(() => {
	window.matchMedia ??= ((query: string) => ({
		matches: false, media: query, onchange: null,
		addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
		dispatchEvent: () => false
	})) as unknown as typeof window.matchMedia;
});
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import DemoPage from './+page.svelte';
import { DEMO_REPLY, samples } from '../../demo/samples';

const fetchSpy = vi.fn();
const WebSocketSpy = vi.fn();

beforeAll(() => {
	vi.stubGlobal('fetch', fetchSpy);
	vi.stubGlobal('WebSocket', WebSocketSpy);
});
afterEach(() => cleanup());
afterAll(() => vi.unstubAllGlobals());

const sendButton = () => screen.getByRole('button', { name: 'Send locally' }) as HTMLButtonElement;
const draft = () => screen.getByLabelText('Try a local message') as HTMLTextAreaElement;
const messageList = () => screen.getByRole('region', { name: 'Sample messages' });

describe('offline demo page', () => {
	it('opens on the first sample with its messages and states it is offline', () => {
		render(DemoPage);
		expect(screen.getByRole('heading', { name: 'Offline demo' })).toBeTruthy();
		const tabs = screen.getAllByRole('button', { pressed: true });
		expect(tabs.map((b) => b.textContent)).toEqual([samples[0].title]);
		expect(messageList().textContent).toContain('What does this phone app do?');
		expect(screen.getByRole('status').textContent).toContain('No network request is made');
	});

	it('switches samples and marks the selected one pressed', async () => {
		render(DemoPage);
		await fireEvent.click(screen.getByRole('button', { name: samples[1].title }));
		expect(screen.getByRole('button', { name: samples[1].title }).getAttribute('aria-pressed')).toBe('true');
		expect(screen.getByRole('button', { name: samples[0].title }).getAttribute('aria-pressed')).toBe('false');
		expect(messageList().textContent).toContain('Show the example checklist.');
	});

	it('disables send for an empty or blank draft', async () => {
		render(DemoPage);
		expect(sendButton().disabled).toBe(true);
		await fireEvent.input(draft(), { target: { value: '   ' } });
		expect(sendButton().disabled).toBe(true);
	});

	it('adds the message and the preset reply locally, clears the draft and makes no request', async () => {
		render(DemoPage);
		await fireEvent.input(draft(), { target: { value: 'hello there' } });
		expect(sendButton().disabled).toBe(false);
		await fireEvent.click(sendButton());
		expect(messageList().textContent).toContain('hello there');
		expect(messageList().textContent).toContain(DEMO_REPLY.slice(0, 30));
		expect(draft().value).toBe('');
		expect(screen.getByRole('status').textContent).toBe('Preset reply added. Nothing was sent to a worker.');
		expect(fetchSpy).not.toHaveBeenCalled();
		expect(WebSocketSpy).not.toHaveBeenCalled();
	});

	it('resets the sample, dropping local messages and the notice', async () => {
		render(DemoPage);
		await fireEvent.input(draft(), { target: { value: 'temporary' } });
		await fireEvent.click(sendButton());
		await fireEvent.click(screen.getByRole('button', { name: 'Reset sample' }));
		expect(messageList().textContent).not.toContain('temporary');
		expect(screen.getByRole('status').textContent).toContain('No network request is made');
	});
});
