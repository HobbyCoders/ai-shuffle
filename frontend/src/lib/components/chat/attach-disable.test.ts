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
});
