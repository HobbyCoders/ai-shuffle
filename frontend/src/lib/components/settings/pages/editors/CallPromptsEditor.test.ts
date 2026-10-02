import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import CallPromptsEditor from './CallPromptsEditor.svelte';

afterEach(cleanup);

const details = {
	model: 'gpt-live-1',
	voices: [],
	transport: 'stream' as const,
	runtime_settings: [],
	prompts: [
		{ label: 'Voice conversation prompt', key: 'instructions' as const, text: 'Default conversation', default_text: 'Default conversation' },
		{ label: 'Chat agent prompt', key: 'delegation_instructions' as const, text: 'Default tasks', default_text: 'Default tasks' }
	],
	prompt_note: 'Uses the chat composer.',
	preview_note: ''
};

function setup(options = {}, onSave = vi.fn().mockResolvedValue(true), onClose = vi.fn()) {
	return { ...render(CallPromptsEditor, { options, details, saving: false, onSave, onClose }), onSave, onClose };
}

const area = (name: string) => screen.getByLabelText(name) as HTMLTextAreaElement;

it('shows the factory prompts as editable text, without model or delegation settings', () => {
	setup();
	expect(area('Voice conversation prompt').value).toBe('Default conversation');
	expect(area('Chat agent prompt').value).toBe('Default tasks');
	expect(screen.queryByRole('combobox')).toBeNull();
	expect(screen.getByText('Uses the chat composer.')).toBeTruthy();
});

it('saves edits, then restores and saves the defaults as empty values', async () => {
	const { onSave, onClose } = setup({ instructions: 'Custom conversation' });
	await fireEvent.input(area('Voice conversation prompt'), { target: { value: 'New prompt' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save prompts' }));
	await waitFor(() => expect(onSave).toHaveBeenCalledWith({ instructions: 'New prompt', delegation_instructions: '' }));
	expect(onClose).toHaveBeenCalledOnce();
	await fireEvent.click(screen.getByRole('button', { name: 'Restore default Voice conversation prompt' }));
	expect(area('Voice conversation prompt').value).toBe('Default conversation');
	await fireEvent.click(screen.getByRole('button', { name: 'Save prompts' }));
	await waitFor(() => expect(onSave).toHaveBeenLastCalledWith({ instructions: '', delegation_instructions: '' }));
});

it('keeps unsaved edits when the saved prompts change elsewhere, and offers the latest', async () => {
	const onSave = vi.fn().mockResolvedValue(false);
	const { rerender } = setup({ instructions: 'Saved' }, onSave);
	expect(screen.queryByText('Changed elsewhere, your edits kept')).toBeNull();
	await fireEvent.input(area('Voice conversation prompt'), { target: { value: 'Typing' } });
	await rerender({ options: { instructions: 'External saved' }, details, saving: false, onSave });
	expect(area('Voice conversation prompt').value).toBe('Typing');
	expect(screen.getByText('Changed elsewhere, your edits kept')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Save prompts' }));
	await waitFor(() => expect(onSave).toHaveBeenCalled());
	expect(area('Voice conversation prompt').value).toBe('Typing');
	await fireEvent.click(screen.getByRole('button', { name: 'Load latest' }));
	expect(area('Voice conversation prompt').value).toBe('External saved');
	expect(screen.queryByText('Changed elsewhere, your edits kept')).toBeNull();
});

it('discards edits back to the saved prompts', async () => {
	setup({ instructions: 'Saved' });
	await fireEvent.input(area('Voice conversation prompt'), { target: { value: 'Typing' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }));
	expect(area('Voice conversation prompt').value).toBe('Saved');
	expect((screen.getByRole('button', { name: 'Save prompts' }) as HTMLButtonElement).disabled).toBe(true);
});
