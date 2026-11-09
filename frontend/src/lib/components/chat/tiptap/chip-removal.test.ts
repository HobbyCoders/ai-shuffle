import { afterEach, describe, expect, it, vi } from 'vitest';
import { NodeSelection } from '@tiptap/pm/state';
import { createChatEditor } from './editorConfig';
import { serializeToMessage, extractFileChips } from './serialization';
import type { Editor } from '@tiptap/core';

const chip = (name: string) => ({
	type: 'fileChip',
	attrs: { path: name, fullPath: `/uploads/${name}`, filename: name, size: 1, type: 'file' }
});

const draft = (...content: object[]) => JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content }] });

let editor: Editor | null = null;
afterEach(() => {
	editor?.destroy();
	editor = null;
});

function open(initialContent: string) {
	const withdraw = vi.fn();
	const notice = vi.fn((_names: string[], _undo: () => void) => withdraw);
	editor = createChatEditor({ element: document.createElement('div'), initialContent, onChipsRemoved: notice });
	return { editor, notice, withdraw };
}

const words = (text: string) => ({ type: 'text', text });

describe('file chips and keystrokes (#1263)', () => {
	it('is never a node selection, the selection a keystroke would replace', () => {
		const { editor } = open(draft(chip('a.txt')));
		const node = editor.state.doc.firstChild!.firstChild!;
		expect(node.type.name).toBe('fileChip');
		expect(NodeSelection.isSelectable(node)).toBe(false);
	});

	it('offers an undo for a chip a keystroke removed, and puts the same chip back', () => {
		const { editor, notice, withdraw } = open(draft(chip('a.txt'), words(' hello')));
		editor.commands.deleteRange({ from: 1, to: 2 });
		expect(extractFileChips(editor)).toHaveLength(0);
		expect(notice.mock.calls[0][0]).toEqual(['a.txt']);

		notice.mock.calls[0][1]();
		expect(withdraw).toHaveBeenCalledOnce();
		expect(extractFileChips(editor)[0]).toMatchObject({ fullPath: '/uploads/a.txt', filename: 'a.txt' });
		expect(serializeToMessage(editor)).toBe('@/uploads/a.txt hello');
	});

	it('restores a chip where it was even after more typing', () => {
		const { editor, notice } = open(draft(words('ab '), chip('a.txt'), words(' cd')));
		editor.commands.deleteRange({ from: 4, to: 5 });
		editor.commands.insertContentAt(1, 'zz');
		notice.mock.calls[0][1]();
		expect(serializeToMessage(editor)).toBe('zzab @/uploads/a.txt cd');
	});

	it('gathers removals into one undo', () => {
		const { editor, notice, withdraw } = open(draft(chip('a.txt'), words(' '), chip('b.txt'), words(' end')));
		editor.commands.deleteRange({ from: 3, to: 4 });
		editor.commands.deleteRange({ from: 1, to: 2 });
		expect(withdraw).toHaveBeenCalledOnce();
		expect(notice.mock.calls[1][0].sort()).toEqual(['a.txt', 'b.txt']);
		notice.mock.calls[1][1]();
		expect(extractFileChips(editor).map((c) => c.filename)).toEqual(['a.txt', 'b.txt']);
	});

	it('withdraws the offer when the draft is replaced, so a sent chip cannot return', () => {
		const { editor, notice, withdraw } = open(draft(chip('a.txt'), words(' hello')));
		editor.commands.deleteRange({ from: 1, to: 2 });
		editor.commands.clearContent();
		expect(withdraw).toHaveBeenCalledOnce();
		notice.mock.calls[0][1]();
		expect(extractFileChips(editor)).toHaveLength(0);
	});

	it('does not offer an undo for a send or a chat switch replacing the draft', () => {
		const { editor, notice } = open(draft(chip('a.txt')));
		editor.commands.clearContent();
		editor.commands.setContent(draft(chip('b.txt')));
		editor.commands.setContent('');
		expect(notice).not.toHaveBeenCalled();
	});

	it('does not offer an undo for the explicit remove control', () => {
		const { editor, notice } = open(draft(chip('a.txt'), words(' hello')));
		const remove = editor.view.dom.querySelector<HTMLButtonElement>('.file-chip-remove')!;
		remove.click();
		expect(extractFileChips(editor)).toHaveLength(0);
		expect(notice).not.toHaveBeenCalled();
	});

	it('keeps a chip whose twin was removed', () => {
		const { editor, notice } = open(draft(chip('a.txt'), words(' '), chip('a.txt')));
		editor.commands.deleteRange({ from: 3, to: 4 });
		expect(extractFileChips(editor)).toHaveLength(1);
		expect(notice.mock.calls[0][0]).toEqual(['a.txt']);
	});

	it('never joins a typed word onto the chip path', () => {
		const { editor } = open(draft(chip('a.txt'), words('word')));
		expect(serializeToMessage(editor)).toBe('@/uploads/a.txt word');
	});
});
