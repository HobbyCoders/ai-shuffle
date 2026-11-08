/**
 * The live-turn status line says "Compacting context…" while the
 * conversation is compacted, shows a failure's reason briefly, and otherwise
 * stays the plain "Working" indicator.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import TurnActivity, { COMPACTION_FAILURE_VISIBLE_MS, THINKING_FRESH_MS } from './TurnActivity.svelte';
import type { LiveCompaction } from '$lib/stores/session/v2/reducer';

afterEach(cleanup);

function compaction(over: Partial<LiveCompaction> = {}): LiveCompaction {
	return {
		phase: 'running',
		trigger: 'auto',
		requestedBy: null,
		error: null,
		at: new Date().toISOString(),
		...over,
	};
}

describe('TurnActivity', () => {
	it('shows Working without a compaction', () => {
		render(TurnActivity, { props: { startedAt: null } });
		expect(screen.getByRole('status').textContent).toContain('Working…');
	});

	it('says a held turn is queued, with no working timer (#583)', () => {
		render(TurnActivity, { props: { startedAt: new Date(Date.now() - 8000).toISOString(), queued: true } });
		const status = screen.getByRole('status');
		expect(status.textContent).toContain('Queued (database busy)');
		expect(status.textContent).not.toContain('Working');
		expect(status.getAttribute('aria-label')).toBe('Waiting for the database');
	});

	it('shows a live thinking estimate instead of Working (#1271)', () => {
		const startedAt = new Date(Date.now() - 65000).toISOString();
		render(TurnActivity, { props: { startedAt, thinking: { tokens: 12400, at: Date.now() } } });
		const text = screen.getByRole('status').textContent ?? '';
		expect(text).toContain('Thinking for');
		expect(text).toContain('12.4k tokens');
		expect(text).not.toContain('Working');
	});

	it('goes back to Working once the estimate stops arriving', () => {
		render(TurnActivity, { props: { thinking: { tokens: 12400, at: Date.now() - THINKING_FRESH_MS - 1 } } });
		const text = screen.getByRole('status').textContent ?? '';
		expect(text).toContain('Working…');
		expect(text).not.toContain('tokens');
	});

	it('shows the running compaction with its reason', () => {
		render(TurnActivity, { props: { compaction: compaction() } });
		const status = screen.getByRole('status');
		expect(status.textContent).toContain('Compacting context… (auto)');
		expect(status.getAttribute('aria-label')).toBe('Context compaction');
	});

	it('names who asked for a /compact', () => {
		render(TurnActivity, {
			props: { compaction: compaction({ trigger: 'manual', requestedBy: 'app' }) },
		});
		expect(screen.getByRole('status').textContent).toContain(
			'Compacting context… (requested by AI Shuffle)'
		);
	});

	it('shows a failure with its reason', () => {
		render(TurnActivity, {
			props: { compaction: compaction({ phase: 'failed', error: 'Prompt is too long' }) },
		});
		expect(screen.getByRole('status').textContent).toContain(
			'Compaction failed: Prompt is too long'
		);
	});

	it('drops an old failure and renders nothing when not working', () => {
		const at = new Date(Date.now() - COMPACTION_FAILURE_VISIBLE_MS - 1000).toISOString();
		render(TurnActivity, {
			props: { compaction: compaction({ phase: 'failed', error: 'x', at }), working: false },
		});
		expect(screen.queryByRole('status')).toBeNull();
	});

	it('shows a compaction even when the working indicator is off', () => {
		render(TurnActivity, { props: { compaction: compaction(), working: false } });
		expect(screen.getByRole('status').textContent).toContain('Compacting context…');
	});
});
