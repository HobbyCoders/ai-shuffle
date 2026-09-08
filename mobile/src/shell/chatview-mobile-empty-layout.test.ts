/**
 * The mobile empty-chat surface: scrolling hero, sticky composer and the
 * `data-panel-flow` fallback. These rules used to be scoped inside
 * ChatView.svelte; they are global rules in mobile.css now (the web build
 * carries none of them), so this pins the ChatView section of that sheet.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const css = readFileSync(resolve(process.cwd(), 'src/mobile.css'), 'utf8');
const start = css.indexOf('/* ── frontend/src/lib/components/layout/ChatView.svelte');
const end = css.indexOf('/* ── ', start + 1);
const section = css.slice(start, end === -1 ? undefined : end);
const MOBILE = "html:root[data-platform='mobile']";
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
/** Declarations of the mobile rule for `selector`. */
const body = (selector: string) =>
	section.match(new RegExp(`${esc(MOBILE)} ${esc(selector)}\\s*\\{([^}]*)\\}`))?.[1] ?? '';

describe('ChatView — stable mobile empty layout', () => {
	it('is a section of the mobile sheet', () => {
		expect(start).toBeGreaterThan(-1);
	});
	it('retains the action-applied panel-flow fallback', () => {
		expect(body('.chat-view.is-empty .input-slot.empty-input-layout[data-panel-flow]')).toMatch(/position: static;/);
	});
	it('scrolls the empty surface with a nonshrinking top-aligned invitation', () => {
		expect(body('.chat-view.is-empty')).toMatch(/overflow-y: auto;/);
		expect(body('.is-empty .empty-hero')).toMatch(/flex: 0 0 auto;[\s\S]*justify-content: flex-start;[\s\S]*padding-top: 1rem;/);
		expect(body('.is-empty .empty-bottom-spacer')).toMatch(/display: none;/);
	});
	it('keeps a sticky, unclipped composer with a normal-flow overflow fallback', () => {
		expect(body('.chat-view.is-empty .input-slot.empty-input-layout')).toMatch(/position: sticky;[\s\S]*bottom: 0;/);
		expect(body('.chat-view.is-empty .input-slot.empty-input-layout.composer-overflow')).toMatch(/position: static;/);
	});
});
