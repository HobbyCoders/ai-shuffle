/**
 * Payload-shape tests for executeRewind — pins the uuid-vs-content contract.
 *
 * Live v2 `message.user` events carry no JSONL uuid, so uuid-less USER prompts
 * rewind via a content-anchored fallback (backend resolves the checkpoint whose
 * text matches, occurrence disambiguates duplicates, and FAILS CLOSED when
 * nothing matches). The uuid path must stay byte-identical for existing
 * callers, and the content fields must only ever be sent when the uuid is
 * absent.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const apiPostMock = vi.fn(async (_path: string, _body: unknown) => ({ success: true }));
vi.mock('$lib/api/client', () => ({
	api: { post: (path: string, body: unknown) => apiPostMock(path, body) },
}));

import { executeRewind } from './rewind';

beforeEach(() => {
	apiPostMock.mockClear();
});

describe('executeRewind payload shape', () => {
	it('uuid present → uuid payload, no content fields (existing callers unchanged)', async () => {
		await executeRewind('sess-1', 'uuid-abc', true, 'assistant', false);

		expect(apiPostMock).toHaveBeenCalledTimes(1);
		const [path, body] = apiPostMock.mock.calls[0] as unknown as [string, Record<string, unknown>];
		expect(path).toBe('/commands/rewind/execute/sess-1');
		expect(body).toEqual({
			target_uuid: 'uuid-abc',
			restore_chat: true,
			restore_files: false,
			include_response: true,
			target_role: 'assistant',
		});
		expect(body).not.toHaveProperty('target_occurrence');
		expect(body).not.toHaveProperty('target_content');
	});

	it('no uuid + occurrence → occurrence+content payload with target_uuid ""', async () => {
		await executeRewind('sess-1', '', false, 'user', false, 2, 'the repeated prompt');

		const [path, body] = apiPostMock.mock.calls[0] as unknown as [string, Record<string, unknown>];
		expect(path).toBe('/commands/rewind/execute/sess-1');
		expect(body).toEqual({
			target_uuid: '',
			restore_chat: true,
			restore_files: false,
			include_response: false,
			target_role: 'user',
			target_occurrence: 2,
			target_content: 'the repeated prompt',
		});
	});

	it('occurrence 0 is a valid target (falsy-number guard)', async () => {
		await executeRewind('sess-1', '', false, 'user', false, 0, 'first prompt');

		const [, body] = apiPostMock.mock.calls[0] as unknown as [string, Record<string, unknown>];
		expect(body.target_occurrence).toBe(0);
		expect(body.target_content).toBe('first prompt');
	});

	it('uuid present wins over a supplied occurrence — content fields are NOT sent', async () => {
		await executeRewind('sess-1', 'uuid-abc', false, 'user', false, 4, 'some prompt');

		const [, body] = apiPostMock.mock.calls[0] as unknown as [string, Record<string, unknown>];
		expect(body.target_uuid).toBe('uuid-abc');
		expect(body).not.toHaveProperty('target_occurrence');
		expect(body).not.toHaveProperty('target_content');
	});

	it('no uuid and no occurrence → legacy payload with empty target_uuid, no content fields', async () => {
		// The component refuses before calling in this state (assistant messages
		// without a uuid keep the graceful refusal); if it IS called, the API must
		// not fabricate a content anchor.
		await executeRewind('sess-1', '', true, 'assistant', false);

		const [, body] = apiPostMock.mock.calls[0] as unknown as [string, Record<string, unknown>];
		expect(body.target_uuid).toBe('');
		expect(body).not.toHaveProperty('target_occurrence');
		expect(body).not.toHaveProperty('target_content');
	});
});
