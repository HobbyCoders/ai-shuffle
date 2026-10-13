import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LinkValue from './LinkValue.svelte';

afterEach(cleanup);

describe('LinkValue', () => {
	it('is a button that runs onclick on click, Enter and Space', async () => {
		const onclick = vi.fn();
		render(LinkValue, { label: 'OpenAI API key', onclick });
		const link = screen.getByRole('button', { name: 'OpenAI API key' });
		await fireEvent.click(link);
		await fireEvent.keyDown(link, { key: 'Enter' });
		await fireEvent.keyDown(link, { key: ' ' });
		expect(onclick).toHaveBeenCalledTimes(3);
	});

	it('stays usable inside a disabled fieldset (read-only detail)', async () => {
		const fieldset = document.createElement('fieldset');
		fieldset.disabled = true;
		const body = document.createElement('div');
		fieldset.append(body);
		document.body.append(fieldset);
		const onclick = vi.fn();
		render(LinkValue, { target: body, props: { label: 'Voice', onclick } });
		const link = screen.getByRole('button', { name: 'Voice' });
		expect(link.matches(':disabled')).toBe(false);
		// dispatch, not .click(): jsdom wrongly refuses .click() on any element in a disabled fieldset.
		await fireEvent.click(link);
		expect(onclick).toHaveBeenCalledOnce();
		fieldset.remove();
	});
});
