import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createSettleHold, createTurnHandoffHold, PARKED_SETTLE_HOLD_MS, SETTLE_HOLD_MS } from './settle-hold';

describe('createSettleHold', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	const at = (unsettled: boolean, parked = false, sessionId = 's1') => ({ sessionId, unsettled, parked });

	it('turn.completed then turn.parked never shows the turn settled', () => {
		const onExpire = vi.fn();
		const hold = createSettleHold(onExpire);
		expect(hold.update(at(true))).toBe(true);
		// turn.completed: live signals drop, background work speculatively settled.
		expect(hold.update(at(false))).toBe(true);
		vi.advanceTimersByTime(300);
		// turn.parked: the background task is running again.
		expect(hold.update(at(true, true))).toBe(true);
		vi.advanceTimersByTime(SETTLE_HOLD_MS * 2);
		expect(onExpire).not.toHaveBeenCalled();
	});

	it('holds a parked turn through the gap before its wake turn starts', () => {
		const onExpire = vi.fn();
		const hold = createSettleHold(onExpire);
		hold.update(at(true, true));
		// The last background task finished; the turn is still parked.
		expect(hold.update(at(false, true))).toBe(true);
		vi.advanceTimersByTime(PARKED_SETTLE_HOLD_MS - 1_000);
		expect(onExpire).not.toHaveBeenCalled();
		// The wake turn starts: still unsettled, nothing moved.
		expect(hold.update(at(true))).toBe(true);
		vi.advanceTimersByTime(PARKED_SETTLE_HOLD_MS);
		expect(onExpire).not.toHaveBeenCalled();
	});

	it('settles once, after the grace, when the turn really finished', () => {
		const onExpire = vi.fn();
		const hold = createSettleHold(onExpire);
		hold.update(at(true));
		expect(hold.update(at(false))).toBe(true);
		// Re-evaluating with the same input does not restart the grace.
		vi.advanceTimersByTime(SETTLE_HOLD_MS - 100);
		expect(hold.update(at(false))).toBe(true);
		vi.advanceTimersByTime(100);
		expect(onExpire).toHaveBeenCalledTimes(1);
		expect(hold.update(at(false))).toBe(false);
		vi.advanceTimersByTime(PARKED_SETTLE_HOLD_MS);
		expect(onExpire).toHaveBeenCalledTimes(1);
	});

	it('a parked turn with no wake still settles', () => {
		const onExpire = vi.fn();
		const hold = createSettleHold(onExpire);
		hold.update(at(true, true));
		hold.update(at(false, true));
		vi.advanceTimersByTime(PARKED_SETTLE_HOLD_MS);
		expect(onExpire).toHaveBeenCalledTimes(1);
		expect(hold.update(at(false, true))).toBe(false);
	});

	it('shortens a parked hold once the turn is no longer parked', () => {
		const onExpire = vi.fn();
		const hold = createSettleHold(onExpire);
		hold.update(at(true, true));
		hold.update(at(false, true));
		hold.update(at(false, false));
		vi.advanceTimersByTime(SETTLE_HOLD_MS);
		expect(onExpire).toHaveBeenCalledTimes(1);
	});

	it('shows a newly opened session as it is, without a hold', () => {
		const onExpire = vi.fn();
		const hold = createSettleHold(onExpire);
		expect(hold.update(at(false))).toBe(false);
		hold.update(at(true));
		expect(hold.update(at(false, false, 's2'))).toBe(false);
		vi.advanceTimersByTime(PARKED_SETTLE_HOLD_MS);
		expect(onExpire).not.toHaveBeenCalled();
	});

	it('dispose cancels a pending hold', () => {
		const onExpire = vi.fn();
		const hold = createSettleHold(onExpire);
		hold.update(at(true));
		hold.update(at(false));
		hold.dispose();
		vi.advanceTimersByTime(PARKED_SETTLE_HOLD_MS);
		expect(onExpire).not.toHaveBeenCalled();
	});
});

describe('createTurnHandoffHold', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	const at = (latestTurnKey: string | null, unsettled: boolean, parked = false, sessionId = 's1') =>
		({ sessionId, latestTurnKey, unsettled, parked });

	it('keeps the interrupted turn held for the ordinary window when a send opens a newer turn', () => {
		const onExpire = vi.fn();
		const handoff = createTurnHandoffHold(onExpire);
		expect([...handoff.update(at('t1', true))]).toEqual([]);
		// The send lands while t1 is still shown live (its settle hold is running).
		expect([...handoff.update(at('t2', true))]).toEqual(['t1']);
		vi.advanceTimersByTime(SETTLE_HOLD_MS - 1);
		expect(onExpire).not.toHaveBeenCalled();
		vi.advanceTimersByTime(1);
		expect(onExpire).toHaveBeenCalledTimes(1);
		expect([...handoff.update(at('t2', true))]).toEqual([]);
	});

	it('uses the parked window for a turn that was parked when the send arrived', () => {
		const onExpire = vi.fn();
		const handoff = createTurnHandoffHold(onExpire);
		handoff.update(at('t1', true, true));
		expect([...handoff.update(at('t2', true))]).toEqual(['t1']);
		vi.advanceTimersByTime(SETTLE_HOLD_MS);
		expect(onExpire).not.toHaveBeenCalled();
		vi.advanceTimersByTime(PARKED_SETTLE_HOLD_MS - SETTLE_HOLD_MS);
		expect(onExpire).toHaveBeenCalledTimes(1);
	});

	it('does not hold a turn that had already settled, and returns a stable set between changes', () => {
		const handoff = createTurnHandoffHold(vi.fn());
		const first = handoff.update(at('t1', false));
		const next = handoff.update(at('t2', true));
		expect([...next]).toEqual([]);
		expect(handoff.update(at('t2', true))).toBe(next);
		expect(first.size).toBe(0);
	});

	it('drops hand-offs on a session switch', () => {
		const onExpire = vi.fn();
		const handoff = createTurnHandoffHold(onExpire);
		handoff.update(at('t1', true));
		expect([...handoff.update(at('t2', true))]).toEqual(['t1']);
		expect([...handoff.update(at('x1', false, false, 's2'))]).toEqual([]);
		vi.advanceTimersByTime(PARKED_SETTLE_HOLD_MS);
		expect(onExpire).not.toHaveBeenCalled();
	});
});
