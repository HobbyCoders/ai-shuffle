import type { HtmlPreviewTheme } from './htmlPreview';

/**
 * The app tokens an HTML preview receives as CSS custom properties. Pages
 * written for the chat use these to look like part of the reply. The agent
 * guidance in app/core/environment.py lists the same names.
 */
export const HTML_PREVIEW_THEME_TOKENS = [
	'--background', '--foreground', '--card', '--card-foreground', '--popover', '--popover-foreground',
	'--primary', '--primary-foreground', '--secondary', '--secondary-foreground',
	'--muted', '--muted-foreground', '--accent', '--accent-foreground',
	'--destructive', '--success', '--warning', '--info',
	'--border', '--input', '--ring', '--code-bg', '--radius', '--font-sans', '--font-mono'
] as const;

/** Categorical series for charts, drawn from the theme's own status hues. */
const CHART_SERIES = ['--primary', '--info', '--warning', '--success', '--destructive'] as const;

export function readHostTheme(root: HTMLElement = document.documentElement): HtmlPreviewTheme {
	const style = getComputedStyle(root);
	const variables: Record<string, string> = {};
	for (const name of HTML_PREVIEW_THEME_TOKENS) {
		const value = style.getPropertyValue(name).trim();
		if (value) variables[name] = value;
	}
	CHART_SERIES.forEach((name, index) => {
		if (variables[name]) variables[`--chart-${index + 1}`] = variables[name];
	});
	return { appearance: root.classList.contains('light') ? 'light' : 'dark', variables };
}

const listeners = new Set<(theme: HtmlPreviewTheme) => void>();
let stopWatching: (() => void) | null = null;
let current: HtmlPreviewTheme | null = null;
let currentKey = '';

function refresh() {
	const next = readHostTheme();
	const key = JSON.stringify(next);
	if (key === currentKey) return;
	current = next;
	currentKey = key;
	for (const listener of listeners) listener(next);
}

/** The live app theme, shared by every preview frame (one observer for all). */
export function currentHostTheme(): HtmlPreviewTheme {
	if (!current) {
		current = readHostTheme();
		currentKey = JSON.stringify(current);
	}
	return current;
}

/**
 * Call `listener` whenever the app's theme, preset or light/dark mode changes.
 * The theme store writes classes, a preset attribute and inline variables on
 * <html>; watching those catches every path that changes the theme.
 */
export function watchHostTheme(listener: (theme: HtmlPreviewTheme) => void): () => void {
	listeners.add(listener);
	if (!stopWatching && typeof MutationObserver !== 'undefined') {
		currentHostTheme();
		let frame = 0;
		const schedule = () => {
			if (frame) return;
			frame = requestAnimationFrame(() => { frame = 0; refresh(); });
		};
		const observer = new MutationObserver(schedule);
		observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style', 'data-theme-preset'] });
		const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null;
		media?.addEventListener?.('change', schedule);
		stopWatching = () => {
			observer.disconnect();
			media?.removeEventListener?.('change', schedule);
			if (frame) cancelAnimationFrame(frame);
		};
	}
	return () => {
		listeners.delete(listener);
		if (!listeners.size && stopWatching) {
			stopWatching();
			stopWatching = null;
			current = null;
			currentKey = '';
		}
	};
}
