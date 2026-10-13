/**
 * Usage limits (#1831): Resume when the limit resets (off by default) and
 * Compact before resuming (on, and only usable while resume is on). One pair
 * of switches for every provider: each change is one PUT to the global
 * setting, never a per-provider one.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import LimitResumeRows from './LimitResumeRows.svelte';

const DEFAULTS = { resume_on_reset: false, compact_before_resume: true };
let stored: { resume_on_reset: boolean; compact_before_resume: boolean };
const puts: Array<{ url: string; body: unknown }> = [];

beforeEach(() => {
	stored = { ...DEFAULTS };
	puts.length = 0;
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (init?.method === 'PUT') {
			const body = JSON.parse(String(init.body));
			puts.push({ url, body });
			stored = { ...stored, ...body };
		}
		return new Response(JSON.stringify({ ...stored, defaults: DEFAULTS }), {
			status: 200, headers: { 'Content-Type': 'application/json' }
		});
	}));
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

it('starts off, with compact on but unusable until resume is on', async () => {
	render(LimitResumeRows);
	expect(screen.getByRole('heading', { level: 2 }).textContent?.trim()).toBe('Usage limits');
	const resume = await screen.findByRole('switch', { name: 'Resume when the limit resets' });
	await waitFor(() => expect(resume.hasAttribute('disabled')).toBe(false));
	expect(resume.getAttribute('aria-checked')).toBe('false');
	const compact = screen.getByRole('switch', { name: 'Compact before resuming' });
	expect(compact.getAttribute('aria-checked')).toBe('true');
	expect(compact.hasAttribute('disabled')).toBe(true);
});

it('saves each switch to the one global setting', async () => {
	render(LimitResumeRows);
	const resume = await screen.findByRole('switch', { name: 'Resume when the limit resets' });
	await waitFor(() => expect(resume.hasAttribute('disabled')).toBe(false));
	await fireEvent.click(resume);
	await waitFor(() => expect(puts).toHaveLength(1));
	expect(puts[0].url).toMatch(/\/settings\/limit-resume$/);
	expect(puts[0].body).toEqual({ resume_on_reset: true });
	const compact = screen.getByRole('switch', { name: 'Compact before resuming' });
	await waitFor(() => expect(compact.hasAttribute('disabled')).toBe(false));
	await fireEvent.click(compact);
	await waitFor(() => expect(puts).toHaveLength(2));
	expect(puts[1].url).toMatch(/\/settings\/limit-resume$/);
	expect(puts[1].body).toEqual({ compact_before_resume: false });
	expect(stored).toEqual({ resume_on_reset: true, compact_before_resume: false });
});

it('shows a stored value', async () => {
	stored = { resume_on_reset: true, compact_before_resume: false };
	render(LimitResumeRows);
	const resume = await screen.findByRole('switch', { name: 'Resume when the limit resets' });
	await waitFor(() => expect(resume.getAttribute('aria-checked')).toBe('true'));
	expect(screen.getByRole('switch', { name: 'Compact before resuming' }).getAttribute('aria-checked')).toBe('false');
});
