/**
 * OpenAI's Ultrafast tier in the composer: the effort label names and animates
 * the tier in force, the Fast toggle becomes a three-way pick only where the
 * route carries Ultrafast, and a model without it falls back to Fast.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { composerPreferences } from '$lib/stores/composer-preferences';
import { activeSession } from '$lib/stores/active-session';
import { hydrateModelRegistry } from '$lib/constants/models';
import ControlsDock from '$lib/components/chat/ControlsDock.svelte';
import '$lib/test-stubs/composer-dom';
import { effortRegistryFixture } from '$lib/test-stubs/effort-registry';
import { installMobileShell } from './register';

installMobileShell();
const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }));
});
vi.mock('$lib/api/client', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/client')>();
	return { ...actual, api: { ...actual.api, get: mocks.get },
		getUsageLimits: vi.fn().mockResolvedValue({ available: false, buckets: {}, provider_buckets: {} }) };
});

/** The fixture with GPT-6 Astra flagged for Ultrafast, as a Pro plan's catalog would. */
const payload = {
	...effortRegistryFixture,
	providers: effortRegistryFixture.providers.map((p) => p.id !== 'codex' ? p : {
		...p, models: p.models.map((m) => m.id === 'gpt-6-astra' ? { ...m, ultrafast: true } : m)
	})
};
const ASTRA = 'prov:codex:gpt-6-astra';
const SOL = 'prov:codex:gpt-5.6-sol';

beforeEach(async () => {
	composerPreferences.set('modelEfforts', {});
	for (const model of [ASTRA, SOL]) composerPreferences.rememberFast(model, null);
	mocks.get.mockResolvedValue(payload);
	await hydrateModelRegistry();
	activeSession.newChat();
	activeSession.setModelOverride(ASTRA);
	activeSession.setEffortLevelOverride('high');
	await activeSession.setSpeed('standard');
});
afterEach(() => {
	cleanup();
	document.querySelectorAll('.mcp-portal').forEach((n) => n.remove());
});

const effortButton = () => document.querySelector<HTMLButtonElement>('.effort-btn')!;
const effortLabel = () => effortButton().querySelector<HTMLElement>('.speed-label')!;

describe('the effort label shows the speed tier', () => {
	it('names and marks Standard, Fast and Ultrafast apart', async () => {
		render(ControlsDock, { props: {} });
		await tick();
		expect(effortButton().getAttribute('aria-label')).toBe('Effort: High');
		expect(effortLabel().dataset.speed).toBe('standard');

		await activeSession.setSpeed('fast');
		await tick();
		expect(effortButton().getAttribute('aria-label')).toBe('Effort: High, Fast');
		expect(effortLabel().classList.contains('fast')).toBe(true);

		await activeSession.setSpeed('ultrafast');
		await tick();
		expect(effortButton().getAttribute('aria-label')).toBe('Effort: High, Ultrafast');
		expect(effortButton().title).toContain('Ultrafast');
		expect(effortLabel().classList.contains('ultrafast')).toBe(true);
		// The words are unchanged; the decoration is hidden from screen readers.
		expect(effortLabel().querySelector('.speed-text')!.textContent).toBe('High');
		expect(effortLabel().querySelector('.speed-bolt')!.getAttribute('aria-hidden')).toBe('true');
	});

	it('keeps no empty bolt slot on a model without a speed choice', async () => {
		render(ControlsDock, { props: {} });
		activeSession.setModelOverride('prov:or:z-ai/glm-5.3-flash');
		await tick();
		expect(effortLabel().querySelector('.speed-bolt')).toBeNull();
	});

	it('drops the tier as soon as the model falls back, and restores the pick per model', async () => {
		render(ControlsDock, { props: {} });
		await tick();
		await fireEvent.click(screen.getByRole('button', { name: /^Effort:/ }));
		await fireEvent.click(screen.getByRole('radio', { name: /Ultrafast/ }));
		await tick();
		expect(get(activeSession)).toMatchObject({ fastMode: true, ultrafastMode: true });
		expect(composerPreferences.speedForModel(ASTRA)).toBe('ultrafast');

		// A model with Fast but no Ultrafast: Fast, and the plain toggle again.
		activeSession.setModelOverride(SOL);
		await tick();
		expect(get(activeSession)).toMatchObject({ fastMode: true, ultrafastMode: false });
		expect(effortButton().getAttribute('aria-label')).toMatch(/, Fast$/);
		expect(screen.queryByRole('radiogroup', { name: 'Speed' })).toBeNull();
		expect(screen.getByRole('button', { name: 'Fast mode' }).getAttribute('aria-pressed')).toBe('true');

		// Back on Astra the owner's Ultrafast pick returns.
		activeSession.setModelOverride(ASTRA);
		await tick();
		expect(get(activeSession)).toMatchObject({ fastMode: true, ultrafastMode: true });
		expect(effortButton().getAttribute('aria-label')).toMatch(/, Ultrafast$/);
	});

	it('falls back on its own when the chat holds Ultrafast for a model without it', async () => {
		render(ControlsDock, { props: {} });
		await activeSession.setSpeed('ultrafast', false);
		activeSession.setModelOverride(SOL);
		await tick();
		await tick();
		expect(get(activeSession)).toMatchObject({ fastMode: true, ultrafastMode: false });
	});
});

describe('the phone controls offer the three tiers', () => {
	it('swaps the Fast switch for a Speed pick and badges the quick bar', async () => {
		render(ControlsDock, { props: {} });
		await tick();
		await fireEvent.click(screen.getByRole('button', { name: /^All chat controls/ }));
		const group = document.querySelector('.mcp [data-row="speed"] [role="radiogroup"][aria-label="Speed"]')!;
		const radios = Array.from(group.querySelectorAll<HTMLButtonElement>('[role="radio"]'));
		expect(radios.map((r) => r.textContent?.trim())).toEqual(['Standard', 'Fast', 'Ultrafast']);
		expect(screen.queryByRole('switch', { name: 'Fast mode' })).toBeNull();
		// The cost shows before any pick, worded for the ChatGPT plan.
		expect(document.querySelector('.mcp [data-testid="ultrafast-cost"]')!.textContent).toMatch(/8x the plan allowance/);
		await fireEvent.click(radios[2]);
		await tick();
		expect(get(activeSession)).toMatchObject({ fastMode: true, ultrafastMode: true });
		expect(radios[2].getAttribute('aria-checked')).toBe('true');
		const model = document.querySelector<HTMLButtonElement>('.qcell.model')!;
		expect(model.getAttribute('aria-label')).toMatch(/, ultrafast mode/);
		expect(model.querySelectorAll('[data-fast-badge] svg')).toHaveLength(2);
		expect(document.querySelector('.mcp [data-row="effort"] .mcp-val .speed-label')!.classList.contains('ultrafast')).toBe(true);
	});

	it('keeps the plain Fast switch on a model without Ultrafast', async () => {
		activeSession.setModelOverride(SOL);
		render(ControlsDock, { props: {} });
		await tick();
		await fireEvent.click(screen.getByRole('button', { name: /^All chat controls/ }));
		expect(screen.getByRole('switch', { name: 'Fast mode' })).toBeTruthy();
		expect(document.querySelector('[data-row="speed"]')).toBeNull();
	});
});
