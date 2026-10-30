import { describe, expect, it } from 'vitest';
import { NO_CALL_ROOM, nextCallRoom, type CallRoom, type CallRoomInput } from './callDockRoom';

const base: CallRoomInput = { callActive: true, dockHeight: 199, session: 's1', streaming: false, phone: true };
const run = (steps: Partial<CallRoomInput>[], start: CallRoom = NO_CALL_ROOM) =>
	steps.reduce((state, step) => nextCallRoom(state, { ...base, ...step }), start);

describe('call dock room (#816)', () => {
	it('reserves the dock height during a phone call and holds it after a quiet end', () => {
		expect(run([{}]).room).toBe(199);
		expect(run([{}, { callActive: false }]).room).toBe(199);
	});

	it('keeps the room when the call ends while a reply is already streaming', () => {
		const state = run([{ streaming: true }, { callActive: false, streaming: true }]);
		expect(state.room).toBe(199);
		// That reply finishing does not release it either...
		expect(nextCallRoom(state, { ...base, callActive: false, streaming: false }).room).toBe(199);
	});

	it('releases on a new reply after the call, on a chat switch, and off phone width', () => {
		expect(run([{}, { callActive: false }, { callActive: false, streaming: true }]).room).toBe(0);
		expect(run([{ streaming: true }, { callActive: false, streaming: true }, { callActive: false }, { callActive: false, streaming: true }]).room).toBe(0);
		expect(run([{}, { callActive: false }, { callActive: false, session: 's2' }]).room).toBe(0);
		expect(run([{}, { callActive: false }, { callActive: false, phone: false }]).room).toBe(0);
		expect(run([{ phone: false }]).room).toBe(0);
	});
});
