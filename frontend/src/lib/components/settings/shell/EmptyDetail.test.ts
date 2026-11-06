import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import EmptyDetail from './EmptyDetail.svelte';
import EmptyListHarness from '../../layout/view/test-fixtures/EmptyListHarness.svelte';

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
	it('retains the full explanation and action outside a list', () => {
		const onAction = vi.fn();
		render(EmptyDetail, { title: 'No items yet', message: 'Create an item to get started.', actionLabel: 'New item', onAction });
		expect(screen.getByRole('heading', { name: 'No items yet' })).toBeTruthy();
		expect(screen.getByText('Create an item to get started.')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'New item' })).toBeTruthy();
	});
});
