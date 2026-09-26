import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import AssistantMessage from './AssistantMessage.svelte';
import { USAGE_ESTIMATED_TITLE } from '$lib/components/UsageEstimatedBadge.svelte';

afterEach(() => cleanup());

describe('AssistantMessage estimated-usage marker', () => {
	it('marks a turn whose token counts the harness estimated, with the reason on hover', () => {
		render(AssistantMessage, {
			content: 'Done.',
			streaming: false,
			metadata: { cost_usd: 0.0042, duration_ms: 1500, tokens_out: 30, usage_estimated: true }
		});
		const badge = screen.getByTestId('usage-estimated');
		expect(badge.textContent).toBe('≈');
		expect(badge.getAttribute('title')).toBe(USAGE_ESTIMATED_TITLE);
		expect(badge.getAttribute('title')).toBe('Token counts estimated: the provider did not report usage');
		expect(screen.getByText('$0.0042')).toBeTruthy();
	});

	it('shows no marker when the counts are the provider\'s own', () => {
		render(AssistantMessage, {
			content: 'Done.',
			streaming: false,
			metadata: { cost_usd: 0.0042, duration_ms: 1500, tokens_out: 30 }
		});
		expect(screen.queryByTestId('usage-estimated')).toBeNull();
		expect(screen.getByText('$0.0042')).toBeTruthy();
	});

	it('shows no marker while the reply is still streaming, even if the flag is set', () => {
		render(AssistantMessage, {
			content: 'Partial',
			streaming: true,
			metadata: { usage_estimated: true }
		});
		expect(screen.queryByTestId('usage-estimated')).toBeNull();
	});
});
