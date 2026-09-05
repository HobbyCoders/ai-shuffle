import { afterEach, describe, expect, it } from 'vitest';
import {
	canStartPanelDismiss,
	cancelPanelGesture,
	createPanelGesture,
	finishPanelGesture,
	updatePanelGesture
} from './mobile-panel-gesture';

afterEach(() => document.body.replaceChildren());

function scrollContainer(parent: HTMLElement, scrollTop = 0) {
	const element = document.createElement('div');
	element.style.overflowY = 'auto';
	Object.defineProperties(element, {
		clientHeight: { value: 120 },
		scrollHeight: { value: 480 }
	});
	element.scrollTop = scrollTop;
	parent.append(element);
	return element;
}

function nestedPanel() {
	const panel = scrollContainer(document.body);
	const wrapper = scrollContainer(panel);
	const list = scrollContainer(wrapper);
	const option = document.createElement('button');
	option.setAttribute('role', 'option');
	const label = document.createElement('span');
	label.textContent = 'Agent 01';
	option.append(label);
	list.append(option);
	return { panel, wrapper, list, option, label };
}

const start = { x: 100, y: 100, time: 0 };
const point = (x: number, y: number, time = 500) => ({ x, y, time });

describe('panel dismiss scroll-chain eligibility', () => {
	it('allows a gesture on a list option only while its entire ancestor chain is at top', () => {
		const { panel, label } = nestedPanel();
		expect(canStartPanelDismiss(label, panel)).toBe(true);
		expect(canStartPanelDismiss(panel, panel)).toBe(true);
	});

	it.each(['list', 'wrapper', 'panel'] as const)('rejects a scrolled %s even when all other ancestors are at top', (name) => {
		const elements = nestedPanel();
		elements[name].scrollTop = 37;
		expect(canStartPanelDismiss(elements.label, elements.panel)).toBe(false);
	});

	it('does not treat a positive fractional scroll offset as already at top', () => {
		const { panel, list, label } = nestedPanel();
		list.scrollTop = 0.25;
		expect(canStartPanelDismiss(label, panel)).toBe(false);
	});

	it('accepts negative elastic overscroll as the top boundary', () => {
		const { panel, list, label } = nestedPanel();
		list.scrollTop = -12;
		expect(canStartPanelDismiss(label, panel)).toBe(true);
	});

	it('ignores unrelated lists and scrolling outside the controls panel', () => {
		const { panel, label } = nestedPanel();
		scrollContainer(panel, 80);
		document.body.scrollTop = 60;
		expect(canStartPanelDismiss(label, panel)).toBe(true);
	});

	it('rejects targets outside the panel and missing targets', () => {
		const { panel } = nestedPanel();
		const elsewhere = document.createElement('button');
		document.body.append(elsewhere);
		expect(canStartPanelDismiss(elsewhere, panel)).toBe(false);
		expect(canStartPanelDismiss(null, panel)).toBe(false);
	});

	it.each(['input', 'textarea', 'select'])('leaves %s editing and native interaction alone', (tag) => {
		const { panel, list } = nestedPanel();
		const input = document.createElement(tag);
		list.append(input);
		expect(canStartPanelDismiss(input, panel)).toBe(false);
	});

	it('leaves text selection within an editable ancestor alone', () => {
		const { panel, option, label } = nestedPanel();
		option.setAttribute('contenteditable', 'true');
		expect(canStartPanelDismiss(label, panel)).toBe(false);
	});
});

describe('panel dismiss gesture ownership', () => {
	it('keeps a gesture native when a nested list reaches the top midway through it', () => {
		const { panel, list, label } = nestedPanel();
		list.scrollTop = 40;
		const gesture = createPanelGesture({ ...start, eligible: canStartPanelDismiss(label, panel) });
		expect(updatePanelGesture(gesture, point(100, 125)).mode).toBe('scroll');
		list.scrollTop = 0;
		expect(canStartPanelDismiss(label, panel)).toBe(true);
		expect(updatePanelGesture(gesture, point(100, 230))).toEqual({ mode: 'scroll', offset: 0 });
		expect(finishPanelGesture(gesture, point(100, 230), 240)).toBe(false);

		const nextGesture = createPanelGesture({ ...start, eligible: canStartPanelDismiss(label, panel) });
		expect(updatePanelGesture(nextGesture, point(100, 200)).mode).toBe('dismiss');
		expect(finishPanelGesture(nextGesture, point(100, 200), 240)).toBe(true);
	});

	it('keeps a gesture native when the outer panel reaches the top midway through it', () => {
		const { panel, label } = nestedPanel();
		panel.scrollTop = 20;
		const gesture = createPanelGesture({ ...start, eligible: canStartPanelDismiss(label, panel) });
		panel.scrollTop = 0;
		expect(updatePanelGesture(gesture, point(100, 240))).toEqual({ mode: 'scroll', offset: 0 });
		expect(finishPanelGesture(gesture, point(100, 240), 240)).toBe(false);
	});

	it('preserves taps and small movement without starting a dismiss', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		expect(updatePanelGesture(gesture, point(102, 104, 10))).toEqual({ mode: 'pending', offset: 0 });
		expect(finishPanelGesture(gesture, point(102, 104, 20), 240)).toBe(false);
	});

	it.each([
		['upward', 100, 80],
		['horizontal', 125, 105],
		['diagonal tie', 120, 120]
	])('locks an initially %s gesture to scrolling even if it later turns downward', (_, x, y) => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		expect(updatePanelGesture(gesture, point(Number(x), Number(y))).mode).toBe('scroll');
		expect(updatePanelGesture(gesture, point(100, 240))).toEqual({ mode: 'scroll', offset: 0 });
		expect(finishPanelGesture(gesture, point(100, 240), 240)).toBe(false);
	});

	it('dismisses a deliberate downward pull that starts with every container at top', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		expect(updatePanelGesture(gesture, point(103, 145))).toEqual({ mode: 'dismiss', offset: 45 });
		expect(finishPanelGesture(gesture, point(103, 180, 900), 240)).toBe(true);
	});

	it('settles an incomplete slow pull without closing', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(gesture, point(100, 135));
		expect(finishPanelGesture(gesture, point(100, 135, 900), 240)).toBe(false);
	});

	it('requires a meaningful travel distance even for a fast flick', () => {
		const tiny = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(tiny, point(100, 114, 5));
		expect(finishPanelGesture(tiny, point(100, 114, 10), 240)).toBe(false);
		const flick = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(flick, point(100, 126, 20));
		expect(finishPanelGesture(flick, point(100, 126, 30), 240)).toBe(true);
	});

	it('does not collapse an extremely short panel from a tiny slow pull', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(gesture, point(100, 120));
		expect(finishPanelGesture(gesture, point(100, 120, 900), 30)).toBe(false);
	});

	it('does not close when a downward pull is retracted before release', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(gesture, point(100, 200));
		updatePanelGesture(gesture, point(100, 104, 900));
		expect(finishPanelGesture(gesture, point(100, 104, 1000), 240)).toBe(false);
	});

	it('makes cancellation terminal so a late release cannot close the panel', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(gesture, point(100, 210));
		cancelPanelGesture(gesture);
		expect(updatePanelGesture(gesture, point(100, 260)).offset).toBe(0);
		expect(finishPanelGesture(gesture, point(100, 260), 240)).toBe(false);
	});

	it('finishes at most once when duplicate release events arrive', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(gesture, point(100, 200));
		expect(finishPanelGesture(gesture, point(100, 200), 240)).toBe(true);
		expect(finishPanelGesture(gesture, point(100, 200), 240)).toBe(false);
	});
});
