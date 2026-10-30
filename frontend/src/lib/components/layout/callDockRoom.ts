/**
 * #816: the room the transcript keeps for a phone's floating call dock.
 *
 * While a call is up on a phone the transcript's tail spacer reserves the
 * dock's height. After the call ends the room is held (the call's closing row
 * lands in it): dropping it with the dock would clamp a bottomed-out
 * transcript down by the dock's height. It is released when the chat changes,
 * the screen is no longer phone-width, or a NEW reply starts after the call
 * (a reply already streaming at hang-up does not count).
 */
export interface CallRoom {
	/** Reserved height in px; 0 = none. */
	room: number;
	/** The chat the room belongs to. */
	session: string | null;
	/** The last streaming state seen while the room was held. */
	streaming: boolean;
}

export interface CallRoomInput {
	callActive: boolean;
	dockHeight: number;
	session: string | null;
	streaming: boolean;
	phone: boolean;
}

export const NO_CALL_ROOM: CallRoom = { room: 0, session: null, streaming: false };

export function nextCallRoom(prev: CallRoom, input: CallRoomInput): CallRoom {
	if (!input.phone) return prev.room ? NO_CALL_ROOM : prev;
	if (input.callActive) {
		return {
			room: input.dockHeight > 0 ? input.dockHeight : prev.room,
			session: input.session,
			streaming: input.streaming
		};
	}
	if (!prev.room) return prev;
	if (input.session !== prev.session || (input.streaming && !prev.streaming)) return NO_CALL_ROOM;
	return prev.streaming === input.streaming ? prev : { ...prev, streaming: input.streaming };
}
