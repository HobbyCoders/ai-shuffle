import { describe, expect, it } from 'vitest';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { initialState, parseWireEvent, reduce, reduceAll, sealState, withLogEpoch, type WireEvent } from './reducer';
import { foldProjection, materializeProjection } from './transcript-projection';

function plan(revision = 1, generation = 1) {
	return { version: 1, generation, revision, goal: 'Ship the scoped change', done_when: 'Reviewed and exercised',
		tasks: [{ id: 'verify', title: 'Exercise the real flow', status: 'in_progress', depends_on: [], evidence: [] as string[] }] };
}
function frame(seq = 1, payload: Record<string, unknown> = {}): WireEvent {
	return { id: `plan-event-${seq}`, seq, type: 'plan.updated', session_id: 'session',
		created_at: '2026-11-05T19:00:00Z', turn_id: null, command_id: null,
		plan: plan(seq), status_source: 'model_reported', ...payload };
}

describe('session Plan transcript projection', () => {
	it('projects the flat backend wire event into an ordinary stable system row without a turn id', () => {
		const event = frame();
		expect(parseWireEvent(event)).toEqual(event);
		const state = reduce(initialState(), event);
		expect(state.messages).toHaveLength(1);
		expect(state.messages[0]).toMatchObject({ id: event.id, role: 'system', type: 'system',
			systemSubtype: 'session_plan', seq: 1, systemData: { plan: event.plan, status_source: 'model_reported' } });
		expect(state.messages[0].turnId).toBeUndefined();
		expect(state.messageOriginSeq.get(event.id)).toBe(1);
	});

	it('keeps each revision and rollover as independent historical snapshots', () => {
		const first = sealState(reduce(initialState(), frame()));
		const second = frame(2, { plan: plan(2), turn_id: 'turn-2' });
		const state = reduceAll([second, frame(3, { plan: plan(3, 2) })], first);
		expect(first.messages).toHaveLength(1);
		expect(state.messages.map(row => row.systemData?.plan)).toEqual([plan(1), plan(2), plan(3, 2)]);
		expect(state.messages[1].turnId).toBe('turn-2');
		expect(reduceAll([frame(), second, frame(3, { plan: plan(3, 2) })]).messages).toEqual(state.messages);
	});

	it('disowns payload objects and does not replace an existing event on a replay', () => {
		const document = plan();
		let state = reduce(initialState(), frame(1, { plan: document }));
		document.tasks[0].status = 'completed';
		document.tasks[0].evidence.push('Later evidence');
		expect(state.messages[0].systemData?.plan).toEqual(plan());
		state = reduce(state, frame(1, { plan: plan(7) }));
		expect(state.messages).toHaveLength(1);
		expect(state.messages[0].systemData?.plan).toEqual(plan());
	});

	it.each([
		{ plan: null }, { plan: [] }, { plan: { ...plan(), version: 9 } },
		{ plan: { ...plan(), generation: 0 } }, { plan: { ...plan(), revision: 1.5 } },
		{ plan: { ...plan(), done_when: [] } }, { plan: { ...plan(), tasks: 'invalid' } },
		{ status_source: 'verified' }
	])('ignores malformed or unsupported Plan frames: %j', payload => {
		expect(reduce(initialState(), frame(1, payload)).messages).toEqual([]);
	});

	it('closes preceding prose without changing the running turn or execution summaries', () => {
		const events: WireEvent[] = [
			{ ...frame(1), type: 'turn.started', turn_id: 'live' },
			{ ...frame(2), type: 'assistant.text_delta', text: 'Before', turn_id: 'live' },
			frame(3, { turn_id: 'live' }),
			{ ...frame(4), type: 'assistant.text_delta', text: 'After', turn_id: 'live' }
		];
		const state = reduceAll(events);
		expect(state.messages.map(row => row.content)).toEqual(['Before', '', 'After']);
		expect(state.liveTurnId).toBe('live');
		expect(state.activeTaskSummaries).toEqual([]);
	});

	it('hydrates the same snapshots through the canonical detached cold-projection path', () => {
		const events = [frame(1), frame(2, { plan: plan(2) }),
			{ ...frame(3), type: 'turn.completed', turn_id: 'turn' }];
		const folded = foldProjection(null, events, []);
		const state = materializeProjection(folded.preview.carry, folded.boundary?.upserts ?? [], 'epoch');
		expect(state).toStrictEqual(withLogEpoch(reduceAll(events), 'epoch'));
		expect(state.messages).toHaveLength(2);
		expect(state.messages.map(row => row.systemData?.plan)).toEqual([plan(1), plan(2)]);
	});

	it('executes these same snapshots in the credential-free shipped server reducer', () => {
		const events = [frame(1), frame(2)];
		const env = Object.fromEntries(['SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TMP', 'TEMP']
			.filter(key => process.env[key] !== undefined).map(key => [key, process.env[key]!]));
		const child = spawnSync(process.execPath, [path.resolve('../app/core/transcript_views/reducer.cjs')], {
			env, encoding: 'utf8', timeout: 10_000,
			input: JSON.stringify({ id: 1, method: 'fold', params: { carry: null, frames: events, records: [] } }) + '\n'
		});
		expect(child.error).toBeUndefined();
		expect(child.status, child.stderr).toBe(0);
		const result = child.stdout.trim().split('\n').map(line => JSON.parse(line))[1];
		expect(result).toMatchObject({ id: 1, ok: true });
		const state = materializeProjection(result.result.preview.carry, [], 'epoch');
		expect(state).toStrictEqual(withLogEpoch(reduceAll(events), 'epoch'));
		expect(state.messages.map(row => row.systemData?.plan)).toEqual([plan(1), plan(2)]);
	});
});
