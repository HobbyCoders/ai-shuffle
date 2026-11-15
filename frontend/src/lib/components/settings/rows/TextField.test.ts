import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import TextField from './TextField.svelte';
afterEach(cleanup);
describe('TextField form keyboard paths', () => {
 it('fills the available row without changing numeric-width consumers', () => {
  render(TextField, { value: '/workspace/project', label: 'Path', width: 'full' });
  const field = screen.getByRole('textbox', { name: 'Path' });
  expect(field.style.getPropertyValue('--st-text-width')).toBe('100%');
  expect(field.parentElement?.style.getPropertyValue('--st-text-width')).toBe('100%');
 });
 it('preserves the real path while its resting display keeps the last segment', async () => {
  const path = '/workspace/a-very-long-project/location/final-directory';
  render(TextField, { value: path, label: 'Path', pathDisplay: true, mono: true });
  const field = screen.getByRole('textbox', { name: 'Path' }) as HTMLInputElement;
  expect(field.value).toBe(path);
  expect(document.querySelector('.path-rest .tail')?.textContent).toBe('/final-directory');
  expect(document.querySelector('.path-text')?.getAttribute('title')).toBe(path);
  await fireEvent.focus(field);
  expect(field.value).toBe(path);
 });
 it('uses an overridable desktop width so narrow rows can fill their control line', () => {
  render(TextField, { value: 'Before', label: 'Name', width: 260 });
  const field = screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement;
  expect(field.style.width).toBe('');
  expect(field.style.getPropertyValue('--st-text-width')).toBe('260px');
 });
 it('keeps default Enter/blur commits deduplicated', async () => {
  const onchange = vi.fn(); render(TextField, { value: 'Before', label: 'Name', onchange });
  const field = screen.getByRole('textbox', { name: 'Name' });
  await fireEvent.focus(field); await fireEvent.input(field, { target: { value: 'After' } });
  await fireEvent.keyDown(field, { key: 'Enter' }); await fireEvent.blur(field);
  expect(onchange).toHaveBeenCalledExactlyOnceWith('After');
 });
 it('lets an explicit-save form handle Enter and Escape without a second commit', async () => {
  const onchange = vi.fn(); const onkeydown = vi.fn((e: KeyboardEvent) => e.preventDefault());
  render(TextField, { value: 'Before', label: 'Name', onchange, onkeydown });
  const field = screen.getByRole('textbox', { name: 'Name' });
  await fireEvent.focus(field); await fireEvent.input(field, { target: { value: 'After' } });
  await fireEvent.keyDown(field, { key: 'Enter' }); await fireEvent.keyDown(field, { key: 'Escape' });
  expect(onkeydown).toHaveBeenCalledTimes(2); expect(onchange).not.toHaveBeenCalled();
 });
});

describe('TextField code-point limit (maxchars)', () => {
	const setup = (value = '') => {
		const oninput = vi.fn();
		render(TextField, { value, label: 'Name', maxchars: 100, oninput });
		const field = screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement;
		/** The browser's edit: the new value with the caret where it lands. */
		const edit = async (next: string, caret = next.length, init: InputEventInit = {}) => {
			field.value = next;
			field.setSelectionRange(caret, caret);
			await fireEvent.input(field, init);
		};
		return { field, oninput, edit };
	};
	const smile = '😀';

	it('holds the review sequence: 50 emoji, then 150 letters pasted over them', async () => {
		const { field, oninput, edit } = setup();
		await edit(smile.repeat(50));
		expect(field.value).toBe(smile.repeat(50));
		expect(oninput).toHaveBeenLastCalledWith(smile.repeat(50));
		await edit('x'.repeat(150));
		expect(field.value).toBe('x'.repeat(100));
		expect(oninput).toHaveBeenLastCalledWith('x'.repeat(100));
		expect(oninput.mock.calls.every(([v]) => Array.from(v as string).length <= 100)).toBe(true);
	});

	it('keeps all of 100 emoji pasted into an empty field', async () => {
		const { field, oninput, edit } = setup();
		await edit(smile.repeat(100));
		expect(field.value).toBe(smile.repeat(100));
		expect(oninput).toHaveBeenLastCalledWith(smile.repeat(100));
	});

	it('cuts a 300-character paste to 100', async () => {
		const { field, oninput, edit } = setup();
		await edit('p'.repeat(300));
		expect(field.value).toBe('p'.repeat(100));
		expect(oninput).toHaveBeenCalledExactlyOnceWith('p'.repeat(100));
	});

	it('drops a letter typed past the limit and keeps the caret where it was', async () => {
		const full = 'a'.repeat(100);
		const { field, oninput, edit } = setup(full);
		await edit('a'.repeat(50) + 'b' + 'a'.repeat(50), 51);
		expect(field.value).toBe(full);
		expect(field.selectionEnd).toBe(50);
		expect(oninput).toHaveBeenLastCalledWith(full);
		// Typing an emoji at the end of 99 letters still fits (100 code points).
		await edit('a'.repeat(99), 99);
		await edit('a'.repeat(99) + smile);
		expect(field.value).toBe('a'.repeat(99) + smile);
	});

	it('drops a ZWJ family pasted after 97 characters whole, never leaving a dangling joiner', async () => {
		const family = '👩\u200d👩\u200d👧';
		const { field, oninput, edit } = setup('a'.repeat(97));
		await edit('a'.repeat(97) + family);
		expect(field.value).toBe('a'.repeat(97));
		expect(oninput).toHaveBeenLastCalledWith('a'.repeat(97));
		// With room for it (95 + 5 code points) the family is kept whole.
		await edit('a'.repeat(95), 95);
		await edit('a'.repeat(95) + family);
		expect(field.value).toBe('a'.repeat(95) + family);
	});

	it('keeps 80 pasted emoji, and a second paste fills to exactly 100', async () => {
		const { field, edit } = setup();
		await edit(smile.repeat(80));
		expect(field.value).toBe(smile.repeat(80));
		await edit(smile.repeat(160));
		expect(field.value).toBe(smile.repeat(100));
	});

	it('cuts a ZWJ family committed by an IME after 97 characters whole', async () => {
		const family = '👩\u200d👩\u200d👧';
		const { field, oninput, edit } = setup('a'.repeat(97));
		await fireEvent.compositionStart(field);
		await edit('a'.repeat(97) + family, 97 + family.length, { isComposing: true });
		await fireEvent.compositionEnd(field);
		expect(field.value).toBe('a'.repeat(97));
		expect(oninput).toHaveBeenCalledExactlyOnceWith('a'.repeat(97));
	});

	it('waits for an IME composition to end before cutting or reporting', async () => {
		const { field, oninput, edit } = setup('a'.repeat(99));
		await fireEvent.compositionStart(field);
		await edit('a'.repeat(99) + 'にほ', 101, { isComposing: true });
		expect(field.value).toBe('a'.repeat(99) + 'にほ');
		expect(oninput).not.toHaveBeenCalled();
		await fireEvent.compositionEnd(field);
		expect(field.value).toBe('a'.repeat(99) + 'に');
		expect(oninput).toHaveBeenCalledExactlyOnceWith('a'.repeat(99) + 'に');
	});
});
