import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import SpeedLabel from './SpeedLabel.svelte';

afterEach(() => cleanup());

function label(): HTMLElement {
	return document.querySelector('.speed-label') as HTMLElement;
}

describe('SpeedLabel', () => {
	it('is the plain text on Standard', () => {
		render(SpeedLabel, { props: { text: 'High', speed: 'standard' } });
		expect(label().dataset.speed).toBe('standard');
		expect(label().classList.contains('fast')).toBe(false);
		expect(label().classList.contains('ultrafast')).toBe(false);
		expect(label().textContent).toBe('High');
		// The reduced-motion slot is kept, empty, so a pick never shifts the label.
		expect(document.querySelector('.speed-bolt')!.querySelectorAll('svg')).toHaveLength(0);
	});

	it('marks Fast and Ultrafast apart, with hidden bolts for reduced motion', () => {
		const view = render(SpeedLabel, { props: { text: 'High', speed: 'fast' } });
		expect(label().classList.contains('fast')).toBe(true);
		let bolt = document.querySelector('.speed-bolt')!;
		expect(bolt.getAttribute('aria-hidden')).toBe('true');
		expect(bolt.querySelectorAll('svg')).toHaveLength(1);
		view.rerender({ text: 'High', speed: 'ultrafast' });
		return Promise.resolve().then(() => {
			expect(label().classList.contains('ultrafast')).toBe(true);
			expect(label().classList.contains('fast')).toBe(false);
			bolt = document.querySelector('.speed-bolt')!;
			expect(bolt.querySelectorAll('svg')).toHaveLength(2);
			// The words stay the words: nothing extra is read out.
			expect(label().textContent).toBe('High');
		});
	});

	it('leaves the bolt to a badge beside it when asked', () => {
		render(SpeedLabel, { props: { text: 'GPT-6.1 Sol', speed: 'ultrafast', bolt: false } });
		expect(label().classList.contains('ultrafast')).toBe(true);
		expect(document.querySelector('.speed-bolt')).toBeNull();
	});
});
