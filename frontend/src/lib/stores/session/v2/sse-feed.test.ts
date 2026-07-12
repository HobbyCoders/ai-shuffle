/**
 * Frame-parser tests for the V2 SSE feed client (docs/V2_PHASE1_DESIGN.md §6).
 *
 * `createFrameParser` is the deterministic, side-effect-free core of the feed:
 * it turns an arbitrarily-chunked SSE body into complete `{ id, event, data }`
 * frames. These tests exercise chunk boundaries, keepalives, multi-line data,
 * CRLF transport, and the leading-space rule.
 */

import { describe, it, expect } from 'vitest';
import { createFrameParser, type ParsedFrame } from './sse-feed';

function frames(...chunks: string[]): ParsedFrame[] {
	const p = createFrameParser();
	const out: ParsedFrame[] = [];
	for (const c of chunks) out.push(...p.push(c));
	return out;
}

describe('createFrameParser', () => {
	it('parses a single well-formed frame', () => {
		const [f] = frames('id: 7\nevent: turn.started\ndata: {"a":1}\n\n');
		expect(f).toEqual({ id: 7, event: 'turn.started', data: '{"a":1}' });
	});

	it('strips exactly one leading space after the colon', () => {
		const [f] = frames('id:3\ndata:  two-spaces\n\n');
		// one space stripped, one retained
		expect(f.id).toBe(3);
		expect(f.data).toBe(' two-spaces');
	});

	it('ignores keepalive comment lines (starting with ":")', () => {
		const out = frames(': keepalive\n\n');
		expect(out).toHaveLength(0);
	});

	it('drops a comment-only / id-only frame that carries no data', () => {
		const out = frames('id: 5\n\n');
		expect(out).toHaveLength(0); // no data field → not surfaced
	});

	it('joins multi-line data with newlines', () => {
		const [f] = frames('event: x\ndata: line1\ndata: line2\n\n');
		expect(f.data).toBe('line1\nline2');
	});

	it('retains a partial frame across chunk boundaries', () => {
		const p = createFrameParser();
		expect(p.push('id: 1\ndata: {"hal')).toHaveLength(0); // incomplete
		const done = p.push('f":true}\n\n');
		expect(done).toHaveLength(1);
		expect(done[0]).toEqual({ id: 1, event: '', data: '{"half":true}' });
	});

	it('splits multiple frames delivered in one chunk', () => {
		const out = frames('id: 1\ndata: a\n\nid: 2\ndata: b\n\n');
		expect(out.map((f) => [f.id, f.data])).toEqual([
			[1, 'a'],
			[2, 'b'],
		]);
	});

	it('normalizes CRLF transport to LF', () => {
		const [f] = frames('id: 9\r\nevent: e\r\ndata: hi\r\n\r\n');
		expect(f).toEqual({ id: 9, event: 'e', data: 'hi' });
	});

	it('yields NaN id when the id field is absent or non-numeric', () => {
		const [noId] = frames('data: x\n\n');
		expect(Number.isNaN(noId.id)).toBe(true);
		const [badId] = frames('id: abc\ndata: y\n\n');
		expect(Number.isNaN(badId.id)).toBe(true);
	});

	it('interleaves keepalives between real frames without losing data', () => {
		const out = frames(': ka\n\n', 'id: 4\ndata: real\n\n', ': ka\n\n');
		expect(out).toHaveLength(1);
		expect(out[0]).toEqual({ id: 4, event: '', data: 'real' });
	});
});
