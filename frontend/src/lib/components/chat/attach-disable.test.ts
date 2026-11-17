import { describe, expect, it } from 'vitest';
import {
	ATTACHMENTS_NEED_LOCAL_CHECKOUT,
	SELECT_PROJECT_TO_UPLOAD,
	attachmentBlockReason,
	composerUploadTarget,
	isRemoteOnlyCompose
} from './attach-disable';

describe('attachmentBlockReason', () => {
	it('allows an existing canonical worker session without a local checkout', () => {
		expect(attachmentBlockReason(null, 'pv2-remote', 'code', 'session-1')).toBeNull();
		expect(attachmentBlockReason('', 'pv2-remote', 'voice', 'session-1')).toBeNull();
		expect(attachmentBlockReason(null, null, 'code', 'session-1')).toBe(SELECT_PROJECT_TO_UPLOAD);
	});
	it('allows attach when a local project is selected', () => {
		expect(attachmentBlockReason('proj-1', 'pv2-1')).toBeNull();
		expect(attachmentBlockReason('proj-1', null)).toBeNull();
	});

	it('disables attach/drop for remote-only compose with the API 400 sentence', () => {
		expect(attachmentBlockReason('', 'pv2-remote')).toBe(ATTACHMENTS_NEED_LOCAL_CHECKOUT);
		expect(attachmentBlockReason(null, 'pv2-remote')).toBe(ATTACHMENTS_NEED_LOCAL_CHECKOUT);
		expect(isRemoteOnlyCompose('', 'pv2-remote')).toBe(true);
	});

	it('keeps the select-a-project sentence when nothing is picked', () => {
		expect(attachmentBlockReason('', null)).toBe(SELECT_PROJECT_TO_UPLOAD);
		expect(isRemoteOnlyCompose('', null)).toBe(false);
	});

	it('defaults to the code lane so pre-lane callers keep their behaviour', () => {
		expect(attachmentBlockReason('', null)).toBe(attachmentBlockReason('', null, 'code'));
	});

	it('never blocks a chat-lane upload, which is projectless by construction', () => {
		// Chat uploads go to the workspace's shared chat-uploads dir, not a
		// project — "select a project first" would be advice with nothing behind it.
		expect(attachmentBlockReason(null, null, 'chat')).toBeNull();
		expect(attachmentBlockReason('', '', 'chat')).toBeNull();
	});

	it('still refuses a code-lane remote-only compose once lanes are passed', () => {
		// An uncreated remote draft still has no session-scoped upload destination;
		// passing its lane must not turn that into a blanket bypass.
		expect(attachmentBlockReason(null, 'pv2-remote', 'code')).toBe(ATTACHMENTS_NEED_LOCAL_CHECKOUT);
		expect(attachmentBlockReason('', 'pv2-remote', 'voice')).toBe(ATTACHMENTS_NEED_LOCAL_CHECKOUT);
	});
});

describe('composerUploadTarget: where an open chat\'s upload goes (#1659)', () => {
	const owners = { unknown: undefined, local: false, remote: true } as const;
	const holdings = {
		'a local project': { project: 'app', projectV2Id: null },
		'the shared id only': { project: '', projectV2Id: 'pv2' },
		'both': { project: 'app', projectV2Id: 'pv2' },
		'none': { project: '', projectV2Id: null }
	} as const;
	const session = { kind: 'session', sessionId: 's' };
	const project = { kind: 'project', project: 'app' };
	const none = { kind: 'blocked', reason: SELECT_PROJECT_TO_UPLOAD };
	// Owner unknown: a local id waits (it may name an unrelated project here).
	// Owner remote: the session upload whatever the store holds, except a
	// legacy chat with no shared id, which keeps its local id (accepted rule).
	// Owner local: the local project. A shared id alone: the session upload.
	const expected: Record<keyof typeof owners, Record<keyof typeof holdings, unknown>> = {
		unknown: { 'a local project': { kind: 'wait' }, 'the shared id only': session, both: { kind: 'wait' }, none },
		local: { 'a local project': project, 'the shared id only': session, both: project, none },
		remote: { 'a local project': project, 'the shared id only': session, both: session, none }
	};
	for (const [owner, isRemote] of Object.entries(owners)) {
		for (const [holding, ids] of Object.entries(holdings)) {
			it(`owner ${owner}, holding ${holding}`, () => {
				expect(composerUploadTarget({ sessionId: 's', ...ids, isRemote }))
					.toEqual(expected[owner as keyof typeof owners][holding as keyof typeof holdings]);
			});
		}
	}

	it('a new chat uploads to the project picked for it, and the chat lane to its own folder', () => {
		expect(composerUploadTarget({ sessionId: null, project: 'app', projectV2Id: 'pv2' })).toEqual(project);
		expect(composerUploadTarget({ sessionId: null, project: '', projectV2Id: 'pv2' }))
			.toEqual({ kind: 'blocked', reason: ATTACHMENTS_NEED_LOCAL_CHECKOUT });
		expect(composerUploadTarget({ sessionId: null, project: '', projectV2Id: null })).toEqual(none);
		for (const isRemote of Object.values(owners)) {
			expect(composerUploadTarget({ sessionId: 's', project: 'app', projectV2Id: 'pv2', isRemote }, 'chat')).toEqual({ kind: 'chat' });
		}
	});
});
