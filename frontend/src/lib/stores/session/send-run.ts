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
 * - refused:   an `error` frame naming its `client_message_id`: retired.
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
 * A Stop names the oldest command of the run without a receipt when pressed. If
 * that command is retired before the Stop is named, the next one in the run (the
 * send that will now run first) takes its place.
 *
 * Retiring goes by command id alone: ids are unique, so it is safe whichever
 * chat is open when the verdict arrives.
 */
import type { ActiveSessionState } from '../active-session';

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

/** The first of `pressedOn` (the run from the command a Stop was pressed on) still in the run. */
export function stillPressedOn(s: RunState, sessionId: string | null, pressedOn: string[]): string | null {
	const run = sendRunIn(s, sessionId);
	return pressedOn.find(id => run.includes(id)) ?? null;
}

/**
 * The state with `commandIds` retired from the run (withdrawn, refused or
 * failed). A timed-out Stop pressed on a retired command moves to the command
 * after it in the run.
 */
export function retireSends<S extends Pick<ActiveSessionState, 'localSendIntent'>
	& Partial<Pick<ActiveSessionState, 'stopTimedOutCommandId'>>>(s: S,
	...commandIds: (string | null | undefined)[]): S {
	const run = s.localSendIntent?.commandIds;
	const gone = commandIds.filter((id): id is string => !!id);
	if (!run?.some(id => gone.includes(id))) return s;
	const kept = run.filter(id => !gone.includes(id));
	const pressed = s.stopTimedOutCommandId;
	const moved = pressed && gone.includes(pressed)
		? { stopTimedOutCommandId: run.slice(run.indexOf(pressed) + 1).find(id => !gone.includes(id)) ?? null }
		: {};
	return { ...s, ...moved, localSendIntent: { ...s.localSendIntent!, commandIds: kept } };
}
