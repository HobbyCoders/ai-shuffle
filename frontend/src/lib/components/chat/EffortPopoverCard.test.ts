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
 it('renders six Codex levels and emits Ultra from the last stop', async () => {
  const onSelect = vi.fn();
  const codexOptions = EFFORT_OPTIONS.filter(o => ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'].includes(o.value));
  const view = render(EffortPopoverCard, { props: { options: codexOptions, value: null, defaultValue: 'low', onSelect } });
  expect(slider().max).toBe('5');
  expect(slider().value).toBe('0');
  expect(Array.from(document.querySelectorAll('.stops span'), el => el.textContent)).toEqual(['Low', 'Medium', 'High', 'XHigh', 'Max', 'Ultra']);
  await fireEvent.input(slider(), { target: { value: '5' } });
  expect(onSelect).toHaveBeenLastCalledWith('ultra');
  await view.rerender({ options: codexOptions, value: 'ultra', defaultValue: 'low', onSelect });
  expect(slider().getAttribute('aria-valuetext')).toBe('Ultra');
 });
	it('renders all eight exact stops and emits literal none and minimal', async () => {
		const onSelect = vi.fn();
		render(EffortPopoverCard, { props: { options: EFFORT_OPTIONS, value: null, defaultValue: 'max', onSelect } });
		expect(slider().max).toBe('7');
		expect(slider().value).toBe('6');
		expect(document.querySelectorAll('.stops span')).toHaveLength(8);
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

	it('holds the Fast toggle, neither on nor off, while the chat\'s own value loads (#418)', async () => {
		const onToggleFast = vi.fn();
		const view = render(EffortPopoverCard, { props: { options, value: 'high', onSelect: vi.fn(), fastSupported: true, fastEnabled: false, fastPending: true, onToggleFast } });
		const toggle = screen.getByRole('button', { name: 'Fast mode: loading' }) as HTMLButtonElement;
		expect(toggle.disabled).toBe(true);
		expect(toggle.getAttribute('aria-pressed')).toBe('mixed');
		expect(toggle.classList.contains('on')).toBe(false);
		await fireEvent.click(toggle);
		expect(onToggleFast).not.toHaveBeenCalled();
		await view.rerender({ options, value: 'high', onSelect: vi.fn(), fastSupported: true, fastEnabled: true, fastPending: false, onToggleFast });
		expect(screen.getByRole('button', { name: 'Fast mode' }).getAttribute('aria-pressed')).toBe('true');
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

	it('puts the thumb back on the level in force when the chat refuses a pick', async () => {
		let answer!: () => void;
		const onSelect = vi.fn(() => new Promise<void>(resolve => { answer = resolve; }));
		render(EffortPopoverCard, { props: { options, value: 'high', onSelect } });

		await fireEvent.input(slider(), { target: { value: '3' } });
		expect(onSelect).toHaveBeenCalledWith('xhigh');
		expect(slider().value).toBe('3');
		answer();
		await vi.waitFor(() => expect(slider().value).toBe('2'));
		expect(document.querySelector('[data-effort-value]')?.textContent).toBe('High');
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

	// #1526: the thumb rests on the model's default, so the label names it.
	it('names the model default the thumb rests on while no level is pinned', () => {
		render(EffortPopoverCard, { props: { options, value: null, defaultValue: 'high', onSelect: vi.fn() } });

		expect(slider().getAttribute('aria-valuetext')).toBe('Auto (High)');
		expect(slider().value).toBe(String(options.findIndex((o) => o.value === 'high')));
		expect(document.querySelector('[data-effort-value]')?.textContent).toBe('Auto (High)');
		expect(document.querySelector('.slider.unpinned')).toBeTruthy();
	});

	it('pins a level from the unpinned resting position', async () => {
		const onSelect = vi.fn();
		render(EffortPopoverCard, { props: { options, value: null, onSelect } });

		// Choosing a stop pins it even when no effort was previously selected.
		await fireEvent.input(slider(), { target: { value: '2' } });
		expect(onSelect).toHaveBeenCalledWith('high');
	});

	it('says when a toggle waits for the next message, only when the worker said so', async () => {
		const base = { options, value: 'high', onSelect: vi.fn(), fastSupported: true, onToggleFast: vi.fn() };
		const view = render(EffortPopoverCard, { props: { ...base, fastEnabled: true } });
		expect(screen.queryByTestId('fast-applies-notice')).toBeNull();
		await view.rerender({ ...base, fastEnabled: true, fastWaitsForNextMessage: true });
		expect(screen.getByTestId('fast-applies-notice').textContent).toMatch(/turns on with your next message/);
		await view.rerender({ ...base, fastEnabled: false, fastWaitsForNextMessage: true });
		expect(screen.getByTestId('fast-applies-notice').textContent).toMatch(/turns off with your next message/);
		await view.rerender({ ...base, fastSupported: false, fastWaitsForNextMessage: true });
		expect(screen.queryByTestId('fast-applies-notice')).toBeNull();
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

	it('energizes only a supported, enabled fast tier and leaves effort unchanged', async () => {
		const onSelect = vi.fn();
		const onToggleFast = vi.fn();
		const props = { options, value: 'high', onSelect, fastSupported: true, fastEnabled: false, onToggleFast };
		const view = render(EffortPopoverCard, { props });
		const card = view.container.querySelector('.effort-pop-card')!;
		const toggle = screen.getByRole('button', { name: 'Fast mode' });
		expect(card.classList.contains('fast-active')).toBe(false);
		await fireEvent.click(toggle);
		expect(onToggleFast).toHaveBeenCalledOnce();
		// The parent owns the setting; a click alone must not fake an enabled tier.
		expect(card.classList.contains('fast-active')).toBe(false);
		await view.rerender({ ...props, fastEnabled: true });
		expect(card.classList.contains('fast-active')).toBe(true);
		expect(toggle.getAttribute('aria-pressed')).toBe('true');
		expect(slider().getAttribute('aria-valuetext')).toBe('High');
		expect(onSelect).not.toHaveBeenCalled();
		await view.rerender(props);
		expect(card.classList.contains('fast-active')).toBe(false);
		expect(toggle.getAttribute('aria-pressed')).toBe('false');
		await view.rerender({ ...props, fastEnabled: true, fastSupported: false });
		expect(card.classList.contains('fast-active')).toBe(false);
		expect(screen.queryByRole('button', { name: 'Fast mode' })).toBeNull();
	});

	it.each([{ levels: [] }, { levels: [options[2]] }])('energizes fast mode without requiring a slider ($levels)', ({ levels }) => {
		const view = render(EffortPopoverCard, { props: {
			options: levels, value: null, onSelect: vi.fn(), fastSupported: true, fastEnabled: true
		} });
		expect(view.container.querySelector('.fast-active')).toBeTruthy();
		expect(screen.queryByRole('slider')).toBeNull();
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

describe('EffortPopoverCard — Ultrafast speed picker', () => {
	const base = { options, value: 'high', onSelect: vi.fn(), fastSupported: true };

	it('keeps the plain Fast toggle where the route has no Ultrafast', () => {
		render(EffortPopoverCard, { props: { ...base, fastEnabled: true } });
		expect(screen.getByRole('button', { name: 'Fast mode' })).toBeTruthy();
		expect(screen.queryByRole('radiogroup', { name: 'Speed' })).toBeNull();
		expect(screen.queryByTestId('ultrafast-cost')).toBeNull();
	});

	it('offers Standard, Fast and Ultrafast as one pick', async () => {
		const onSelectSpeed = vi.fn();
		const view = render(EffortPopoverCard, { props: { ...base, fastEnabled: true, ultrafastSupported: true, onSelectSpeed } });
		expect(screen.queryByRole('button', { name: 'Fast mode' })).toBeNull();
		const radios = screen.getAllByRole('radio');
		expect(radios.map((r) => r.textContent?.trim())).toEqual(['Standard', 'Fast', 'Ultrafast']);
		expect(radios.map((r) => r.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false']);
		expect(radios[2].getAttribute('title')).toMatch(/fastest tier/);
		// The cost is said before any pick (an API-key row by default: a price).
		expect(screen.getByTestId('ultrafast-cost').textContent).toMatch(/several times the Standard API price/);
		await fireEvent.click(radios[2]);
		expect(onSelectSpeed).toHaveBeenLastCalledWith('ultrafast');
		// The current pick is not sent again.
		await fireEvent.click(radios[1]);
		expect(onSelectSpeed).toHaveBeenCalledOnce();
		await view.rerender({ ...base, fastEnabled: true, ultrafastSupported: true, ultrafastEnabled: true, onSelectSpeed });
		expect(screen.getByRole('radio', { name: /Ultrafast/ }).getAttribute('aria-checked')).toBe('true');
		expect(screen.getByTestId('ultrafast-cost')).toBeTruthy();
		expect(document.querySelector('.effort-pop-card')!.classList.contains('ultrafast-active')).toBe(true);
		await fireEvent.click(screen.getByRole('radio', { name: /Standard/ }));
		expect(onSelectSpeed).toHaveBeenLastCalledWith('standard');
		// A ChatGPT sign-in keeps the picker without a cost paragraph.
		await view.rerender({ ...base, fastEnabled: false, ultrafastSupported: true, ultrafastBilling: 'plan', onSelectSpeed });
		expect(screen.getByRole('radio', { name: /Ultrafast/ })).toBeTruthy();
		expect(screen.queryByTestId('ultrafast-cost')).toBeNull();
	});

	it('holds every tier while the chat\'s own value loads', async () => {
		const onSelectSpeed = vi.fn();
		render(EffortPopoverCard, { props: { ...base, fastEnabled: true, fastPending: true, ultrafastSupported: true, onSelectSpeed } });
		const group = screen.getByRole('radiogroup', { name: 'Speed' });
		expect(group.getAttribute('aria-busy')).toBe('true');
		for (const radio of screen.getAllByRole('radio')) {
			expect(radio.getAttribute('aria-checked')).toBe('false');
			expect((radio as HTMLButtonElement).disabled).toBe(true);
		}
		await fireEvent.click(screen.getAllByRole('radio')[2]);
		expect(onSelectSpeed).not.toHaveBeenCalled();
	});

	it('names the tier a waiting change applies', () => {
		render(EffortPopoverCard, { props: { ...base, fastEnabled: true, ultrafastSupported: true, ultrafastEnabled: true, fastWaitsForNextMessage: true } });
		expect(screen.getByTestId('fast-applies-notice').textContent).toBe(
			'Ultrafast speed applies from your next message. This turn finishes at its current speed.'
		);
	});
});
