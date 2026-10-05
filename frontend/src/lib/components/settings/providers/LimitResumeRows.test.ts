/**
 * A provider's usage-limit switches (#1831): Resume when the limit resets
 * (off by default) and Compact before resuming (on, and only usable while
 * resume is on). Each change is one PUT for that provider.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import LimitResumeRows from './LimitResumeRows.svelte';

let stored: Record<string, { resume_on_reset: boolean; compact_before_resume: boolean }>;
const puts: Array<{ url: string; body: unknown }> = [];
const DEFAULTS = { resume_on_reset: false, compact_before_resume: true };

beforeEach(() => {
	stored = { claude: { ...DEFAULTS }, 'prov-1': { ...DEFAULTS } };
	puts.length = 0;
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (init?.method === 'PUT') {
			const body = JSON.parse(String(init.body));
			puts.push({ url, body });
			const key = decodeURIComponent(url.split('/').pop() ?? '');
			stored[key] = { ...stored[key], ...body };
		}
		return new Response(JSON.stringify({ providers: stored, defaults: DEFAULTS }), {
			status: 200, headers: { 'Content-Type': 'application/json' }
		});
	}));
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

it('starts off, with compact on but unusable until resume is on', async () => {
	render(LimitResumeRows, { props: { providerKey: 'prov-1' } });
	const resume = await screen.findByRole('switch', { name: 'Resume when the limit resets' });
	await waitFor(() => expect(resume.hasAttribute('disabled')).toBe(false));
	expect(resume.getAttribute('aria-checked')).toBe('false');
	const compact = screen.getByRole('switch', { name: 'Compact before resuming' });
	expect(compact.getAttribute('aria-checked')).toBe('true');
	expect(compact.hasAttribute('disabled')).toBe(true);
});

it('saves each switch for its own provider', async () => {
	render(LimitResumeRows, { props: { providerKey: 'prov-1' } });
	const resume = await screen.findByRole('switch', { name: 'Resume when the limit resets' });
	await waitFor(() => expect(resume.hasAttribute('disabled')).toBe(false));
	await fireEvent.click(resume);
	await waitFor(() => expect(puts).toHaveLength(1));
	expect(puts[0].url).toMatch(/\/settings\/limit-resume\/prov-1$/);
	expect(puts[0].body).toEqual({ resume_on_reset: true });
	const compact = screen.getByRole('switch', { name: 'Compact before resuming' });
	await waitFor(() => expect(compact.hasAttribute('disabled')).toBe(false));
	await fireEvent.click(compact);
	await waitFor(() => expect(puts).toHaveLength(2));
	expect(puts[1].body).toEqual({ compact_before_resume: false });
	expect(stored.claude).toEqual(DEFAULTS);
});
