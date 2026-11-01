import { describe, it, expect } from 'vitest';
import {
	guardrailCategoryLabel,
	guardrailNoteView,
	guardrailSummary,
	guardrailView,
	isGuardrailRequest,
	rerouteTooltip
} from './guardrail';

const prompt = (input: Record<string, unknown> = {}) => ({
	tool_name: 'ModelGuardrail',
	tool_input: {
		guardrail: true,
		provider: 'anthropic',
		provider_label: 'Anthropic',
		category: 'cyber',
		explanation: 'This request was flagged by our cyber safety classifier.',
		requested_model: 'fake-ai',
		suggested_model: 'fake-ai-safe',
		suggested_by: 'provider',
		partial_output_discarded: false,
		...input
	}
});

describe('isGuardrailRequest', () => {
	it('knows a safety check by tool name or by the input marker', () => {
		expect(isGuardrailRequest(prompt())).toBe(true);
		expect(isGuardrailRequest({ tool_name: 'Other', tool_input: { guardrail: true } })).toBe(true);
		expect(isGuardrailRequest({ tool_name: 'Bash', tool_input: { command: 'ls' } })).toBe(false);
		expect(isGuardrailRequest(null)).toBe(false);
	});
});

describe('guardrailCategoryLabel', () => {
	it('says categories in plain words', () => {
		expect(guardrailCategoryLabel('cyber')).toBe('cybersecurity');
		expect(guardrailCategoryLabel('bio')).toBe('biology');
		expect(guardrailCategoryLabel('reasoning_extraction')).toBe('requests for its hidden reasoning');
		expect(guardrailCategoryLabel('some_new_thing')).toBe('some new thing');
		expect(guardrailCategoryLabel(null)).toBeNull();
		expect(guardrailCategoryLabel('  ')).toBeNull();
	});
});

describe('guardrailView', () => {
	it('builds the card wording', () => {
		const view = guardrailView(prompt())!;
		expect(view.heading).toBe("Anthropic's safety check stopped this request");
		expect(view.category).toBe('cybersecurity');
		expect(view.explanation).toMatch(/cyber safety classifier/);
		expect(view.continueLabel).toBe(`Continue on ${view.suggestedLabel}`);
		expect(view.suggestedByApp).toBe(false);
		expect(view.partialDiscarded).toBe(false);
		expect(view.hint).toMatch(/Only this turn/);
	});

	it('notes an app-picked fallback and a discarded partial reply', () => {
		const view = guardrailView(prompt({ suggested_by: 'app', partial_output_discarded: true }))!;
		expect(view.suggestedByApp).toBe(true);
		expect(view.partialDiscarded).toBe(true);
	});

	it('copes with missing fields', () => {
		const view = guardrailView({ tool_name: 'ModelGuardrail', tool_input: {} })!;
		expect(view.heading).toBe("The provider's safety check stopped this request");
		expect(view.continueLabel).toBe('Continue');
		expect(view.category).toBeNull();
		expect(view.explanation).toBeNull();
	});

	it('handles a provider name ending in s', () => {
		expect(guardrailView(prompt({ provider_label: 'Acme Labs' }))!.heading).toBe(
			"Acme Labs' safety check stopped this request"
		);
	});

	it('is null for any other request', () => {
		expect(guardrailView({ tool_name: 'Bash', tool_input: {} })).toBeNull();
	});

	it('summarises for the queue', () => {
		expect(guardrailSummary(prompt())).toMatch(/^Continue on .+\?$/);
		expect(guardrailSummary({ tool_name: 'Bash', tool_input: {} })).toBe('');
	});
});

describe('guardrailNoteView', () => {
	it('words a reroute with friendly labels and keeps the backend sentence for the tooltip', () => {
		const note = guardrailNoteView('model_rerouted', {
			content: 'Anthropic answered with fake-ai-safe instead of fake-ai.',
			provider_label: 'Anthropic',
			requested_model: 'fake-ai',
			served_model: 'fake-ai-safe',
			reason: 'refusal_fallback',
			category: 'cyber'
		})!;
		expect(note.kind).toBe('rerouted');
		expect(note.title).toBe('Safety check');
		expect(note.text).toMatch(/^Anthropic answered with .+ instead of .+ \(safety check: cybersecurity\)\.$/);
		expect(note.detail).toBe('Anthropic answered with fake-ai-safe instead of fake-ai.');
	});

	it('calls an availability reroute a reroute', () => {
		const note = guardrailNoteView('model_rerouted', {
			requested_model: 'a',
			served_model: 'b',
			reason: 'availability'
		})!;
		expect(note.title).toBe('Model rerouted');
		expect(note.text).toBe('The provider answered with b instead of a.');
	});

	it('falls back to the persisted content when models are missing', () => {
		const note = guardrailNoteView('model_rerouted', {}, 'Something changed.')!;
		expect(note.text).toBe('Something changed.');
	});

	it('says Continued on Y for a continued block', () => {
		const note = guardrailNoteView('guardrail_blocked', {
			outcome: 'continued',
			provider_label: 'Anthropic',
			category: 'bio',
			retry_model: 'fake-ai-safe',
			content: 'Anthropic blocked … Continued this turn on fake-ai-safe.'
		})!;
		expect(note.kind).toBe('continued');
		expect(note.text).toMatch(/^Continued on .+ — Anthropic's safety check blocked the request \(biology\)\.$/);
	});

	it('says Stopped with the reason for a stopped block', () => {
		const denied = guardrailNoteView('guardrail_blocked', {
			outcome: 'stopped',
			provider_label: 'OpenAI',
			reason: 'User denied permission'
		})!;
		expect(denied.kind).toBe('stopped');
		expect(denied.text).toBe("Stopped — OpenAI's safety check blocked the request; you chose Stop.");
		const again = guardrailNoteView('guardrail_blocked', { reason: 'blocked again after a retry' })!;
		expect(again.text).toBe("Stopped — the provider's safety check blocked the request; blocked again after a retry.");
	});

	it('is null for other notes', () => {
		expect(guardrailNoteView('info', {})).toBeNull();
		expect(guardrailNoteView(undefined, undefined)).toBeNull();
	});
});

describe('rerouteTooltip', () => {
	it('names the reason when known', () => {
		expect(rerouteTooltip('X', 'Y', 'refusal_fallback')).toBe('Safety check — running X instead of Y');
		expect(rerouteTooltip('X', 'Y', 'availability')).toBe('Model unavailable — running X instead of Y');
		expect(rerouteTooltip('X', 'Y', null)).toBe('Model rerouted: running X instead of Y');
	});
});
