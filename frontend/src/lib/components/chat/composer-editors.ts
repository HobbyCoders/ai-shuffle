/**
 * The message box's editors, so a picker that borrowed focus (the new-session
 * project picker, Cmd/Ctrl+P) can put the caret back through the editor's own
 * selection rather than DOM text offsets, which cannot tell paragraphs or hard
 * breaks apart. Several copies can be mounted (the phone sheet keeps a
 * CSS-hidden one), so "the composer" is the focused one, else the visible one.
 */
import { Selection, TextSelection } from '@tiptap/pm/state';
import type { Editor } from './tiptap/editorConfig';

const editors = new Set<Editor>();

/** Called by TiptapEditor on mount; the returned function forgets it. */
export function trackComposerEditor(editor: Editor): () => void {
	editors.add(editor);
	return () => { editors.delete(editor); };
}

function live(): Editor[] {
	return [...editors].filter(editor => !editor.isDestroyed);
}

/** The composer that holds focus now, if any. */
export function focusedComposerEditor(): Editor | null {
	return live().find(editor => {
		const dom = editor.view.dom;
		return dom.contains(dom.ownerDocument.activeElement);
	}) ?? null;
}

/** The composer on screen: focused first, else the first one visible. */
export function visibleComposerEditor(): Editor | null {
	return focusedComposerEditor() ?? live().find(editor => {
		const dom = editor.view.dom as HTMLElement;
		return dom.isConnected && (typeof dom.checkVisibility !== 'function' || dom.checkVisibility());
	}) ?? null;
}

export type ComposerSelection = { from: number; to: number };

/**
 * How to put the caret back later: in the editor that was focused now, at its
 * selection; or, with nothing focused, at the end of whichever composer is on
 * screen then. A rebuilt editor (the composer remounted) gets the same
 * positions when they still fit its document, else the end.
 */
export function composerReturn(): () => void {
	const editor = focusedComposerEditor();
	const saved: ComposerSelection | null = editor ? { from: editor.state.selection.from, to: editor.state.selection.to } : null;
	return () => {
		const target = editor && !editor.isDestroyed && editor.view.dom.isConnected ? editor : visibleComposerEditor();
		if (!target) return;
		restoreComposerSelection(target, saved);
	};
}

/**
 * Focus `editor` with `saved` selected, or the caret at the end when unknown or
 * out of range. Synchronous (ProseMirror's own focus, which does not scroll):
 * TipTap's focus command waits a frame, and the user may already be typing.
 */
export function restoreComposerSelection(editor: Editor, saved: ComposerSelection | null) {
	const { doc, tr } = editor.state;
	const selection = saved && saved.to <= doc.content.size
		? TextSelection.between(doc.resolve(saved.from), doc.resolve(saved.to))
		: Selection.atEnd(doc);
	editor.view.dispatch(tr.setSelection(selection));
	editor.view.focus();
}
