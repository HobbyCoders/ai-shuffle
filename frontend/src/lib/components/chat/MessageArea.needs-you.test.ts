/**
 * The rail's Needs-you dot must come from the same derivation and signals as
 * the sidebar row and the tab title (spec §5). Mounting MessageArea pulls in
 * the whole session graph, so this pins the wiring; the derivation itself is
 * covered in session-card-data.test.ts and transcript-navigation.test.ts.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');

describe('MessageArea Needs-you rail wiring', () => {
	it('counts the real session row with its background attention and private access', () => {
		const call = source.slice(source.indexOf('const openNeedsYou'), source.indexOf('const openMessages'));
		expect(call).toContain('needsYouCount(');
		expect(call).toContain('openRow ??');
		expect(call).toContain('$backgroundPermissions.get(openSessionId)');
		expect(call).toContain('$privateAccessPending.get(openSessionId)');
		expect(call).toContain('openNeeds');
		expect(call).not.toMatch(/\{\s*status:\s*'active',\s*is_remote:\s*false/);
	});

	it('places the dots through the pending questions\' tool calls', () => {
		expect(source).toContain('needsYouTurnIds(openNeedsYou, openMessages, pendingQuestionToolIds, newestTurnId)');
		expect(source).toContain('needsYouIds={needsYouTurns}');
	});
});
