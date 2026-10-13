import { describe, expect, it } from 'vitest';
import {
	BYPASS, STEP_DISTANCE, VelocityTracker, dragAxis, knobFraction, settlesClosed, settlesOpen, slideAnswer, stepValue, taskSummary
} from './quick-controls';

const ACCESS = ['default', 'auto', 'acceptEdits', BYPASS];
const EFFORT = ['low', 'medium', 'high', 'xhigh', 'max'];

describe('sideways steps on the quick bar', () => {
	it('moves one level per swipe, right up and left down', () => {
		expect(stepValue(EFFORT, 'high', STEP_DISTANCE)).toBe('xhigh');
		expect(stepValue(EFFORT, 'high', -STEP_DISTANCE)).toBe('medium');
		// A long swipe is still one level.
		expect(stepValue(EFFORT, 'high', 400)).toBe('xhigh');
	});

	it('ignores a short wobble and stays inside the list', () => {
		expect(stepValue(EFFORT, 'high', STEP_DISTANCE - 1)).toBe('high');
		expect(stepValue(EFFORT, 'max', 200)).toBe('max');
		expect(stepValue(EFFORT, 'low', -200)).toBe('low');
	});

	it('never lands on Bypass', () => {
		expect(stepValue(ACCESS, 'acceptEdits', 200, BYPASS)).toBe('acceptEdits');
		expect(stepValue(['default', 'acceptEdits', BYPASS], 'acceptEdits', 200, BYPASS)).toBe('acceptEdits');
		expect(stepValue(ACCESS, 'auto', 200, BYPASS)).toBe('acceptEdits');
	});

	it('steps down out of Bypass when the chat is already there', () => {
		expect(stepValue(ACCESS, BYPASS, -200, BYPASS)).toBe('acceptEdits');
		expect(stepValue(ACCESS, BYPASS, 200, BYPASS)).toBe(BYPASS);
	});
});

describe('panel drag', () => {
	it('waits for a real movement before picking an axis', () => {
		expect(dragAxis(3, -4)).toBeNull();
		expect(dragAxis(2, -30)).toBe('vertical');
		expect(dragAxis(40, 6)).toBe('horizontal');
	});

	it('opens past a third or on an upward flick, and closes the same way down', () => {
		expect(settlesOpen(0.35, 0)).toBe(true);
		expect(settlesOpen(0.2, 0)).toBe(false);
		expect(settlesOpen(0.1, -0.6)).toBe(true);
		expect(settlesOpen(0.9, 0.6)).toBe(false);
		expect(settlesClosed(300, 800, 0)).toBe(true);
		expect(settlesClosed(100, 800, 0)).toBe(false);
		expect(settlesClosed(40, 800, 0.8)).toBe(true);
		expect(settlesClosed(500, 800, -0.8)).toBe(false);
	});

	it('reads a release velocity from recent samples', () => {
		const t = new VelocityTracker();
		t.add(500, 0);
		t.add(400, 50);
		t.add(300, 100);
		expect(t.velocity()).toBeCloseTo(-2);
	});
});

describe('slide to answer', () => {
	it('answers only near the ends', () => {
		expect(slideAnswer(knobFraction(140, 150))).toBe('approve');
		expect(slideAnswer(knobFraction(-140, 150))).toBe('deny');
		expect(slideAnswer(knobFraction(100, 150))).toBeNull();
		expect(knobFraction(400, 150)).toBe(1);
		expect(knobFraction(10, 0)).toBe(0);
	});
});

it('summarises running tasks by kind', () => {
	expect(taskSummary([{ kind: 'agent' }, { kind: 'shell' }])).toBe('1 agent · 1 shell');
	expect(taskSummary([{ kind: 'agent' }, { kind: 'agent' }])).toBe('2 agents');
});
