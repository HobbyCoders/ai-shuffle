import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import EmptyDetail from './EmptyDetail.svelte';
import EmptyListHarness from '../../layout/view/test-fixtures/EmptyListHarness.svelte';
import EmptyStateWithAction from './test-fixtures/EmptyStateWithAction.svelte';
import { Puzzle } from 'lucide-svelte';

beforeEach(() => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('shared list and detail empty states', () => {
	it('leaves one create action in the panel header and only a muted line in its body', () => {
		const { container } = render(EmptyListHarness);
		expect(screen.getAllByRole('button', { name: 'New item' })).toHaveLength(1);
		const empty = container.querySelector('.panel-body .st-empty-detail.in-list');
		expect(empty?.textContent?.trim()).toBe('No items yet');
		expect(empty?.querySelector('h2, button')).toBeNull();
	});
	it('retains the separate Clear search recovery path', async () => {
		render(EmptyListHarness, { filtered: true });
		expect(screen.getByText('No matching items')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));
		expect(screen.getByText('No items yet')).toBeTruthy();
	});
	it('shows the full state with its create action when a phone list is truly empty', () => {
		vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
		const { container } = render(EmptyListHarness);
		const hero = container.querySelector('.panel-body .st-empty-detail.list-hero');
		expect(hero).toBeTruthy();
		expect(hero?.querySelector('h2')?.textContent).toBe('No items yet');
		expect(hero?.querySelector('p')?.textContent).toBe('Create an item to get started.');
		expect(hero?.querySelector('.acts button')?.textContent).toBe('New item');
	});
	it('keeps the muted line and Clear on a phone when a search finds nothing', () => {
		vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
		const { container } = render(EmptyListHarness, { filtered: true });
		expect(container.querySelector('.list-hero')).toBeNull();
		expect(container.querySelector('.st-empty-detail.in-list p')?.textContent).toBe('No matching items');
		expect(screen.getByRole('button', { name: 'Clear search' })).toBeTruthy();
	});
	it('retains the full explanation and action outside a list', () => {
		const onAction = vi.fn();
		render(EmptyDetail, { title: 'No items yet', message: 'Create an item to get started.', actionLabel: 'New item', onAction });
		expect(screen.getByRole('heading', { name: 'No items yet' })).toBeTruthy();
		expect(screen.getByText('Create an item to get started.')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'New item' })).toBeTruthy();
	});
	it('draws one 24px, 1.5px-stroke icon on desktop as well as on a phone', () => {
		const { container } = render(EmptyDetail, { title: 'No notes yet', message: 'Keep ideas together.', icon: Puzzle });
		const svg = container.querySelector('.st-empty-detail .icon svg');
		expect(svg?.getAttribute('width')).toBe('24');
		expect(svg?.getAttribute('stroke-width')).toBe('1.5');
		expect(container.querySelector('.icon')?.getAttribute('aria-hidden')).toBe('true');
	});
	it('shows the full state on desktop for a list that is the whole page', () => {
		const context = new Map([['view-panel-empty', true]]);
		const { container } = render(EmptyStateWithAction, { context, props: { full: true } });
		expect(container.querySelector('.st-empty-detail.list-hero h2')?.textContent).toBe('No plugins yet');
		expect(container.querySelector('.icon svg')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Add plugin' })).toBeTruthy();
	});
	it('keeps the muted line in a list beside an open detail', () => {
		const context = new Map([['view-panel-empty', true]]);
		const { container } = render(EmptyStateWithAction, { context, props: { full: false } });
		expect(container.querySelector('.list-hero')).toBeNull();
		expect(container.querySelector('.st-empty-detail.in-list p')?.textContent).toBe('No plugins yet');
		expect(container.querySelector('.icon')).toBeNull();
	});
});
