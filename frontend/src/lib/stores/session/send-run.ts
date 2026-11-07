/**
 * This page's current run of sends: the send that starts a turn plus the
 * messages queued behind it, oldest first. A Stop that times out names its
 * turn from the receipt of the send it was pressed on (#1114): the oldest
 * command in the run that is still waiting to run.
 *
 * So the run holds exactly the commands that may still run, and nothing else.
 * The lifecycle of a command, and the only places that change the run:
 *
 * - added:     every send. A send while no turn is live starts a new run with
 *              just itself; a send while a turn is live (or compact-and-send)
 *              joins the end of the current run (`runWithSend`).
 * - confirmed: the feed shows its durable receipt. It has run, so it can no
 *              longer be the next to run; it is pruned when the next command
 *              is added. Until then Stop skips it (`hasReceipt`).
 * - withdrawn: it left the queue without running, whoever removed it (× on
 *              any page or device, clear, Stop, the worker). The worker writes a
 *              durable `queued_message.withdrawn` receipt with its command id,
 *              which every page's feed sees, live or on reconnect: retired. The
 *              page that pressed × also retires on its own verdict (`withdrawn`,
 *              or `not_found` from an older worker); `already_delivered` means
 *              delivery won the race: kept. Leaving the queue snapshot is never
 *              read as a withdrawal, since delivery removes the row too.
 * - refused:   an `error` frame naming its `client_message_id`: retired. Not
 *              one marked `maybe_applied` (the owner got it, its answer was
 *              lost): that send may still run, so it is kept.
 * - failed:    only when the worker certainly did not take it: the frame never
 *              left this page, or compact-and-send was answered with a refusal
 *              (HTTP 4xx, or no queued turn): retired. A send whose fate is
 *              unknown (unconfirmed, transport error, timeout, 5xx, unreadable
 *              reply) is kept until its receipt or a withdrawal settles it; a
 *              retry reuses its command id and keeps its place.
 * - evicted:   never. Only confirmed or retired commands leave the run, so the
 *              command a Stop needs is always there; a new run starts with the
 *              next send made while no turn is live.
 *
 * A Stop is pressed on the run's commands without a receipt, from the oldest on
 * (its pressed-on sends), and names the oldest of them. Pressed with no live turn,
 * it is named only by the receipt of one of its pressed-on sends, while it waits
 * and after it timed out: a turn started elsewhere never claims it. If that
 * command is retired first, the next pressed-on send takes its place (the send
 * that will now run first); a send made after the Stop never does. When every
 * one is retired, nothing the Stop was pressed on will run: a Stop still waiting
 * for its turn ends, and so does a timed-out Stop's recovery.
 *
 * Retiring goes by command id alone: ids are unique, so it is safe whichever
 * chat is open when the verdict arrives.
 */
import type { ActiveSessionState } from '../active-session';
import { STOP_TURN_NOT_NAMED } from '$lib/utils/not-sent-hold';

type RunState = Pick<ActiveSessionState, 'localSendIntent' | 'messages'>;

/**
 * The run in `sessionId`, oldest first. A new chat's first send is made before
 * its session exists, so a run recorded without a session belongs to the
 * session that adopted one of its sends' rows.
 */
export function sendRunIn(s: RunState, sessionId: string | null): string[] {
	const intent = s.localSendIntent;
	const run = intent?.commandIds ?? [];
	if (!run.length) return [];
	if (intent?.sessionId === sessionId) return run;
	return intent?.sessionId === null && sessionId !== null
		&& s.messages.some(m => m.commandId != null && run.includes(m.commandId)) ? run : [];
}

/** The run after adding `commandId`: confirmed commands are pruned, nothing still waiting is dropped. */
export function runWithSend(s: RunState, sessionId: string | null, commandId: string, joinsRun: boolean,
	hasReceipt: (commandId: string) => boolean): string[] {
	if (!joinsRun) return [commandId];
	const run = sendRunIn(s, sessionId).filter(id => !hasReceipt(id));
	// A retried send (same command id) is the same command: it keeps its place.
	return run.includes(commandId) ? run : [...run, commandId];
}

/** The sends of `pressedOn` (the run from the command a Stop was pressed on) still in the run. */
export function stillPressedOn(s: RunState, sessionId: string | null, pressedOn: string[]): string[] {
	const run = sendRunIn(s, sessionId);
	return pressedOn.filter(id => run.includes(id));
}

/**
 * The state with `commandIds` retired from the run (withdrawn, refused or
 * failed). A Stop pressed on a retired command moves to the next send it was
 * pressed on, never to one sent after it; when none is left, an unnamed Stop
 * still waiting ends, and so does a recovery still waiting for a turn, since
 * nothing it was pressed on will run.
 */
export function retireSends<S extends Pick<ActiveSessionState, 'localSendIntent'>
	& Partial<Pick<ActiveSessionState, 'stopTimedOutCommandId' | 'stopTimedOutPressedOn' | 'stopTimedOutTurnId'
		| 'stopRequestedPressedOn' | 'stopRequestedTurnId'>>>(s: S,
	...commandIds: (string | null | undefined)[]): S {
	const gone = commandIds.filter((id): id is string => !!id);
	const run = s.localSendIntent?.commandIds ?? [];
	const pressedOn = s.stopTimedOutPressedOn ?? [];
	const waiting = s.stopRequestedPressedOn ?? [];
	let next = s;
	if (run.some(id => gone.includes(id))) {
		next = { ...next, localSendIntent: { ...s.localSendIntent!, commandIds: run.filter(id => !gone.includes(id)) } };
	}
	if (pressedOn.some(id => gone.includes(id))) {
		const left = pressedOn.filter(id => !gone.includes(id));
		next = { ...next, stopTimedOutPressedOn: left, stopTimedOutCommandId: left[0] ?? null,
			...(!left.length && s.stopTimedOutTurnId === STOP_TURN_NOT_NAMED ? { stopTimedOutTurnId: null } : {}) };
	}
	if (waiting.some(id => gone.includes(id))) {
		const left = waiting.filter(id => !gone.includes(id));
		next = { ...next, stopRequestedPressedOn: left,
			...(!left.length && s.stopRequestedTurnId === STOP_TURN_NOT_NAMED ? { stopRequestedTurnId: null } : {}) };
	}
	return next;
}
