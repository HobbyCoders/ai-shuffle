/**
 * Context a SettingRow gives the control inside it, so the control is
 * labelled by the row's label and described by its hint without the page
 * repeating either.
 */
import { getContext, setContext } from 'svelte';

export interface RowContext {
	/** Registry id, when the row has one. */
	readonly id: string | undefined;
	readonly label: string;
	readonly labelId: string;
	readonly hintId: string | undefined;
	/** DOM id of the row's expansion region. */
	readonly expansionId: string;
}

const ROW = Symbol('settings-row');
const DETAIL = Symbol('settings-detail');

export function setRowContext(ctx: RowContext): void {
	setContext(ROW, ctx);
}

export function getRowContext(): RowContext | undefined {
	return getContext<RowContext | undefined>(ROW);
}

/** DetailPane marks its subtree so RowGroup renders detail-style sections. */
export function setInDetail(): void {
	setContext(DETAIL, true);
}

export function isInDetail(): boolean {
	return getContext<boolean | undefined>(DETAIL) === true;
}

/**
 * aria attributes for a control: an explicit `label` wins; otherwise the row
 * labels and describes it.
 */
export function controlAria(row: RowContext | undefined, label: string | undefined): Record<string, string | undefined> {
	if (label) return { 'aria-label': label, 'aria-describedby': row?.hintId };
	if (row) return { 'aria-labelledby': row.labelId, 'aria-describedby': row.hintId };
	return {};
}

/**
 * What a control's `onchange` may return. A promise that resolves to `false`
 * or rejects means the save failed, and the control goes back to the value
 * it showed before the change. Plain return values are ignored, so
 * `(v) => (on = v)` is safe.
 */
export type ChangeResult = unknown;

/** Calls `revert` when an `onchange` result reports a failed save. */
export function settle(result: ChangeResult, revert: () => void): void {
	if (!(result instanceof Promise)) return;
	result.then(
		(ok) => {
			if (ok === false) revert();
		},
		() => revert()
	);
}
