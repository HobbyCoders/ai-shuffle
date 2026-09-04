import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	APP_VIEWPORT_HEIGHT_VAR,
	applyVisualViewportHeight,
	bindVisualViewport,
	visualViewportCssHeight
} from './visual-viewport';

describe('visualViewportCssHeight', () => {
	it('prefers a positive visual viewport height', () => {
		expect(visualViewportCssHeight(432, 800)).toBe('432px');
	});

	it('falls back to innerHeight when visual viewport is missing or zero', () => {
		expect(visualViewportCssHeight(undefined, 800)).toBe('800px');
		expect(visualViewportCssHeight(0, 800)).toBe('800px');
	});
});

describe('applyVisualViewportHeight', () => {
	it(`writes ${APP_VIEWPORT_HEIGHT_VAR}`, () => {
		const el = document.createElement('div');
		applyVisualViewportHeight(el.style, 500, 800);
		expect(el.style.getPropertyValue(APP_VIEWPORT_HEIGHT_VAR)).toBe('500px');
	});
});

describe('bindVisualViewport', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('sets the CSS var from visualViewport.height and updates on resize', () => {
		const viewport = new EventTarget() as EventTarget & { height: number };
		viewport.height = 800;
		vi.stubGlobal('visualViewport', viewport);
		Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });

		const root = document.createElement('div');
		const stop = bindVisualViewport(root);
		expect(root.style.getPropertyValue(APP_VIEWPORT_HEIGHT_VAR)).toBe('800px');

		viewport.height = 410;
		viewport.dispatchEvent(new Event('resize'));
		expect(root.style.getPropertyValue(APP_VIEWPORT_HEIGHT_VAR)).toBe('410px');

		stop();
		expect(root.style.getPropertyValue(APP_VIEWPORT_HEIGHT_VAR)).toBe('');
	});

	it('falls back to innerHeight when visualViewport is absent', () => {
		vi.stubGlobal('visualViewport', undefined);
		Object.defineProperty(window, 'innerHeight', { configurable: true, value: 720 });

		const root = document.createElement('div');
		const stop = bindVisualViewport(root);
		expect(root.style.getPropertyValue(APP_VIEWPORT_HEIGHT_VAR)).toBe('720px');
		stop();
	});
});

describe('keyboard viewport contract', () => {
	it('opts the document into content-resizing around the virtual keyboard', () => {
		const appHtml = readFileSync(resolve(process.cwd(), 'src/app.html'), 'utf8');
		expect(appHtml).toMatch(
			/name="viewport"[^>]*interactive-widget=resizes-content/
		);
		expect(appHtml).toContain('viewport-fit=cover');
	});

	it('keeps the layout viewport meta aligned with app.html', () => {
		const layout = readFileSync(resolve(process.cwd(), 'src/routes/+layout.svelte'), 'utf8');
		expect(layout).toContain('interactive-widget=resizes-content');
		expect(layout).toContain('viewport-fit=cover');
		expect(layout).toContain('bindVisualViewport');
	});

	it('sizes the app shell to --app-viewport-height', () => {
		const shell = readFileSync(
			resolve(process.cwd(), 'src/lib/components/layout/AppLayout.svelte'),
			'utf8'
		);
		expect(shell).toContain('height: var(--app-viewport-height, 100dvh)');
	});
});
