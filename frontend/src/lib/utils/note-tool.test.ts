import { describe, expect, it } from 'vitest';
import { notesFromToolMessage, splitNoteText } from './note-tool';

describe('splitNoteText', () => {
	it('splits one note per line and strips leading dashes', () => {
		expect(splitNoteText('Explore UI\n- Explore fluid\n\nImplement')).toEqual([
			'Explore UI',
			'Explore fluid',
			'Implement'
		]);
	});

	it('keeps a single line as one note', () => {
		expect(splitNoteText('just this')).toEqual(['just this']);
	});
});

describe('notesFromToolMessage', () => {
	it('reads the recited list and does not invent status', () => {
		const notes = notesFromToolMessage({
			toolResult: '# Notes\n- first thought\n- second thought\n- newest'
		});
		expect(notes).toEqual(['first thought', 'second thought', 'newest']);
	});

	it('hides a cleared list', () => {
		expect(notesFromToolMessage({ toolResult: '# Notes\n(empty)' })).toBeNull();
	});

	it('shows the note being added before a result arrives', () => {
		expect(
			notesFromToolMessage({
				toolInput: { action: 'add', text: 'wire up AskUser' }
			})
		).toEqual(['wire up AskUser']);
	});

	it('batches several notes from texts or multiline text while streaming', () => {
		expect(
			notesFromToolMessage({
				toolInput: {
					action: 'add',
					texts: ['step one', 'step two']
				}
			})
		).toEqual(['step one', 'step two']);

		expect(
			notesFromToolMessage({
				partialToolInput: '{"action": "add", "text": "alpha\\nbeta'
			})
		).toEqual(['alpha', 'beta']);
	});
});
