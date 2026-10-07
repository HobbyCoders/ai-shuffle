/**
 * The emulator's colours, taken from the app's design tokens so the terminal
 * follows the light and dark themes. xterm.js wants plain colour strings, and
 * the tokens are `oklch()`, so each is resolved to rgb through a 1px canvas.
 */
import type { ITheme } from '@xterm/xterm';

let ctx: CanvasRenderingContext2D | null = null;

function rgb(value: string, fallback: string): string {
	if (!value) return fallback;
	ctx ??= document.createElement('canvas').getContext('2d', { willReadFrequently: true });
	if (!ctx) return fallback;
	ctx.clearRect(0, 0, 1, 1);
	ctx.fillStyle = fallback;
	ctx.fillStyle = value;
	ctx.fillRect(0, 0, 1, 1);
	const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
	return a === 255 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)})`;
}

export function terminalTheme(element: HTMLElement): ITheme {
	const css = getComputedStyle(element);
	const token = (name: string) => css.getPropertyValue(name).trim();
	const dark = document.documentElement.classList.contains('dark') || !document.documentElement.classList.contains('light');
	const foreground = rgb(token('--foreground'), dark ? '#e6e8eb' : '#1d2125');
	return {
		background: rgb(token('--code-bg'), dark ? '#0f1214' : '#f7f8fa'),
		foreground,
		cursor: rgb(token('--primary'), '#2bb3a3'),
		cursorAccent: rgb(token('--code-bg'), '#0f1214'),
		selectionBackground: rgb(`color-mix(in oklch, ${token('--primary') || '#2bb3a3'} 32%, transparent)`, 'rgba(43,179,163,0.3)'),
		// ANSI palette tuned to the Studio Noir accents (emerald, rose, blue, amber).
		black: dark ? '#1b1f23' : '#2a2f35',
		red: rgb(token('--destructive'), '#e5676b'),
		green: rgb(token('--success'), '#4fc98a'),
		yellow: rgb(token('--warning'), '#e6b450'),
		blue: rgb(token('--info'), '#6ea8fe'),
		magenta: dark ? '#c792ea' : '#8e4fc4',
		cyan: rgb(token('--primary'), '#2bb3a3'),
		white: dark ? '#c9ced4' : '#5c6670',
		brightBlack: dark ? '#5c6670' : '#7a838c',
		brightRed: dark ? '#ff8a8e' : '#c4383d',
		brightGreen: dark ? '#7fe0a8' : '#1f8a52',
		brightYellow: dark ? '#ffd27a' : '#a8741a',
		brightBlue: dark ? '#9cc3ff' : '#2f6fd6',
		brightMagenta: dark ? '#e0b5ff' : '#7a3bb0',
		brightCyan: dark ? '#6fe0d2' : '#147f73',
		brightWhite: dark ? '#f5f7f9' : '#1d2125',
	};
}
