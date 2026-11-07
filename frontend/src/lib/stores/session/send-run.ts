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
 * - withdrawn: the worker confirms it withdrew the queued message (verdict
 *              `withdrawn`, or `not_found` from an older worker): retired.
 *              `already_delivered` means delivery won the race: kept.
 * - refused:   an `error` frame naming its `client_message_id`: retired.
 * - failed:    the frame never left this page, or compact-and-send failed:
 *              retired. A send that is only unconfirmed is kept, since it may
 *              still be delivered.
 * - evicted:   never. Only confirmed or retired commands leave the run, so the
 *              command a Stop needs is always there; a new run starts with the
 *              next send made while no turn is live.
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
	return [...sendRunIn(s, sessionId).filter(id => !hasReceipt(id)), commandId];
}

/** The state with `commandIds` retired from the run (withdrawn, refused or failed). */
export function retireSends<S extends Pick<ActiveSessionState, 'localSendIntent'>>(s: S,
	...commandIds: (string | null | undefined)[]): S {
	const run = s.localSendIntent?.commandIds;
	const gone = commandIds.filter((id): id is string => !!id);
	if (!run?.some(id => gone.includes(id))) return s;
	return { ...s, localSendIntent: { ...s.localSendIntent!, commandIds: run.filter(id => !gone.includes(id)) } };
}
