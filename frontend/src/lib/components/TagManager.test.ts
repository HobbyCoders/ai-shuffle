/**
 * Manage Tags is a named modal that takes and returns focus, and its colour
 * swatches expose which colour is chosen (not only via the ring colour).
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
vi.mock('$lib/api/client', () => ({
	getTags: vi.fn(async () => [{ id: 't1', name: 'Urgent', color: '#ef4444' }]),
	createTag: vi.fn(),
	updateTag: vi.fn(),
	deleteTag: vi.fn()
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import TagManager from './TagManager.svelte';

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
});

describe('TagManager accessibility', () => {
	it('is a named modal that takes focus and returns it to the opener', async () => {
		const opener = document.createElement('button');
		document.body.appendChild(opener);
		opener.focus();
		const view = render(TagManager);
		await tick();
		await Promise.resolve();
		const dialog = screen.getByRole('dialog', { name: 'Manage Tags' });
		expect(dialog.contains(document.activeElement)).toBe(true);
		view.unmount();
		expect(document.activeElement).toBe(opener);
	});

	it('marks the chosen colour swatch as pressed', async () => {
		render(TagManager);
		await tick();
		const blue = screen.getByRole('button', { name: 'Blue' });
		const red = screen.getByRole('button', { name: 'Red' });
		expect(blue.getAttribute('aria-pressed')).toBe('true');
		expect(red.getAttribute('aria-pressed')).toBe('false');
		await fireEvent.click(red);
		expect(red.getAttribute('aria-pressed')).toBe('true');
		expect(blue.getAttribute('aria-pressed')).toBe('false');
	});
});
