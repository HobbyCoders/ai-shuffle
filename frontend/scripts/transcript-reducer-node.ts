/** Private, server-owned reducer process. No HTTP capability or read route. */
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import {
	foldProjection, materializeProjection, projectionReferences, ProjectionUnsupported,
	type ProjectionRow, type ProjectionFold
} from '../src/lib/stores/session/v2/transcript-projection';
import { ProjectionCodecError, packProjection } from '../src/lib/stores/session/v2/transcript-codec';
import { reduceAll, withLogEpoch, parseWireEvent, type WireEvent } from '../src/lib/stores/session/v2/reducer';

declare const __TRANSCRIPT_REDUCER_FINGERPRINT__: string;
export const REDUCER_FINGERPRINT = __TRANSCRIPT_REDUCER_FINGERPRINT__;
export const RPC_VERSION = 1;
const MAX_PACKET_BYTES = 24 * 1024 * 1024;

// Exports also let golden tests drive the exact shipped bundle and full fold.
export { foldProjection, materializeProjection, projectionReferences,
	packProjection, reduceAll, withLogEpoch, parseWireEvent };

type Params = {
	carry: string | null; frames?: WireEvent[]; records?: ProjectionRow[];
	epoch?: string; runtime?: { sessionId: string; apiRoot?: string };
};

function dispatch(method: string, params: Params): string[] | ProjectionFold | unknown {
	if (!params || (params.carry !== null && typeof params.carry !== 'string')) {
		throw new ProjectionUnsupported('invalid_request');
	}
	if (method === 'plan' || method === 'fold') {
		if (!Array.isArray(params.frames)) throw new ProjectionUnsupported('invalid_request');
		if (method === 'plan') return projectionReferences(params.carry, params.frames);
		if (!Array.isArray(params.records)) throw new ProjectionUnsupported('invalid_request');
		return foldProjection(params.carry, params.frames, params.records);
	}
	// Private golden-test adapter: run the unpaged full serving fold, with no
	// projection eligibility, retention, origin or field normalization.
	if (method === 'reference') {
		if (!Array.isArray(params.frames) || typeof params.epoch !== 'string') throw new ProjectionUnsupported('invalid_request');
		const events = params.frames.map(value => {
			const event = parseWireEvent(value);
			if (!event) throw new ProjectionUnsupported('invalid_canonical_input');
			return event;
		});
		return packProjection(withLogEpoch(reduceAll(events), params.epoch));
	}
	if (method === 'materialize') {
		if (typeof params.carry !== 'string' || !Array.isArray(params.records) || typeof params.epoch !== 'string') {
			throw new ProjectionUnsupported('invalid_request');
		}
		return packProjection(materializeProjection(params.carry, params.records, params.epoch, params.runtime));
	}
	throw new ProjectionUnsupported('unknown_method');
}

async function write(value: unknown): Promise<void> {
	const line = JSON.stringify(value) + '\n';
	if (Buffer.byteLength(line, 'utf8') > MAX_PACKET_BYTES) throw new ProjectionUnsupported('rpc_output_limit');
	if (!process.stdout.write(line)) await once(process.stdout, 'drain');
}

async function run(): Promise<void> {
	// Keep payloads and reducer diagnostics off both logs and the JSON channel.
	console.log = console.info = console.warn = console.error = console.debug = () => {};
	process.on('unhandledRejection', () => process.exit(70));
	process.on('uncaughtException', () => process.exit(70));
	const major = Number(process.versions.node.split('.')[0]);
	if (major < 22) { process.exitCode = 78; return; }
	await write({ type: 'ready', version: RPC_VERSION, fingerprint: REDUCER_FINGERPRINT, node: process.versions.node });
	const input = createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
	for await (const line of input) {
		let id: number | null = null;
		try {
			if (Buffer.byteLength(line, 'utf8') > MAX_PACKET_BYTES) throw new ProjectionUnsupported('rpc_input_limit');
			const request = JSON.parse(line);
			if (!Number.isSafeInteger(request.id) || request.id < 1 || typeof request.method !== 'string') {
				throw new ProjectionUnsupported('invalid_request');
			}
			id = request.id;
			await write({ id, ok: true, result: dispatch(request.method, request.params) });
		} catch (error) {
			const code = error instanceof ProjectionUnsupported || error instanceof ProjectionCodecError
				? error.code : 'fold_failed';
			await write({ id, ok: false, error: code });
		}
	}
}

if (require.main === module) void run().catch(() => { process.exitCode = 70; });
