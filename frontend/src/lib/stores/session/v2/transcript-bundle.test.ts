import { describe, expect, it } from 'vitest';
import { execFileSync, spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { reduceAll, withLogEpoch, type WireEvent } from './reducer';
import { materializeProjection } from './transcript-projection';

const root = path.resolve(process.cwd(), '..');
const frontend = path.join(root, 'frontend');
const bundle = path.join(root, 'app/core/transcript_views/reducer.cjs');
const manifest = () => JSON.parse(readFileSync(path.join(root, 'app/core/transcript_views/manifest.json'), 'utf8'));
const minimalEnv = Object.fromEntries(['SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TMP', 'TEMP']
	.filter(key => process.env[key] !== undefined).map(key => [key, process.env[key]!]));

function rpc(request: unknown) {
	const child = spawnSync(process.execPath, ['--max-old-space-size=192', bundle], {
		cwd: root, env: minimalEnv, input: JSON.stringify(request) + '\n', encoding: 'utf8', timeout: 10_000
	});
	expect(child.error).toBeUndefined();
	expect(child.status).toBe(0);
	expect(child.stderr).toBe('');
	return { raw: child.stdout, lines: child.stdout.trim().split('\n').map(line => JSON.parse(line)) };
}

describe('shipped transcript reducer', () => {
	it('is regenerated from the entire source closure and canonical server input contract', () => {
		const output = execFileSync(process.execPath, ['scripts/build-transcript-reducer.mjs', '--check'], {
			cwd: frontend, env: minimalEnv, timeout: 15_000, encoding: 'utf8'
		});
		expect(output).toContain(manifest().fingerprint);
		expect(createHash('sha256').update(readFileSync(bundle)).digest('hex')).toBe(manifest().bundle_sha256);
	}, 20_000);

	it('executes the authoritative reducer in a credential-free server process', () => {
		const frames: WireEvent[] = [
			{ id: 'u', seq: 1, type: 'message.user', content: 'Run', created_at: '2026-10-30T00:00:00Z' },
			{ id: 'a', seq: 2, type: 'assistant.text_delta', text: 'Finished', created_at: '2026-10-30T00:00:01Z' },
			{ id: 'done', seq: 3, type: 'turn.completed', created_at: '2026-10-30T00:00:02Z' }
		];
		const { lines } = rpc({ id: 1, method: 'fold', params: { carry: null, frames, records: [] } });
		expect(lines[0]).toMatchObject({ type: 'ready', version: 1, fingerprint: manifest().fingerprint });
		expect(lines[1]).toMatchObject({ id: 1, ok: true });
		const actual = materializeProjection(lines[1].result.preview.carry, [], 'epoch');
		expect(actual).toStrictEqual(withLogEpoch(reduceAll(frames), 'epoch'));
	});

	it.each(['held', 'task-block', 'task-field'])('bounds incomplete %s in the actual shipped reducer with an OS timeout', (kind) => {
		const content = kind === 'held' ? ('<system-reminder>\n' +
			'Messages from other agents that arrived while this session was parked:\n').repeat(80_000)
			: kind === 'task-block' ? '<task-notification>'.repeat(400_000)
			: '<task-notification>' + '<summary>'.repeat(400_000) + '</task-notification>';
		const child = spawnSync(process.execPath, ['--max-old-space-size=192', bundle], {
			cwd: root, env: minimalEnv, encoding: 'utf8', timeout: 5_000, killSignal: 'SIGKILL',
			input: JSON.stringify({ id: 1, method: 'fold', params: { carry: null, records: [], frames: [
				{ id: 'u', seq: 1, type: 'message.user', content }
			] } }) + '\n'
		});
		expect(child.error).toBeUndefined();
		expect(child.status, child.stderr).toBe(0);
		const lines = child.stdout.trim().split('\n').map(line => JSON.parse(line));
		// The oversized request must finish parsing before enforcing the carry
		// limit; it must not hang or silently return partial transcript state.
		expect(lines[1]).toEqual(kind === 'task-field'
			? expect.objectContaining({ id: 1, ok: true })
			: { id: 1, ok: false, error: 'carry_limit' });
		const small = rpc({ id: 2, method: 'fold', params: { carry: null, records: [], frames: [
			{ id: 'u', seq: 1, type: 'message.user', content: content.slice(0, 10_000) }
		] } });
		expect(small.lines[1]).toMatchObject({ id: 2, ok: true });
	}, 10_000);

	it('returns bounded error codes, never partial state, input or diagnostic payloads', () => {
		const { raw, lines } = rpc({ id: 1, method: 'fold', params: { carry: null, records: [], frames: [
			{ id: 'u', seq: 1, type: 'message.user', content: 'fixture-private-payload' },
			{ id: 'bad', seq: 0, type: 'future.event' }
		] } });
		expect(lines[1]).toEqual({ id: 1, ok: false, error: 'non_monotonic_input' });
		expect(raw).not.toContain('fixture-private-payload');
		expect(lines[1]).not.toHaveProperty('result');
	});
});
