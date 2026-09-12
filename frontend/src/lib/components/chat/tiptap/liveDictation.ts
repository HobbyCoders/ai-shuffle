import type { Editor } from '@tiptap/core';
import type { Transaction } from '@tiptap/pm/state';

/** Tracks only characters inserted by dictation. User edits are never owned. */
export function createLiveDictation(editor: Editor) {
	let previous = '';
	let positions: Array<number | null> = [];
	let anchor: number | null = null;
	let needsSpace = false;
	let writing = false;

	const mapEdits = ({ transaction }: { transaction: Transaction }) => {
		if (writing || !transaction.docChanged || anchor === null) return;
		positions = positions.map((position) => {
			if (position === null) return null;
			const start = transaction.mapping.mapResult(position, 1);
			const end = transaction.mapping.mapResult(position + 1, -1);
			return start.deletedAcross || end.deletedAcross || end.pos <= start.pos ? null : start.pos;
		});
		anchor = transaction.mapping.map(anchor, 1);
	};
	editor.on('transaction', mapEdits);

	function commit() {
		previous = '';
		positions = [];
		anchor = null;
		needsSpace = false;
	}

	function write(text: string) {
		if (editor.isDestroyed) return;
		const tr = editor.state.tr;
		if (anchor === null) {
			if (!text) return;
			// Insert at the saved caret without deleting selected pre-existing text.
			anchor = editor.state.selection.from;
			const before = tr.doc.textBetween(Math.max(0, anchor - 1), anchor);
			needsSpace = Boolean(before && !/\s$/.test(before) && !/^\s/.test(text));
		}
		if (text && needsSpace) text = ' ' + text;
		if (text === previous) return;

		let commonStart = 0;
		while (commonStart < previous.length && commonStart < text.length && previous[commonStart] === text[commonStart]) commonStart++;
		let commonEnd = 0;
		while (commonEnd < previous.length - commonStart && commonEnd < text.length - commonStart && previous[previous.length - 1 - commonEnd] === text[text.length - 1 - commonEnd]) commonEnd++;
		const oldEnd = previous.length - commonEnd;
		const replacement = text.slice(commonStart, text.length - commonEnd);
		const changed = positions.slice(commonStart, oldEnd).filter((at): at is number => at !== null);
		const left = positions.slice(0, commonStart).reverse().find((at) => at !== null);
		const right = positions.slice(oldEnd).find((at) => at !== null);
		let insertion = changed[0] ?? right ?? (left !== undefined && left !== null ? left + 1 : anchor);
		// Delete only dictation-owned characters, backwards. Any user-inserted
		// characters between them survive the model's final correction.
		for (let i = changed.length - 1; i >= 0; i--) tr.delete(changed[i], changed[i] + 1);
		insertion = tr.mapping.map(insertion, -1);
		const beforePositions = positions.slice(0, commonStart).map((at) => at === null ? null : tr.mapping.map(at, 1));
		const afterPositions = positions.slice(oldEnd).map((at) => at === null ? null : tr.mapping.map(at, 1));
		if (replacement) tr.insertText(replacement, insertion);
		positions = [
			...beforePositions.map((at) => at !== null && at >= insertion ? at + replacement.length : at),
			...Array.from({ length: replacement.length }, (_, i) => insertion + i),
			...afterPositions.map((at) => at !== null && at >= insertion ? at + replacement.length : at)
		];
		anchor = insertion + replacement.length;
		previous = text;
		// No focus()/selection reset: a reader can move their caret while speech
		// continues. ProseMirror maps their selection through this transaction.
		writing = true;
		try { editor.view.dispatch(tr); } finally { writing = false; }
	}

	function discard() {
		// Reconcile to empty using the same mapped character ownership as a final
		// correction. Restoring a draft snapshot would erase intervening user edits.
		write('');
		commit();
	}

	return { write, commit, discard,
		moveTo(next: Editor) {
			editor.off('transaction', mapEdits);
			editor = next;
			editor.on('transaction', mapEdits);
		},
		destroy() { editor.off('transaction', mapEdits); commit(); }
	};
}
