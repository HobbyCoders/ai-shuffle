import { describe, expect, it } from 'vitest';
import {
	ATTACHMENTS_NEED_LOCAL_CHECKOUT,
	SELECT_PROJECT_TO_UPLOAD,
	attachmentBlockReason,
	isRemoteOnlyCompose
} from './attach-disable';

describe('attachmentBlockReason', () => {
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
		// The lane argument must not become a blanket bypass: a projectV2Id with
		// no local checkout has nowhere to put the file in ANY code-lane compose.
		expect(attachmentBlockReason(null, 'pv2-remote', 'code')).toBe(ATTACHMENTS_NEED_LOCAL_CHECKOUT);
		expect(attachmentBlockReason('', 'pv2-remote', 'voice')).toBe(ATTACHMENTS_NEED_LOCAL_CHECKOUT);
	});
});
