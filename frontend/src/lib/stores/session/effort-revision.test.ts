import { describe, expect, it } from 'vitest';
import { effortFromRow, effortIsNewer, effortRevOf, effortRevToSend, restoredEffort } from './effort-revision';

describe('effortRevOf', () => {
	it('reads a count and nothing else', () => {
		expect(effortRevOf(0)).toBe(0);
		expect(effortRevOf(7)).toBe(7);
		for (const bad of [undefined, null, '3', -1, 1.5, true, NaN]) expect(effortRevOf(bad)).toBeUndefined();
	});
});

describe('effortIsNewer', () => {
	it('adopts only a revision above the one held', () => {
		expect(effortIsNewer(3, 2)).toBe(true);
		expect(effortIsNewer(2, 2)).toBe(false);
		expect(effortIsNewer(1, 2)).toBe(false);
	});

	it('adopts a level with no revision (a server too old to count), and any level when none is held', () => {
		expect(effortIsNewer(undefined, 5)).toBe(true);
		expect(effortIsNewer(0, undefined)).toBe(true);
		expect(effortIsNewer(undefined, undefined)).toBe(true);
	});
});

describe('restoredEffort', () => {
	it('keeps the cached level and its revision', () => {
		expect(restoredEffort({ effortLevelOverride: 'high', effortRev: 2 }, { effort: 'high', effort_rev: 2 }))
			.toEqual({ effortLevelOverride: 'high', effortRev: 2 });
	});

	it('takes the row when it was changed while the chat was not open here', () => {
		expect(restoredEffort({ effortLevelOverride: 'high', effortRev: 2 }, { effort: 'low', effort_rev: 3 }))
			.toEqual({ effortLevelOverride: 'low', effortRev: 3 });
		// Auto included.
		expect(restoredEffort({ effortLevelOverride: 'high', effortRev: 2 }, { effort: null, effort_rev: 3 }))
			.toEqual({ effortLevelOverride: null, effortRev: 3 });
	});

	it('never pairs a cached level with the row revision', () => {
		// The cached copy knows no revision: the level is sent with none.
		expect(restoredEffort({ effortLevelOverride: 'high' }, { effort: 'low', effort_rev: 3 }))
			.toEqual({ effortLevelOverride: 'high', effortRev: undefined });
	});

	it('falls back to the row for a cached Auto, and for a chat with no cached copy', () => {
		expect(restoredEffort({ effortLevelOverride: null, effortRev: 1 }, { effort: 'low', effort_rev: 3 }))
			.toEqual({ effortLevelOverride: 'low', effortRev: 3 });
		expect(restoredEffort(null, { effort: 'max', effort_rev: 0 })).toEqual({ effortLevelOverride: 'max', effortRev: 0 });
		expect(restoredEffort(undefined, undefined)).toEqual({ effortLevelOverride: null, effortRev: undefined });
		// A row from a server that does not count.
		expect(restoredEffort(null, { effort: 'max' })).toEqual({ effortLevelOverride: 'max', effortRev: undefined });
	});
});

describe('effortRevToSend', () => {
	it('sends the revision held', () => {
		expect(effortRevToSend({ effortLevelOverride: 'high', effortRev: 4 }, { effort: 'low', effort_rev: 9 })).toBe(4);
		expect(effortRevToSend({ effortLevelOverride: null, effortRev: 0 }, undefined)).toBe(0);
	});

	it('a chat created here takes the row revision while the row names its level', () => {
		expect(effortRevToSend({ effortLevelOverride: 'high' }, { effort: 'high', effort_rev: 0 })).toBe(0);
		expect(effortRevToSend({ effortLevelOverride: null }, { effort: null, effort_rev: 2 })).toBe(2);
		expect(effortRevToSend({ effortLevelOverride: null }, { effort_rev: 2 })).toBe(2);
	});

	it('sends none when the row names another level, has no revision or is not loaded', () => {
		expect(effortRevToSend({ effortLevelOverride: 'high' }, { effort: 'low', effort_rev: 1 })).toBeUndefined();
		expect(effortRevToSend({ effortLevelOverride: 'high' }, { effort: 'high' })).toBeUndefined();
		expect(effortRevToSend({ effortLevelOverride: 'high' }, undefined)).toBeUndefined();
	});
});

describe('effortFromRow', () => {
	it('takes the level of a row whose revision is above the one held', () => {
		expect(effortFromRow({ effortLevelOverride: 'high', effortRev: 2 }, { effort: 'low', effort_rev: 3 }))
			.toEqual({ effortLevelOverride: 'low', effortRev: 3 });
		expect(effortFromRow({ effortLevelOverride: 'high', effortRev: 2 }, { effort: null, effort_rev: 3 }))
			.toEqual({ effortLevelOverride: null, effortRev: 3 });
	});

	it('ignores a reloaded row that is as old as, or older than, what is held', () => {
		expect(effortFromRow({ effortLevelOverride: 'high', effortRev: 3 }, { effort: 'low', effort_rev: 3 })).toBeNull();
		expect(effortFromRow({ effortLevelOverride: 'high', effortRev: 3 }, { effort: 'low', effort_rev: 2 })).toBeNull();
	});

	it('a chat created here opened at revision 0: its own row is no news, a later change is', () => {
		expect(effortFromRow({ effortLevelOverride: 'medium' }, { effort: 'high', effort_rev: 0 })).toBeNull();
		expect(effortFromRow({ effortLevelOverride: 'medium' }, { effort: 'high', effort_rev: 1 }))
			.toEqual({ effortLevelOverride: 'high', effortRev: 1 });
	});

	it('ignores a row with no revision, and a row that is not loaded', () => {
		expect(effortFromRow({ effortLevelOverride: 'high', effortRev: 1 }, { effort: 'low' })).toBeNull();
		expect(effortFromRow({ effortLevelOverride: 'high' }, undefined)).toBeNull();
	});
});
