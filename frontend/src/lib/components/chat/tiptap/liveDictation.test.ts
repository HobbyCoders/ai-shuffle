import { afterEach, describe, expect, it } from 'vitest';
import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { createLiveDictation } from './liveDictation';

const editors: Editor[] = [];
function setup(content = '<p>Draft</p>') {
	const editor = new Editor({ extensions: [StarterKit], content });
	editors.push(editor);
	editor.commands.setTextSelection(editor.state.doc.content.size - 1);
	return { editor, dictation: createLiveDictation(editor) };
}
afterEach(() => editors.splice(0).forEach((editor) => editor.destroy()));

describe('live dictation ownership', () => {
	it('discard removes speech and preserves the draft and edits inside speech', () => {
		const { editor, dictation } = setup();
		dictation.write('private words');
		editor.commands.insertContentAt(1, 'My ');
		editor.commands.insertContentAt(15, '(manual)');
		dictation.discard();
		expect(editor.getText()).toBe('My Draft(manual)');
		dictation.discard();
		expect(editor.getText()).toBe('My Draft(manual)');
	});
	it('discard preserves manual replacements of dictated characters', () => {
		const { editor, dictation } = setup('<p></p>');
		dictation.write('hello world');
		editor.commands.insertContentAt({ from: 7, to: 12 }, 'friend');
		dictation.discard();
		expect(editor.getText()).toBe('friend');
	});
	it('renders interim text immediately, reconciles final and starts a fresh run', () => {
		const { editor, dictation } = setup();
		dictation.write('hello wor');
		expect(editor.getText()).toBe('Draft hello wor');
		dictation.write('hello world.');
		dictation.write('hello world.');
		expect(editor.getText()).toBe('Draft hello world.');
		dictation.commit();
		dictation.write('Next');
		expect(editor.getText()).toBe('Draft hello world. Next');
	});
	it('maps the dictated span when the user edits earlier draft text and moves the caret', () => {
		const { editor, dictation } = setup();
		dictation.write('hello');
		editor.commands.insertContentAt(1, 'My ');
		editor.commands.setTextSelection(2);
		dictation.write('hello world');
		expect(editor.getText()).toBe('My Draft hello world');
		expect(editor.state.selection.from).toBe(2);
	});
	it('preserves manual text inserted inside a provisional transcript during correction', () => {
		const { editor, dictation } = setup('<p></p>');
		dictation.write('hello wurld');
		editor.commands.insertContentAt(9, '(note)');
		dictation.write('hello world');
		expect(editor.getText()).toContain('(note)');
		expect(editor.getText().replace('(note)', '')).toBe('hello world');
	});
	it('never treats dictated markup as HTML or replaces pre-existing selected text', () => {
		const { editor, dictation } = setup();
		editor.commands.setTextSelection({ from: 1, to: 6 });
		dictation.write('<b>test</b> ');
		expect(editor.getText()).toBe('<b>test</b> Draft');
	});
	it('empty final removes only provisional speech and its space', () => {
		const { editor, dictation } = setup();
		dictation.write('noise');
		dictation.write('');
		expect(editor.getText()).toBe('Draft');
	});
});
