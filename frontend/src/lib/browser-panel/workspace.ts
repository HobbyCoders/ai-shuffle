import { PICKER_KINDS } from './pickers';

export interface BrowserViewport {
	mode: 'fit' | 'fixed';
	width: number;
	height: number;
}

/** A page field's computed text metrics in unscaled CSS px (newer runtimes). */
export interface BrowserEditorStyle {
  fontSize: number; lineHeight: number | null; paddingLeft: number; paddingTop: number; fontFamily: string;
  /** Computed border widths; absent from runtimes that predate them. */
  borderLeft?: number; borderTop?: number;
}

export interface BrowserEditor {
  /** For a select, `value` is the chosen option's label (what it shows). */
  id: string; type: string; label: string; value: string;
  accept?: string; multiple?: boolean; chooser?: boolean; keys?: boolean;
  /** Date/time pickers: the page's own min/max/step attributes, unparsed; its
   * value attribute (`base`, where steps count from without a min); and the
   * remote browser's IANA time zone (`zone`), for Today and Now. */
  min?: string; max?: string; step?: string; base?: string; zone?: string;
  /** A picker control (select, date, time…) that has the page's focus. */
  focused?: boolean;
  x: number; y: number; width: number; height: number;
  style?: BrowserEditorStyle;
}

// Same limits as the runtime and server: any CSS px value 0..1000, families
// up to 200 characters. Pages really report 0 (hidden inputs) and big values.
const metric = (v: unknown) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1000;
function editorStyle(v: unknown): boolean {
	return object(v) && metric(v.fontSize) && (v.lineHeight === null || metric(v.lineHeight)) &&
		metric(v.paddingLeft) && metric(v.paddingTop) && typeof v.fontFamily === 'string' && v.fontFamily.length <= 200 &&
		(v.borderLeft === undefined || metric(v.borderLeft)) && (v.borderTop === undefined || metric(v.borderTop));
}

/**
 * `style` is only a hint for lining up text. One that cannot be used is
 * removed from that field (it falls back to estimated metrics) and never
 * fails the whole workspace update. Mutates the freshly parsed field.
 */
function dropUnusableStyle(field: unknown) {
	if (object(field) && field.style !== undefined && !editorStyle(field.style)) delete field.style;
}

export interface BrowserWorkspaceState {
	editors?: BrowserEditor[];
	runtime_id: string;
	revision: number;
	available: boolean;
	target_id: string | null;
	active_tab_id: string | null;
	tabs: { id: string; url: string; title: string; current: boolean }[];
	viewport: BrowserViewport;
	control: {
		owner: 'agent' | 'human';
		viewer_id: string | null;
		pending_viewer_id: string | null;
	};
	busy: boolean;
	/** Plain notice for the viewer, e.g. a captcha the site withheld from this browser. */
	notice?: string;
}

const TEXT_FIELDS = ['text', 'search', 'email', 'url', 'tel', 'password', 'textarea', 'file'];

/** Picker limits (short strings) and the focus flag: valid, and only on pickers. */
function pickerDetailsOk(field: Record<string, unknown>): boolean {
	if (!(PICKER_KINDS as readonly string[]).includes(String(field.type)))
		return ['min', 'max', 'step', 'base', 'zone', 'focused'].every((k) => field[k] === undefined);
	return ['min', 'max', 'step', 'base', 'zone'].every((k) => field[k] === undefined || (typeof field[k] === 'string' && (field[k] as string).length <= 64)) &&
		(field.focused === undefined || typeof field.focused === 'boolean');
}

function object(value: unknown): value is Record<string, unknown> {
	return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function id(value: unknown): value is string {
	return typeof value === 'string' && value.length > 0 && value.length <= 256;
}

function nullableId(value: unknown): value is string | null {
	return value === null || id(value);
}

function validWorkspaceViewport(value: unknown): value is BrowserViewport {
	return (
		object(value) &&
		(value.mode === 'fit' || value.mode === 'fixed') &&
		typeof value.width === 'number' &&
		Number.isInteger(value.width) &&
		typeof value.height === 'number' &&
		Number.isInteger(value.height) &&
		value.width >= 1 &&
		value.width <= 4096 &&
		value.height >= 1 &&
		value.height <= 4096
	);
}

export function validViewport(value: unknown): value is BrowserViewport {
	return (
		validWorkspaceViewport(value) &&
		value.width >= 240 &&
		value.width <= 3840 &&
		value.height >= 240 &&
		value.height <= 2160
	);
}

/** Validate untrusted wire state before exposing it to reactive consumers. */
export function isBrowserWorkspaceState(
	value: unknown,
): value is BrowserWorkspaceState {
	if (
		!object(value) ||
		!id(value.runtime_id) ||
		typeof value.revision !== 'number' ||
		!Number.isSafeInteger(value.revision) ||
		value.revision < 0 ||
		typeof value.available !== 'boolean' ||
		typeof value.busy !== 'boolean' ||
		!nullableId(value.target_id) ||
		!nullableId(value.active_tab_id) ||
		!validWorkspaceViewport(value.viewport) ||
		!Array.isArray(value.tabs) ||
		value.tabs.length > 128 ||
		!object(value.control) ||
		(value.notice !== undefined &&
			(typeof value.notice !== 'string' || value.notice.length > 512))
	) {
		return false;
	}

	if (Array.isArray(value.editors) && value.editors.length <= 32) value.editors.forEach(dropUnusableStyle);
	if (value.editors !== undefined && (!Array.isArray(value.editors) || value.editors.length > 32 || !value.editors.every((field) =>
		object(field) && id(field.id) && [...TEXT_FIELDS, ...PICKER_KINDS].includes(String(field.type)) &&
		pickerDetailsOk(field) &&
		typeof field.label === 'string' && field.label.length <= 256 && typeof field.value === 'string' && field.value.length <= 16384 &&
		(field.accept === undefined || (typeof field.accept === 'string' && field.accept.length <= 1024)) &&
		(field.multiple === undefined || typeof field.multiple === 'boolean') &&
		(field.chooser === undefined || typeof field.chooser === 'boolean') &&
		(field.keys === undefined || typeof field.keys === 'boolean') &&
		(field.type !== 'password' || field.value === '') && ['x', 'y', 'width', 'height'].every(k => typeof field[k] === 'number' && Number.isFinite(field[k])) &&
		(field.width as number) > 0 && (field.height as number) > 0))) return false;
	const ids = new Set<string>();
	for (const tab of value.tabs) {
		if (
			!object(tab) ||
			!id(tab.id) ||
			ids.has(tab.id) ||
			typeof tab.url !== 'string' ||
			typeof tab.title !== 'string' ||
			typeof tab.current !== 'boolean' ||
			tab.current !== (tab.id === value.active_tab_id)
		) {
			return false;
		}
		ids.add(tab.id);
	}
	if (
		value.active_tab_id === null
			? ids.size !== 0
			: !ids.has(value.active_tab_id)
	)
		return false;

	const control = value.control;
	return (
		(control.owner === 'agent' || control.owner === 'human') &&
		nullableId(control.viewer_id) &&
		nullableId(control.pending_viewer_id) &&
		(control.owner === 'human'
			? id(control.viewer_id)
			: control.viewer_id === null)
	);
}

export function ownsControl(
	state: BrowserWorkspaceState | null,
	viewer: string | null,
): boolean {
	return !!(
		state?.available &&
		viewer &&
		state.control.owner === 'human' &&
		state.control.viewer_id === viewer
	);
}
