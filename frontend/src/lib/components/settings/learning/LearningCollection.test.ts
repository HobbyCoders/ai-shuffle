/**
 * Settings > Learned behaviour against a mocked learner API: rules list with
 * the New badge, undo, adding a rule, the on/off switch writing the owner
 * policy, pass summaries in plain words, and registry coverage with data.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const http = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	api: http
}));

import { settingsNav } from '$lib/settings/navigation.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import LearningCollection from './LearningCollection.svelte';
import { passSummary, setupSummary, type LearnedRule, type LearnerOverview, type LearnerPass } from './learning-model.svelte';

const now = Math.floor(Date.now() / 1000);
const rule = (over: Partial<LearnedRule> = {}): LearnedRule => ({
	id: 'r1',
	revision: 2,
	metadata_version: 'meta+1',
	text: 'When a test fails: read the whole log before retrying.',
	applies_to: 'all',
	applies_label: 'All models',
	kept: false,
	new: true,
	previous_text: null,
	scope_label: 'All projects',
	needs_review: false,
	created_at: now - 60,
	updated_at: now - 60,
	expires_at: now + 86_400 * 30,
	...over
});
const pass = (over: Partial<LearnerPass> = {}): LearnerPass => ({
	at: now - 120,
	kind: 'pass',
	ran: true,
	read: 12,
	rules: [{ id: 'r1', text: rule().text }],
	memories: 1,
	input_tokens: 4_200,
	output_tokens: 180,
	outcome: 'ok',
	error_kind: null,
	...over
});

let overview: LearnerOverview;
function fresh(): LearnerOverview {
	return {
		setup: {
			enabled: true,
			daily_calls_setting: 0,
			daily_cap: 48,
			cap_source: 'metered_default',
			calls_today: 3,
			model: 'claude-haiku-4-5',
			provider: 'Anthropic API',
			billing: 'metered',
			pass_chars: 32_000
		},
		rules: [rule(), rule({ id: 'r2', text: 'When replying: keep it short.', kept: true, new: false, applies_to: 'claude', applies_label: 'Claude', expires_at: null })],
		activity: [pass()],
		guidance: [{ title: 'Act, don’t narrate', text: 'Do the work.', families: ['gpt', 'other models'] }],
		applies_to: [
			{ value: 'all', label: 'All models' },
			{ value: 'claude', label: 'Claude' },
			{ value: 'gpt', label: 'GPT / Codex' }
		],
		limits: { rule_chars: 300, learned_rules: 24 }
	};
}

async function show(item?: string) {
	settingsNav.reset();
	settingsNav.selectCategory('learning');
	if (item) settingsNav.go({ category: 'learning', item });
	const page = render(LearningCollection);
	await waitFor(() => expect(http.get).toHaveBeenCalledWith('/learner'));
	return page;
}

beforeEach(() => {
	vi.clearAllMocks();
	overview = fresh();
	http.get.mockImplementation(async () => structuredClone(overview));
	http.put.mockResolvedValue({});
	http.post.mockResolvedValue({});
	http.delete.mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe('plain-word summaries', () => {
	it('says what a pass did and what the setup is', () => {
		expect(passSummary(pass())).toBe('Read 12 entries · 1 rule · 1 memory');
		expect(passSummary(pass({ rules: [], memories: 0 }))).toContain('nothing new');
		expect(passSummary(pass({ outcome: 'budget' }))).toContain('daily call limit');
		expect(setupSummary(overview.setup)).toBe('On · 3 of 48 calls today');
		expect(setupSummary({ ...overview.setup, enabled: false })).toContain('no learning calls');
	});
});

describe('rules', () => {
	it('lists rules with a New badge and counts the new ones', async () => {
		await show();
		await screen.findByText(rule().text);
		expect(screen.getAllByText('New').length).toBeGreaterThan(0);
		expect(screen.getByText(/2 \/ 24 learned max/)).toBeTruthy();
	});

	it('undoes a rule the learner just added', async () => {
		http.post.mockResolvedValueOnce({ deleted: 'r1' });
		await show('r1');
		await fireEvent.click(await screen.findByRole('button', { name: 'Undo' }));
		await waitFor(() =>
			expect(http.post).toHaveBeenCalledWith('/learner/rules/r1/undo', { expected_revision: 2, expected_metadata_version: 'meta+1' })
		);
	});

	it('adds a rule as the owner’s own, with the family picked', async () => {
		http.post.mockResolvedValueOnce(rule({ id: 'r9', kept: true, new: false }));
		await show('new-rule');
		const box = (await screen.findByRole('textbox', { name: 'Rule text' })) as HTMLTextAreaElement;
		await fireEvent.input(box, { target: { value: '  When   asked for a plan: list   steps.  ' } });
		await fireEvent.change(screen.getByRole('combobox', { name: 'Applies to' }), { target: { value: 'gpt' } });
		await fireEvent.click(screen.getAllByRole('button', { name: 'Add rule' }).at(-1)!);
		await waitFor(() =>
			expect(http.post).toHaveBeenCalledWith('/learner/rules', { text: 'When asked for a plan: list steps.', applies_to: 'gpt' })
		);
	});

	it('refuses an empty rule without calling the worker', async () => {
		await show('new-rule');
		await screen.findByRole('textbox', { name: 'Rule text' });
		await fireEvent.click(screen.getAllByRole('button', { name: 'Add rule' }).at(-1)!);
		expect(await screen.findByText('Write the rule.')).toBeTruthy();
		expect(http.post).not.toHaveBeenCalled();
	});
});

describe('setup', () => {
	it('switches the learner off through the owner policy', async () => {
		await show('setup');
		const toggle = await screen.findByRole('switch', { name: 'Learn from chats' });
		overview.setup.enabled = false;
		await fireEvent.click(toggle);
		await waitFor(() => expect(http.put).toHaveBeenCalledWith('/settings/owner-policy', { learner_enabled: false }));
		expect(await screen.findAllByText(/no learning calls/)).not.toHaveLength(0);
	});

	it('shows the model, today’s calls and the per-pass cap', async () => {
		await show('setup');
		expect(await screen.findByText('claude-haiku-4-5')).toBeTruthy();
		expect(screen.getByText('Up to ~8k tokens')).toBeTruthy();
		expect(screen.getByRole('meter', { name: 'Learning calls used today' }).getAttribute('aria-valuenow')).toBe('3');
	});
});

describe('coverage', () => {
	it('renders every Learned behaviour registry entry where the registry says it lives', async () => {
		await assertCategoryCoverage(
			'learning',
			({ item, tab }) => {
				settingsNav.reset();
				settingsNav.go({ category: 'learning', item, tab });
				return render(LearningCollection);
			},
			{ settle: () => new Promise((done) => setTimeout(done, 30)) }
		);
	});
});
