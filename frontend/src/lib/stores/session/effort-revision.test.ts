import { describe, expect, it } from 'vitest';
import { effortFromRow, effortIsNewer, effortRevOf, effortRevToSend, pinIfNewer, restoredEffort } from './effort-revision';

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

	it('a cached level with no revision loses to a row that names one (S4)', () => {
		// A cache from before revisions holds High; the loaded row says Low at
		// revision 3. Sending before /live answers used to save High.
		const restored = restoredEffort({ effortLevelOverride: 'high' }, { effort: 'low', effort_rev: 3 });
		expect(restored).toEqual({ effortLevelOverride: 'low', effortRev: 3 });
		expect(effortRevToSend(restored, { effort: 'low', effort_rev: 3 })).toBe(3);
		// Revision 0 is a revision too: the row's never-changed level stands.
		expect(restoredEffort({ effortLevelOverride: 'high' }, { effort: 'low', effort_rev: 0 }))
			.toEqual({ effortLevelOverride: 'low', effortRev: 0 });
		// Neither side counts (a server too old): the cached level, as before.
		expect(restoredEffort({ effortLevelOverride: 'high' }, { effort: 'low' }))
			.toEqual({ effortLevelOverride: 'high', effortRev: undefined });
		expect(restoredEffort({ effortLevelOverride: 'high' }, undefined))
			.toEqual({ effortLevelOverride: 'high', effortRev: undefined });
	});

	it('a cached Auto with a revision is a choice, ordered like any level (S5)', () => {
		// Switching back to a chat whose mirror row is older than the Auto this
		// page had acknowledged used to restore High.
		expect(restoredEffort({ effortLevelOverride: null, effortRev: 4 }, { effort: 'high', effort_rev: 3 }))
			.toEqual({ effortLevelOverride: null, effortRev: 4 });
		expect(restoredEffort({ effortLevelOverride: null, effortRev: 4 }, { effort: 'high', effort_rev: 4 }))
			.toEqual({ effortLevelOverride: null, effortRev: 4 });
		expect(restoredEffort({ effortLevelOverride: null, effortRev: 4 }, undefined))
			.toEqual({ effortLevelOverride: null, effortRev: 4 });
	});

	it('falls back to the row for an older or unversioned cached Auto, and for a chat with no cached copy', () => {
		expect(restoredEffort({ effortLevelOverride: null, effortRev: 1 }, { effort: 'low', effort_rev: 3 }))
			.toEqual({ effortLevelOverride: 'low', effortRev: 3 });
		expect(restoredEffort({ effortLevelOverride: null }, { effort: 'low' }))
			.toEqual({ effortLevelOverride: 'low', effortRev: undefined });
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

describe('pinIfNewer (#1640)', () => {
	it('applies a pin only when its revision is above the one held', () => {
		expect(pinIfNewer({ contextWindowPin: 1_000_000, contextWindowRev: 2 }, 200_000, 3))
			.toEqual({ contextWindowPin: 200_000, contextWindowRev: 3 });
		expect(pinIfNewer({ contextWindowPin: 1_000_000, contextWindowRev: 2 }, 200_000, 2)).toBeNull();
		expect(pinIfNewer({ contextWindowPin: 1_000_000, contextWindowRev: 2 }, 200_000, 1)).toBeNull();
	});

	it('a dropped pin is a pin like any other: newer applies, older does not', () => {
		expect(pinIfNewer({ contextWindowPin: 1_000_000, contextWindowRev: 2 }, null, 3))
			.toEqual({ contextWindowPin: null, contextWindowRev: 3 });
		// The clear of an earlier model arriving after a newer pick was saved.
		expect(pinIfNewer({ contextWindowPin: 1_000_000, contextWindowRev: 4 }, null, 3)).toBeNull();
	});

	it('a holder that was told no revision takes the first one it gets', () => {
		expect(pinIfNewer({ contextWindowPin: 1_000_000 }, null, 0)).toEqual({ contextWindowPin: null, contextWindowRev: 0 });
		expect(pinIfNewer({}, 200_000, 5)).toEqual({ contextWindowPin: 200_000, contextWindowRev: 5 });
	});

	it('without a revision (an older worker) a saved window is taken as before, and "no pin" is not', () => {
		expect(pinIfNewer({ contextWindowPin: 1_000_000, contextWindowRev: 9 }, 200_000, undefined))
			.toEqual({ contextWindowPin: 200_000 });
		expect(pinIfNewer({ contextWindowPin: 1_000_000 }, null, undefined)).toBeNull();
		expect(pinIfNewer({ contextWindowPin: 1_000_000 }, undefined, undefined)).toBeNull();
	});
});
