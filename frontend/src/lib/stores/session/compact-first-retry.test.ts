import { beforeEach, describe, expect, it, vi } from 'vitest';
import { compactRetryId, clearCompactRetry } from './compact-first-retry';
describe('compact-first retry expiry and confirmation', () => {
	beforeEach(() => { sessionStorage.clear(); vi.restoreAllMocks(); });
	it.each([[4 * 60_000, true], [5 * 60_000, false], [6 * 60 * 60_000, false]])(
		'reuses only a fresh ambiguous id after %s ms', (elapsed, reused) => {
			const clock = vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
			const first = compactRetryId('s1', 'continue', () => 'client-' + 'a'.repeat(32));
			clock.mockReturnValue(1_000_000 + elapsed);
			const next = compactRetryId('s1', 'continue', () => 'client-' + 'b'.repeat(32));
			expect(next === first).toBe(reused);
		});
	it('clears only a matching authoritative queue/feed command', () => {
		const id = compactRetryId('s1', 'continue', () => 'client-' + 'a'.repeat(32));
		clearCompactRetry('s1', 'other-command');
		expect(compactRetryId('s1', 'continue', () => 'client-' + 'b'.repeat(32))).toBe(id);
		clearCompactRetry('s1', id);
		expect(sessionStorage.getItem('compact-first-retry:s1')).toBeNull();
		expect(compactRetryId('s1', 'continue', () => 'client-' + 'b'.repeat(32))).not.toBe(id);
	});
	it('does not revive old records without a timestamp', () => {
		sessionStorage.setItem('compact-first-retry:s1', JSON.stringify({fingerprint:'continue',id:'client-'+'a'.repeat(32)}));
		expect(compactRetryId('s1', 'continue', () => 'client-'+'b'.repeat(32))).toBe('client-'+'b'.repeat(32));
	});
});
