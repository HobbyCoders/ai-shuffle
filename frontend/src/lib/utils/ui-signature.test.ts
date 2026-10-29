import { describe, expect, it } from 'vitest';
import { UI_MOUNT, uiComposer, uiFlag, uiSessionTag } from './ui-signature';

describe('ui-signature', () => {
	it('tags a bound session with a short stable hash, never the id', () => {
		const id = '7f3c2a10-8b44-4d1e-9a51-2f6c0b9d1e77';
		const tag = uiSessionTag(id);
		expect(tag).toMatch(/^s:[0-9a-z]{6}$/);
		expect(uiSessionTag(id)).toBe(tag);
		expect(tag).not.toContain(id.slice(0, 6));
		expect(uiSessionTag('7f3c2a10-8b44-4d1e-9a51-2f6c0b9d1e78')).not.toBe(tag);
		expect(uiSessionTag(null)).toBe('none');
		expect(uiSessionTag(undefined)).toBe('none');
		expect(uiSessionTag('')).toBe('none');
	});

	it('places the composer', () => {
		expect(uiComposer({ archived: false, emptyHero: true })).toBe('centre');
		expect(uiComposer({ archived: false, emptyHero: false })).toBe('bottom');
		expect(uiComposer({ archived: true, emptyHero: true })).toBe('hidden');
	});

	it('spells flags as strings and names the mount markers', () => {
		expect([uiFlag(true), uiFlag(0), uiFlag(undefined)]).toEqual(['true', 'false', 'false']);
		expect(Object.values(UI_MOUNT)).toEqual(['ChatView', 'ChatInput', 'NewSessionHero', 'MessageArea', 'SidePanel']);
	});
});
