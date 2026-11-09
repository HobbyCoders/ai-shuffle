import type { Editor } from '@tiptap/core';
import type { Node as ProseMirrorNode } from '@tiptap/pm/model';
import { isHistoryTransaction } from '@tiptap/pm/history';
import { Selection, type Transaction } from '@tiptap/pm/state';
import type { FileChipAttributes } from './FileChipNode';

/** Transaction meta: the chip's own remove control did this, so it is not silent. */
export const EXPLICIT_CHIP_REMOVAL = 'fileChipExplicitRemoval';

interface PlacedChip {
	attrs: FileChipAttributes;
	pos: number;
}

/** Shows the owner what went and how to get it back; returns how to withdraw that offer. */
export type ChipRemovalNotice = (filenames: string[], undo: () => void) => () => void;

function chipsIn(doc: ProseMirrorNode): PlacedChip[] {
	const chips: PlacedChip[] = [];
	doc.descendants((node, pos) => {
		if (node.type.name === 'fileChip') chips.push({ attrs: node.attrs as FileChipAttributes, pos });
	});
	return chips;
}

const chipKey = (attrs: FileChipAttributes) => attrs.fullPath || attrs.path;

/** The chips of `before` that have no counterpart left in `after`. */
export function removedChips(before: ProseMirrorNode, after: ProseMirrorNode): PlacedChip[] {
	const remaining = new Map<string, number>();
	for (const { attrs } of chipsIn(after)) {
		remaining.set(chipKey(attrs), (remaining.get(chipKey(attrs)) ?? 0) + 1);
	}
	return chipsIn(before).filter(({ attrs }) => {
		const left = remaining.get(chipKey(attrs)) ?? 0;
		if (left > 0) remaining.set(chipKey(attrs), left - 1);
		return left === 0;
	});
}

/**
 * A keystroke (Backspace, or typing over a selection) can take a chip with it.
 * Every such removal offers an undo. Replacing the whole draft (send, switching
 * chats) and the editor's own undo withdraw the offer instead: the chip would
 * otherwise come back into a different message.
 */
export function createChipRemovalGuard(editor: Editor, notice: ChipRemovalNotice) {
	let pending: PlacedChip[] = [];
	let withdraw: (() => void) | null = null;

	function settle() {
		withdraw?.();
		withdraw = null;
		pending = [];
	}

	function restore() {
		const chips = [...pending].sort((a, b) => b.pos - a.pos);
		settle();
		if (editor.isDestroyed || !chips.length) return;
		const { tr, schema } = editor.state;
		for (const { attrs, pos } of chips) {
			const $at = tr.doc.resolve(Math.min(pos, tr.doc.content.size));
			const at = $at.parent.inlineContent ? $at.pos : Selection.near($at).from;
			const next = tr.doc.resolve(at).nodeAfter;
			const spaced = !!next?.isText && !!next.text?.startsWith(' ');
			tr.insert(at, spaced
				? schema.nodes.fileChip.create(attrs)
				: [schema.nodes.fileChip.create(attrs), schema.text(' ')]);
		}
		editor.view.dispatch(tr);
	}

	function observe(tr: Transaction) {
		if (!tr.docChanged) return;
		// Tiptap's setContent / clearContent always set this meta.
		if (tr.getMeta('preventUpdate') !== undefined || isHistoryTransaction(tr)) {
			settle();
			return;
		}
		pending = pending.map((chip) => ({ ...chip, pos: tr.mapping.map(chip.pos) }));
		if (tr.getMeta(EXPLICIT_CHIP_REMOVAL)) return;
		const removed = removedChips(tr.before, tr.doc);
		if (!removed.length) return;
		withdraw?.();
		pending = [...pending, ...removed.map((chip) => ({ ...chip, pos: tr.mapping.map(chip.pos) }))];
		withdraw = notice(pending.map((chip) => chip.attrs.filename), restore);
	}

	editor.on('destroy', settle);
	return { observe };
}
