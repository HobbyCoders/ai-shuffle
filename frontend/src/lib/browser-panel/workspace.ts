export interface BrowserViewport {
	mode: 'fit' | 'fixed';
	width: number;
	height: number;
}

export interface BrowserEditor {
  id: string; type: string; label: string; value: string;
  x: number; y: number; width: number; height: number;
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
		!object(value.control)
	) {
		return false;
	}

	if (value.editors !== undefined && (!Array.isArray(value.editors) || value.editors.length > 32 || !value.editors.every((field) =>
		object(field) && id(field.id) && ['text', 'search', 'email', 'url', 'tel', 'password', 'textarea'].includes(String(field.type)) &&
		typeof field.label === 'string' && field.label.length <= 256 && typeof field.value === 'string' && field.value.length <= 16384 &&
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
