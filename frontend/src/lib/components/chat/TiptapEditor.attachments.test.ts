import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import TiptapEditor from './TiptapEditor.svelte';
import { extractFileChips } from './tiptap/serialization';
import { evaluateSubmitGuards } from './submit-guards';

afterEach(cleanup);

describe('attachment-only composer messages', () => {
	it.each(['image.png', 'document.pdf'])('serializes %s inserted through the upload chip API without typed text', async (filename) => {
		const onTextChange = vi.fn();
		const { component } = render(TiptapEditor, { props: { onTextChange } });
		await tick();
		component.insertFileChip({ path: `uploads/${filename}`, fullPath: `/workspace/uploads/${filename}`, filename, size: 100, type: 'file' });
		await tick();

		expect(component.getText().trim()).toBe('');
		expect(component.getMessage().trim()).toBe(`@/workspace/uploads/${filename}`);
		expect(onTextChange).toHaveBeenLastCalledWith(component.getMessage());
		expect(extractFileChips(component.getEditor()!)[0].fullPath).toBe(`/workspace/uploads/${filename}`);
		expect(evaluateSubmitGuards({ hasPrompt: !!component.getMessage().trim(), isUploading: false, isCreatingWorktree: false, hasProfile: true, hasAnyProfile: true, hasProject: false, mode: 'chat' })).toBeNull();

		component.clear();
		await tick();
		expect(component.getMessage().trim()).toBe('');
		expect(onTextChange).toHaveBeenLastCalledWith('');
	});

	it('preserves multiple attachments when typed text is removed', async () => {
		const { component } = render(TiptapEditor);
		await tick();
		for (const filename of ['one.png', 'two.txt']) {
			component.insertFileChip({ path: filename, fullPath: `/uploads/${filename}`, filename, size: 10, type: 'file' });
		}
		component.appendText('temporary caption');
		const editor = component.getEditor()!;
		const end = editor.state.doc.content.size - 1;
		editor.commands.deleteRange({ from: end - 'temporary caption'.length, to: end });
		await tick();
		expect(component.getText().trim()).toBe('');
		expect(component.getMessage().trim()).toBe('@/uploads/one.png @/uploads/two.txt');
		expect(extractFileChips(editor)).toHaveLength(2);
	});
});
