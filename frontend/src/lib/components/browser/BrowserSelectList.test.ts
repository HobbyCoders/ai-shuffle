// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import BrowserSelectList from './BrowserSelectList.svelte';
import type { BrowserChoices } from '$lib/browser-panel/pickers';

afterEach(cleanup);

const choices: BrowserChoices = {
	selected: 2,
	groups: ['Europe', 'Asia'],
	options: [
		{ label: 'Choose a country', disabled: false },
		{ label: 'Austria', disabled: false, group: 0 },
		{ label: 'Belgium', disabled: false, group: 0 },
		{ label: 'Bhutan', disabled: true, group: 1 },
		{ label: 'Bangladesh', disabled: false, group: 1 },
	],
};

function setup(list = choices) {
	const onChoose = vi.fn();
	const onCancel = vi.fn();
	render(BrowserSelectList, { choices: list, label: 'Country', onChoose, onCancel });
	const box = screen.getByRole('listbox', { name: 'Country' });
	const active = () => box.getAttribute('aria-activedescendant') && document.getElementById(box.getAttribute('aria-activedescendant')!)?.textContent?.trim();
	return { onChoose, onCancel, box, active };
}

describe('BrowserSelectList', () => {
	it('shows the options with their groups, the chosen one and disabled ones', () => {
		const { box } = setup();
		expect(document.activeElement).toBe(box);
		expect(screen.getByText('Europe')).toBeTruthy();
		expect(screen.getByText('Asia')).toBeTruthy();
		const options = screen.getAllByRole('option');
		expect(options.map((o) => o.textContent?.trim())).toEqual(['Choose a country', 'Austria', 'Belgium', 'Bhutan', 'Bangladesh']);
		expect(options[2].getAttribute('aria-selected')).toBe('true');
		expect(options[3].getAttribute('aria-disabled')).toBe('true');
		expect(screen.queryByLabelText('Filter options')).toBeNull(); // short list: no filter box
	});

	it('moves with the arrows past disabled options and chooses with Enter', async () => {
		const { box, active, onChoose } = setup();
		expect(active()).toBe('Belgium');
		await fireEvent.keyDown(box, { key: 'ArrowDown' });
		expect(active()).toBe('Bangladesh'); // Bhutan is disabled
		await fireEvent.keyDown(box, { key: 'ArrowDown' });
		expect(active()).toBe('Bangladesh');
		await fireEvent.keyDown(box, { key: 'Home' });
		expect(active()).toBe('Choose a country');
		await fireEvent.keyDown(box, { key: 'End' });
		await fireEvent.keyDown(box, { key: 'Enter' });
		expect(onChoose).toHaveBeenCalledWith(4);
	});

	it('jumps by typed letters and chooses with Space', async () => {
		const { box, active, onChoose } = setup();
		await fireEvent.keyDown(box, { key: 'a' });
		expect(active()).toBe('Austria');
		await fireEvent.keyDown(box, { key: 'b' });
		expect(active()).toBe('Austria'); // "ab" matches nothing: stay put
		await new Promise((r) => setTimeout(r, 800));
		await fireEvent.keyDown(box, { key: 'b' });
		await fireEvent.keyDown(box, { key: 'a' });
		expect(active()).toBe('Bangladesh');
		await new Promise((r) => setTimeout(r, 800));
		await fireEvent.keyDown(box, { key: ' ' });
		expect(onChoose).toHaveBeenCalledWith(4);
	});

	it('chooses with a click, ignores disabled options and cancels on Escape', async () => {
		const { box, onChoose, onCancel } = setup();
		await fireEvent.click(screen.getByText('Bhutan'));
		expect(onChoose).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByText('Austria'));
		expect(onChoose).toHaveBeenCalledWith(1);
		await fireEvent.keyDown(box, { key: 'Escape' });
		expect(onCancel).toHaveBeenCalled();
	});

	it('filters a long list and says when nothing matches or the list was cut short', async () => {
		const long: BrowserChoices = {
			selected: 0,
			groups: [],
			truncated: true,
			options: Array.from({ length: 20 }, (_, i) => ({ label: `Item ${i}`, disabled: false })),
		};
		const { onChoose } = setup(long);
		expect(screen.getByText('Showing the first 20 options.')).toBeTruthy();
		const filter = screen.getByLabelText('Filter options');
		await fireEvent.input(filter, { target: { value: '17' } });
		expect(screen.getAllByRole('option').map((o) => o.textContent?.trim())).toEqual(['Item 17']);
		await fireEvent.keyDown(filter, { key: 'Enter' });
		expect(onChoose).toHaveBeenCalledWith(17);
		await fireEvent.input(filter, { target: { value: 'zzz' } });
		expect(screen.getByRole('status').textContent).toContain('No options match');
	});
});
