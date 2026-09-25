/**
 * The AI generate dialog is named by its title, takes focus into its first
 * field, closes on Escape and hands focus back to the opener.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
vi.mock('$lib/api/client', () => ({ generateProfile: vi.fn(), generateSubagent: vi.fn(), continueGeneration: vi.fn() }));
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import AIGenerateDialog from './AIGenerateDialog.svelte';

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
});

describe('AIGenerateDialog keyboard and focus', () => {
	it('is named, focuses its first field, closes on Escape and restores focus', async () => {
		const opener = document.createElement('button');
		document.body.appendChild(opener);
		opener.focus();
		const onClose = vi.fn();
		const props = { type: 'profile' as const, isOpen: true, onClose, onGenerated: vi.fn() };
		const view = render(AIGenerateDialog, { props });
		await tick();
		await Promise.resolve();

		const dialog = screen.getByRole('dialog', { name: 'Generate Agent with AI' });
		expect(document.activeElement?.tagName).toBe('TEXTAREA');
		expect(dialog.contains(document.activeElement)).toBe(true);

		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(1);

		await view.rerender({ ...props, isOpen: false });
		await tick();
		expect(document.activeElement).toBe(opener);
	});
});
