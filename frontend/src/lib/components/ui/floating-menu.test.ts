/**
 * floatingMenu: menus that must escape their trigger's stacking context and
 * clipping (the new-session agent card's agent and "Runs on" menus, #371).
 */
import { afterEach, describe, expect, it } from 'vitest';
import { computeFloatingPosition, floatingMenu, FLOATING_MENU_Z } from './floating-menu';

const view = { width: 1440, height: 900 };
const rect = (top: number, left: number, width = 120, height = 32) => ({ top, left, bottom: top + height, right: left + width });

describe('computeFloatingPosition', () => {
	it('opens below the anchor, start-aligned, when it fits', () => {
		const pos = computeFloatingPosition(rect(100, 200), { width: 280, height: 400 }, view);
		expect(pos).toMatchObject({ placement: 'bottom', top: 138, left: 200 });
		expect(pos.maxHeight).toBe(900 - 8 - 138);
	});

	it('end-aligns to the anchor’s right edge', () => {
		const pos = computeFloatingPosition(rect(100, 1000), { width: 240, height: 200 }, view, { align: 'end' });
		expect(pos.left).toBe(1120 - 240);
	});

	it('centres under the anchor', () => {
		const pos = computeFloatingPosition(rect(100, 1000), { width: 240, height: 200 }, view, { align: 'center' });
		expect(pos.left).toBe(1000 + (120 - 240) / 2);
	});

	it('flips above when the space below is short and above is roomier', () => {
		const pos = computeFloatingPosition(rect(700, 200), { width: 280, height: 400 }, view);
		expect(pos.placement).toBe('top');
		expect(pos.top).toBe(700 - 6 - 400);
		expect(pos.maxHeight).toBe(700 - 6 - 8);
	});

	it('stays below and caps its height when below is still the roomier side', () => {
		const pos = computeFloatingPosition(rect(300, 200), { width: 280, height: 800 }, view);
		expect(pos.placement).toBe('bottom');
		expect(pos.maxHeight).toBe(900 - 8 - 338);
	});

	it('shifts inside the viewport on a narrow phone', () => {
		const phone = { width: 390, height: 844 };
		const right = computeFloatingPosition(rect(200, 300, 80), { width: 280, height: 200 }, phone);
		expect(right.left).toBe(390 - 8 - 280);
		const left = computeFloatingPosition(rect(200, 20, 80), { width: 280, height: 200 }, phone, { align: 'end' });
		expect(left.left).toBe(8);
	});
});

describe('floatingMenu action', () => {
	afterEach(() => { document.body.innerHTML = ''; });

	it('moves the menu to <body>, fixes it above the page, and removes it on destroy', () => {
		const card = document.createElement('section');
		card.style.isolation = 'isolate';
		const trigger = document.createElement('button');
		const menu = document.createElement('div');
		card.append(trigger, menu);
		document.body.append(card);

		const action = floatingMenu(menu, { anchor: trigger });
		expect(menu.parentElement).toBe(document.body);
		expect(menu.style.position).toBe('fixed');
		expect(menu.style.zIndex).toBe(String(FLOATING_MENU_Z));
		expect(menu.dataset.placement).toBe('bottom');

		action.destroy();
		expect(menu.isConnected).toBe(false);
	});

	it('leaves the node in place when disabled', () => {
		const wrap = document.createElement('div');
		const menu = document.createElement('div');
		wrap.append(menu);
		document.body.append(wrap);
		floatingMenu(menu, { anchor: wrap, enabled: false });
		expect(menu.parentElement).toBe(wrap);
		expect(menu.style.position).toBe('');
	});
});
