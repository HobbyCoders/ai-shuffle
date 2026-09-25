/**
 * Import Session is a modal: named, focus moves in and back out, Escape
 * closes it, and the drop zone (a role="button" div) opens the file picker
 * from Space as well as Enter.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import ImportSessionModal from './ImportSessionModal.svelte';

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
});

async function settle() {
	await tick();
	await Promise.resolve();
}

describe('ImportSessionModal keyboard and focus', () => {
	it('is a named dialog that takes focus, closes on Escape and restores focus', async () => {
		const opener = document.createElement('button');
		document.body.appendChild(opener);
		opener.focus();
		const onclose = vi.fn();
		render(ImportSessionModal, { props: { show: true, onclose } });
		await settle();

		const dialog = screen.getByRole('dialog', { name: 'Import Session' });
		expect(dialog.contains(document.activeElement)).toBe(true);

		await fireEvent.keyDown(window, { key: 'Escape' });
		await settle();
		expect(onclose).toHaveBeenCalledTimes(1);
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(document.activeElement).toBe(opener);
	});

	it('opens the file picker from the drop zone with Space and Enter', async () => {
		render(ImportSessionModal, { props: { show: true } });
		await settle();
		const input = document.querySelector('input[type="file"]') as HTMLInputElement;
		const click = vi.spyOn(input, 'click').mockImplementation(() => {});
		const zone = screen.getByRole('button', { name: /Drop your export file here/ });

		await fireEvent.keyDown(zone, { key: ' ' });
		await fireEvent.keyDown(zone, { key: 'Enter' });
		expect(click).toHaveBeenCalledTimes(2);
	});
});
