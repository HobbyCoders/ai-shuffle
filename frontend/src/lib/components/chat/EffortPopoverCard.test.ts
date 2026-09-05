import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import EffortPopoverCard from './EffortPopoverCard.svelte';
import { EFFORT_OPTIONS, UNKNOWN_EFFORT } from '$lib/constants/model-effort';

afterEach(() => cleanup());

const options = [
	{ value: 'low', label: 'Low', description: 'Quick, minimal reasoning' },
	{ value: 'medium', label: 'Medium', description: 'Balanced reasoning' },
	{ value: 'high', label: 'High', description: 'Thorough reasoning' },
	{ value: 'xhigh', label: 'XHigh', description: 'Extreme reasoning' },
	{ value: 'max', label: 'Max', description: 'Maximum reasoning depth' }
];

function slider(): HTMLInputElement {
	return screen.getByRole('slider', { name: /reasoning effort/i }) as HTMLInputElement;
}

describe('EffortPopoverCard', () => {
	it('renders seven exact stops and emits literal none and minimal', async () => {
		const onSelect = vi.fn();
		render(EffortPopoverCard, { props: { options: EFFORT_OPTIONS, value: null, defaultValue: 'max', onSelect } });
		expect(slider().max).toBe('6');
		expect(slider().value).toBe('6');
		expect(document.querySelectorAll('.stops span')).toHaveLength(7);
		await fireEvent.input(slider(), { target: { value: '0' } });
		expect(onSelect).toHaveBeenLastCalledWith('none');
		await fireEvent.input(slider(), { target: { value: '1' } });
		expect(onSelect).toHaveBeenLastCalledWith('minimal');
	});
	it('explains unknown capabilities without calling them unsupported', () => {
		render(EffortPopoverCard, { props: { options: [], value: 'minimal', known: false, explanation: UNKNOWN_EFFORT, onSelect: vi.fn() } });
		expect(screen.getByText(UNKNOWN_EFFORT)).toBeTruthy();
		expect(screen.queryByText('Not adjustable')).toBeNull();
		expect(screen.queryByRole('slider')).toBeNull();
	});
	it('explains no adjustable effort without hiding fast mode', async () => {
		const onToggleFast = vi.fn();
		render(EffortPopoverCard, { props: { options: [], value: null, onSelect: vi.fn(), fastSupported: true, onToggleFast } });
		expect(screen.queryByRole('slider')).toBeNull();
		expect(screen.getByText('Adjustable reasoning effort is not available for this model in AI Shuffle.')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Fast mode' }));
		expect(onToggleFast).toHaveBeenCalledOnce();
	});

	it('renders a single supported level as a button, not a zero-range slider', async () => {
		const onSelect = vi.fn();
		render(EffortPopoverCard, { props: { options: [options[2]], value: null, onSelect } });
		expect(screen.queryByRole('slider')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'High' }));
		expect(onSelect).toHaveBeenCalledWith('high');
	});
	it('shows the pinned level and a five-stop slider', () => {
		render(EffortPopoverCard, { props: { options, value: 'high', onSelect: vi.fn() } });

		expect(slider().max).toBe('4');
		expect(slider().value).toBe('2');
		expect(slider().getAttribute('aria-valuetext')).toBe('High');
		expect(document.querySelector('[data-effort-value]')?.textContent).toBe('High');
		expect(screen.getByText('Faster')).toBeTruthy();
		expect(screen.getByText('Smarter')).toBeTruthy();
		expect(document.querySelectorAll('.stops span')).toHaveLength(5);
		expect(document.querySelector('.stops .on')?.textContent).toBe('High');
		expect(document.querySelector('.pop-note')).toBeNull();
		expect(screen.queryByRole('button', { name: /what effort changes/i })).toBeNull();
	});

	it('emits the option at the dragged stop', async () => {
		const onSelect = vi.fn();
		render(EffortPopoverCard, { props: { options, value: 'high', onSelect } });

		await fireEvent.input(slider(), { target: { value: '4' } });
		expect(onSelect).toHaveBeenCalledWith('max');
	});

	it('does not re-emit the level that is already pinned', async () => {
		const onSelect = vi.fn();
		render(EffortPopoverCard, { props: { options, value: 'high', onSelect } });

		await fireEvent.input(slider(), { target: { value: '2' } });
		expect(onSelect).not.toHaveBeenCalled();
	});

	it('reads as Auto with no stop selected when no level is pinned', () => {
		render(EffortPopoverCard, { props: { options, value: null, onSelect: vi.fn() } });

		expect(slider().getAttribute('aria-valuetext')).toBe('Auto');
		expect(document.querySelector('[data-effort-value]')?.textContent).toBe('Auto');
		expect(document.querySelector('.stops .on')).toBeNull();
		expect(document.querySelector('.slider.unpinned')).toBeTruthy();
		expect(document.querySelector('.pop-note')).toBeNull();
	});

	it('pins a level from the unpinned resting position', async () => {
		const onSelect = vi.fn();
		render(EffortPopoverCard, { props: { options, value: null, onSelect } });

		// Choosing a stop pins it even when no effort was previously selected.
		await fireEvent.input(slider(), { target: { value: '2' } });
		expect(onSelect).toHaveBeenCalledWith('high');
	});

	it('shows the fast toggle only when the model carries the tier', () => {
		const onToggleFast = vi.fn();
		const { unmount } = render(EffortPopoverCard, {
			props: { options, value: 'high', onSelect: vi.fn(), fastSupported: true, fastEnabled: true, onToggleFast }
		});
		const toggle = screen.getByRole('button', { name: 'Fast mode' });
		expect(toggle.getAttribute('aria-pressed')).toBe('true');
		unmount();

		render(EffortPopoverCard, { props: { options, value: 'high', onSelect: vi.fn(), fastSupported: false } });
		expect(screen.queryByRole('button', { name: 'Fast mode' })).toBeNull();
	});

	it('updates the header level when the pinned value changes', async () => {
		const onSelect = vi.fn();
		const view = render(EffortPopoverCard, { props: { options, value: 'high', onSelect } });

		expect(document.querySelector('[data-effort-value]')?.textContent).toBe('High');
		await view.rerender({ options, value: 'xhigh', onSelect });
		expect(document.querySelector('[data-effort-value]')?.textContent).toBe('XHigh');
		expect(slider().getAttribute('aria-valuetext')).toBe('XHigh');
	});
});
