/**
 * Escape on a page opened straight from its bar cell closes the phone controls
 * panel and hands focus back to that cell. A touch tap on plain panel text
 * (a group name on the Model page) drops focus to the page: Escape from an
 * attached keyboard must still return it to the cell, and must never take
 * focus back from another control.
 */
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import type { ComposerSheetProps } from '../composer-shell';
import { EFFORT_OPTIONS } from '$lib/constants/model-effort';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import '$lib/test-stubs/composer-dom';

vi.mock('$app/navigation', () => ({ pushState: () => { throw new Error('No router in this component test'); } }));
vi.mock('$lib/components/chat/ContextPillBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/RunningTasksList.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/layout/ChatExportActions.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/settings/ModelPickerPanel.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));

function props(): ComposerSheetProps {
	return {
		open: false, onClose: vi.fn(), modelOptions: [], modelValue: 'sonnet', modelLabel: 'Sonnet', onModelSelect: vi.fn(),
		contextChoice: 'standard', onContextChoiceSelect: vi.fn(),
		effortOptions: EFFORT_OPTIONS.filter((o) => ['low', 'high'].includes(o.value)), effortValue: null,
		effortKnown: true, effortDefaultValue: 'high', onEffortSelect: vi.fn(),
		permissionOptions: [{ value: 'default', label: 'Ask', description: 'Ask first' }],
		permissionValue: 'default', onPermissionSelect: vi.fn()
	};
}

beforeEach(() => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	document.body.innerHTML = '';
});

/** Render the sheet as the dock does: it owns `open` and follows the sheet's requests. */
async function openFromCell(cellName: 'model' | 'controls') {
	const p = props();
	const utils = render(MobileComposerControlsSheet, { ...p, onOpenChange: (next: boolean) => void utils.rerender({ open: next }) });
	const cell = document.querySelector<HTMLElement>(`[data-cell="${cellName}"]`)!;
	cell.focus();
	await fireEvent.click(cell);
	await tick();
	await tick();
	const page = document.querySelector<HTMLElement>(`.mcp [data-page="${cellName === 'controls' ? 'main' : cellName}"]`)!;
	expect(page.inert).toBe(false);
	return { cell };
}

async function escape() {
	await fireEvent.keyDown(window, { key: 'Escape' });
	await tick();
	await tick();
}

const panelOpen = () => document.querySelector('[data-cell="controls"]')!.getAttribute('aria-expanded') === 'true';

describe('phone controls panel: Escape after focus was lost to the page', () => {
	it('returns focus to the Model cell that opened the Model page', async () => {
		const { cell } = await openFromCell('model');
		expect(panelOpen()).toBe(true);
		(document.activeElement as HTMLElement).blur();
		expect(document.activeElement).toBe(document.body);

		await escape();
		expect(panelOpen()).toBe(false);
		expect(document.activeElement).toBe(cell);
	});

	it('on the Model page reached through the panel\'s own Model row, goes back to that row', async () => {
		await openFromCell('controls');
		const row = document.querySelector<HTMLElement>('.mcp button[aria-label^="Model: "]')!;
		row.focus();
		await fireEvent.click(row);
		await tick();
		expect(document.querySelector<HTMLElement>('.mcp [data-page="model"]')!.inert).toBe(false);
		(document.activeElement as HTMLElement).blur();

		await escape();
		expect(panelOpen()).toBe(true);
		expect(document.activeElement).toBe(row);
	});

	it('leaves focus alone when it sits on another control', async () => {
		const other = document.createElement('button');
		document.body.append(other);
		await openFromCell('model');
		other.focus();
		await escape();
		expect(panelOpen()).toBe(false);
		expect(document.activeElement).toBe(other);
	});

	it('falls back to the controls cell, as with focus inside, when the Model cell is gone', async () => {
		const { cell } = await openFromCell('model');
		(document.activeElement as HTMLElement).blur();
		cell.remove();
		await escape();
		expect(panelOpen()).toBe(false);
		expect(document.activeElement).toBe(document.querySelector('[data-cell="controls"]'));
	});

	it('does not throw when the sheet is unmounted while open with focus on the page', async () => {
		await openFromCell('model');
		(document.activeElement as HTMLElement).blur();
		expect(() => cleanup()).not.toThrow();
		await escape();
		expect(document.activeElement).toBe(document.body);
	});

	it('does not grab focus on a later close by another route after an Escape that only went back', async () => {
		await openFromCell('controls');
		const row = document.querySelector<HTMLElement>('.mcp button[aria-label^="Model: "]')!;
		await fireEvent.click(row);
		await tick();
		(document.activeElement as HTMLElement).blur();
		await escape();
		(document.activeElement as HTMLElement).blur();
		document.querySelector<HTMLElement>('.mcp-scrim')!.click();
		await tick();
		await tick();
		expect(panelOpen()).toBe(false);
		expect(document.activeElement).toBe(document.body);
	});
});
