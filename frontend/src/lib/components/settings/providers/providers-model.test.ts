import { describe, expect, it } from 'vitest';
import {
	planLabel,
	addableKinds,
	attentionCount,
	buildProviders,
	classifyRow,
	CLAUDE_KEY_ITEM,
	claudeGateNotice,
	CLAUDE_SUB_ITEM,
	claudeHeaderStatus,
	claudeInUse,
	claudeUsageRecorded,
	draftName,
	FAL_KEY_ITEM,
	filterProviders,
	hasPlusMenu,
	lineState,
	splitProviders,
	type ProvidersInput
} from './providers-model';
import { chatgptRow, claudeSummary, gatewayRow, grokRow, row } from './fixtures';

const noFal = { keySet: false, masked: '', enabled: true };
const input = (over: Partial<ProvidersInput> = {}): ProvidersInput => ({
	rows: [],
	claude: claudeSummary(),
	fal: noFal,
	...over
});
const byId = (views: ReturnType<typeof buildProviders>, id: string) => views.find((p) => p.id === id)!;

describe('row classification', () => {
	it('keys on auth_mode first, then provider_type', () => {
		expect(classifyRow({ provider_type: 'custom', auth_mode: 'chatgpt' })).toBe('chatgpt');
		expect(classifyRow({ provider_type: 'grok', auth_mode: null })).toBe('grok');
		expect(classifyRow({ provider_type: 'openai', auth_mode: null })).toBe('openai');
		expect(classifyRow({ provider_type: 'xai', auth_mode: null })).toBe('xai');
		expect(classifyRow({ provider_type: 'openrouter', auth_mode: null })).toBe('openrouter');
		expect(classifyRow({ provider_type: 'custom', auth_mode: null })).toBe('custom');
	});
});

describe('grouping', () => {
	it('lists only connected credentials and puts empty providers under Not connected', () => {
		const views = buildProviders(
			input({
				rows: [row(), chatgptRow(), grokRow(), gatewayRow()],
				fal: { keySet: true, masked: '…9c1e', enabled: true }
			})
		);
		const { connected, notConnected } = splitProviders(views);
		expect(connected.map((p) => p.name)).toEqual(['OpenAI', 'xAI Grok', 'Claude', 'fal.ai', 'Homelab vLLM']);
		expect(notConnected.map((p) => p.name)).toEqual(['OpenRouter']);
		// Subscriptions come before keys inside a provider.
		expect(byId(views, 'openai').credentials.map((c) => c.flavor)).toEqual(['device', 'key']);
		expect(byId(views, 'gw:gw-1').credentials.map((c) => c.name)).toEqual(['Gateway key']);
		expect(byId(views, 'fal').credentials[0].item).toBe(FAL_KEY_ITEM);
	});

	it('names a legacy key row after its provider brand', () => {
		const views = buildProviders(input({ rows: [row({ name: 'OpenAI' })] }));
		expect(byId(views, 'openai').credentials[0].name).toBe('OpenAI API key');
	});

	it('counts the credentials that are on; independent rows can all be on', () => {
		const views = buildProviders(input({ rows: [chatgptRow(), row({ id: 'k1' }), row({ id: 'k2', enabled: false })] }));
		const openai = byId(views, 'openai');
		expect(openai.onCount).toBe(2);
		expect(openai.credentials.map((c) => [c.switchable, c.on])).toEqual([
			[true, true],
			[true, true],
			[true, false]
		]);
	});

	it('marks an expired subscription for attention without taking it off its switch', () => {
		const views = buildProviders(input({ rows: [grokRow()] }));
		const sub = byId(views, 'grok').credentials[0];
		expect(sub).toMatchObject({
			status: 'warn',
			statusText: 'Reconnect needed',
			on: false,
			enabled: true,
			attention: true
		});
		expect(attentionCount(views)).toBe(1);
	});

	it('hides a row a setup created until the draft finishes', () => {
		const views = buildProviders(
			input({
				rows: [row({ id: 'new-key' })],
				drafts: [{ item: 'draft:1', providerId: 'openai', flavor: 'key', name: 'OpenAI API key', rowId: 'new-key' }]
			})
		);
		expect(byId(views, 'openai').credentials.map((c) => c.item)).toEqual(['draft:1']);
		expect(byId(views, 'openai').credentials[0]).toMatchObject({ status: 'busy', switchable: false });
	});

	it('shows a new gateway as its own provider while it is set up', () => {
		const views = buildProviders(
			input({
				drafts: [{ item: 'draft:2', providerId: 'gw:draft-2', flavor: 'gateway', name: 'Gateway key' }],
				draftGateways: [{ id: 'gw:draft-2', name: 'Together AI' }]
			})
		);
		const gw = byId(views, 'gw:draft-2');
		expect(gw).toMatchObject({ name: 'Together AI', draft: true, letter: 'T' });
		expect(splitProviders(views).connected).toContain(gw);
	});

	it('filters by provider name or credential text', () => {
		const views = buildProviders(input({ rows: [chatgptRow(), row()] }));
		expect(filterProviders(views, 'openai').find((p) => p.id === 'openai')!.credentials).toHaveLength(2);
		const hit = filterProviders(views, 'api.openai');
		expect(hit.map((p) => p.id)).toEqual(['openai']);
		expect(hit[0].credentials).toHaveLength(1);
	});
});

describe('ChatGPT and SuperGrok subscriptions', () => {
	const openaiSub = (views: ReturnType<typeof buildProviders>) => byId(views, 'openai').credentials[0];

	it('names the plan and shows the account like the Claude row', () => {
		const sub = openaiSub(
			buildProviders(input({ rows: [chatgptRow({ account_email: 'owner@example.com', account_plan: 'plus' })] }))
		);
		expect(sub.name).toBe('OpenAI Plus subscription');
		expect(sub.summary).toBe('owner@example.com, Plus plan');
	});

	it('prefers the usage snapshot plan over the sign-in one', () => {
		const sub = openaiSub(
			buildProviders(
				input({
					rows: [chatgptRow({ account_email: 'owner@example.com', account_plan: 'plus' })],
					usage: [{ provider_id: 'chatgpt-1', plan_type: 'pro' }]
				})
			)
		);
		expect(sub.name).toBe('OpenAI Pro subscription');
		expect(sub.summary).toBe('owner@example.com, Pro plan');
	});

	it('maps plan ids to display names', () => {
		const name = (plan: string) =>
			openaiSub(buildProviders(input({ rows: [chatgptRow({ account_plan: plan })] }))).name;
		expect(name('team')).toBe('OpenAI Team subscription');
		expect(name('business')).toBe('OpenAI Business subscription');
		expect(name('enterprise')).toBe('OpenAI Enterprise subscription');
		expect(name('free')).toBe('OpenAI Free subscription');
		expect(name('unknown')).toBe('OpenAI subscription');
	});

	it('falls back without an empty comma when the email or plan is unknown', () => {
		const only = (over: Parameters<typeof chatgptRow>[0]) => openaiSub(buildProviders(input({ rows: [chatgptRow(over)] })));
		expect(only({ account_email: 'owner@example.com' })).toMatchObject({
			name: 'OpenAI subscription',
			summary: 'owner@example.com'
		});
		expect(only({ account_plan: 'plus' }).summary).toBe('Plus plan');
		expect(only({})).toMatchObject({ name: 'OpenAI subscription', summary: '1 model' });
		expect(only({ enabled: false })).toMatchObject({ summary: 'Off' });
		expect(only({ account_email: 'owner@example.com', enabled: false }).summary).toBe('owner@example.com, off');
		expect(only({ account_email: 'owner@example.com', needs_reauth: true }).summary).toBe('Reconnect needed');
	});

	it('titles a SuperGrok subscription by its tier', () => {
		const grok = (plan?: string) =>
			byId(
				buildProviders(
					input({
						rows: [grokRow({ needs_reauth: false, account_email: 'me@x.com' })],
						usage: plan ? [{ provider_id: 'grok-1', plan_type: plan }] : []
					})
				),
				'grok'
			).credentials[0];
		expect(grok('SuperGrok Heavy')).toMatchObject({
			name: 'SuperGrok Heavy subscription',
			summary: 'me@x.com, SuperGrok Heavy plan'
		});
		expect(grok().name).toBe('SuperGrok subscription');
	});
});

describe('Claude', () => {
	it('subscription mode: the subscription is active, the key on standby, no switches', () => {
		const claude = byId(buildProviders(input()), 'claude');
		expect(claude.credentials.map((c) => [c.item, c.status, c.on, c.switchable])).toEqual([
			[CLAUDE_SUB_ITEM, 'ok', true, false],
			[CLAUDE_KEY_ITEM, 'standby', false, false]
		]);
		expect(claude.credentials[0].name).toBe('Claude Max subscription');
		expect(hasPlusMenu(claude)).toBe(false);
	});

	it('API key mode: the key is active', () => {
		const claude = byId(buildProviders(input({ claude: claudeSummary({ mode: 'api_key' }) })), 'claude');
		expect(claude.credentials.map((c) => c.on)).toEqual([false, true]);
		expect(claude.credentials[1].statusText).toBe('Active for Claude');
	});

	it('off: nothing is active and both rows read Off', () => {
		const summary = claudeSummary({ mode: 'off' });
		const claude = byId(buildProviders(input({ claude: summary })), 'claude');
		expect(claude.credentials.map((c) => [c.on, c.status, c.statusText])).toEqual([
			[false, 'off', 'Off'],
			[false, 'off', 'Off']
		]);
		expect(claude.onCount).toBe(0);
		expect(claudeHeaderStatus(summary)).toEqual({ status: 'off', text: 'Off, not in the picker' });
	});

	it('is not connected and needs nothing when Claude was never set up', () => {
		// The worker records "subscription" by default; that alone must not
		// put a warned Claude row in the list or in the attention badge.
		const summary = claudeSummary({
			mode: 'subscription',
			cli: {
				installed: false,
				signed_in: false,
				email: null,
				org_name: null,
				plan: null,
				auth_method_label: null,
				error: null
			},
			api_key: { set: false, last4: null }
		});
		const views = buildProviders(input({ claude: summary }));
		expect(byId(views, 'claude').credentials).toEqual([]);
		expect(splitProviders(views).notConnected.map((p) => p.id)).toContain('claude');
		expect(attentionCount(views)).toBe(0);
	});

	describe('a lapsed Claude sign-in', () => {
		const signedOut = {
			installed: true, signed_in: false, email: null, org_name: null, plan: null, auth_method_label: null, error: null
		};
		const noKey = { set: false, last4: null };

		const DAY = 24 * 60 * 60 * 1000;
		const lastWeek = { windows: [{ type: 'five_hour', observed_at: Date.now() - 7 * DAY }] };

		it('keeps the warned subscription row and counts it when Claude was used last week', () => {
			const summary = claudeSummary({ mode: 'subscription', cli: signedOut, api_key: noKey });
			const used = claudeUsageRecorded(lastWeek);
			expect(used).toBe(true);
			expect(claudeInUse(summary, used)).toBe(true);
			const views = buildProviders(input({ claude: summary, claudeUsed: used }));
			const claude = byId(views, 'claude');
			expect(claude.credentials.map((c) => [c.item, c.status, c.attention])).toEqual([[CLAUDE_SUB_ITEM, 'warn', true]]);
			expect(splitProviders(views).connected.map((p) => p.id)).toContain('claude');
			expect(attentionCount(views)).toBe(1);
		});

		it('keeps the warned key row when the API key mode was used recently and the key is gone', () => {
			const summary = claudeSummary({ mode: 'api_key', cli: signedOut, api_key: noKey });
			const views = buildProviders(input({ claude: summary, claudeUsed: claudeUsageRecorded(lastWeek) }));
			expect(byId(views, 'claude').credentials.map((c) => [c.item, c.attention])).toEqual([[CLAUDE_KEY_ITEM, true]]);
			expect(attentionCount(views)).toBe(1);
		});

		it('usage observed 3 months ago with no sign-in or key is not in use', () => {
			const old = { windows: [{ type: 'five_hour', observed_at: Date.now() - 90 * DAY }, { type: 'seven_day' }] };
			for (const mode of ['subscription', 'api_key'] as const) {
				const summary = claudeSummary({ mode, cli: signedOut, api_key: noKey });
				const used = claudeUsageRecorded(old);
				expect(used).toBe(false);
				expect(claudeInUse(summary, used)).toBe(false);
				const views = buildProviders(input({ claude: summary, claudeUsed: used }));
				expect(byId(views, 'claude').credentials).toEqual([]);
				expect(splitProviders(views).notConnected.map((p) => p.id)).toContain('claude');
				expect(attentionCount(views)).toBe(0);
			}
		});

		it('an owner who never used Claude still sees it under Not connected, with no warning', () => {
			const summary = claudeSummary({ mode: 'subscription', cli: signedOut, api_key: noKey });
			const used = claudeUsageRecorded({ windows: [] });
			expect(claudeInUse(summary, used)).toBe(false);
			const views = buildProviders(input({ claude: summary, claudeUsed: used }));
			expect(byId(views, 'claude').credentials).toEqual([]);
			expect(splitProviders(views).notConnected.map((p) => p.id)).toContain('claude');
			expect(attentionCount(views)).toBe(0);
		});

		it('Claude turned off is not in use, whatever was recorded', () => {
			const summary = claudeSummary({ mode: 'off', cli: signedOut, api_key: noKey });
			expect(claudeInUse(summary, true)).toBe(false);
		});
	});

	it('shows the selected credential even when it is not ready', () => {
		const summary = claudeSummary({
			mode: 'subscription',
			cli: {
				installed: true,
				signed_in: false,
				email: null,
				org_name: null,
				plan: null,
				auth_method_label: null,
				error: null
			},
			api_key: { set: true, last4: '1234' },
			gate: {
				ready: false,
				mode: 'subscription',
				claude_credential_ready: false,
				reason: 'Sign in',
				license_allows_runs: true
			}
		});
		const views = buildProviders(input({ claude: summary }));
		const claude = byId(views, 'claude');
		expect(claude.credentials.map((c) => [c.item, c.status, c.statusText])).toEqual([
			[CLAUDE_SUB_ITEM, 'warn', 'Not signed in'],
			[CLAUDE_KEY_ITEM, 'standby', 'Standby']
		]);
		expect(attentionCount(views)).toBe(1);
		expect(claudeHeaderStatus(summary).status).toBe('warn');
	});

	it('is not connected when off with nothing signed in or stored', () => {
		const summary = claudeSummary({
			mode: 'off',
			cli: {
				installed: true,
				signed_in: false,
				email: null,
				org_name: null,
				plan: null,
				auth_method_label: null,
				error: null
			},
			api_key: { set: false, last4: null }
		});
		const { notConnected } = splitProviders(buildProviders(input({ claude: summary })));
		expect(notConnected.map((p) => p.id)).toContain('claude');
	});
});

describe('connector line', () => {
	it('is teal into the credentials that are on', () => {
		expect(lineState([{ on: true }, { on: false }, { on: true }])).toEqual({
			lit: true,
			rows: [
				{ on: true, nextOn: false, last: false },
				{ on: false, nextOn: true, last: false },
				{ on: true, nextOn: false, last: true }
			]
		});
		expect(lineState([{ on: false }]).lit).toBe(false);
		expect(lineState([]).lit).toBe(false);
	});
});

describe('"+" menu', () => {
	it('offers the kinds not added yet and another of a kind that allows several', () => {
		const views = buildProviders(input({ rows: [chatgptRow(), row()] }));
		expect(addableKinds(byId(views, 'openai')).map((k) => k.menuLabel)).toEqual(['Another API key']);
		expect(addableKinds(byId(views, 'grok')).map((k) => k.menuLabel)).toEqual(['Subscription', 'API key']);
	});

	it('is complete for fal.ai and a gateway once their one credential exists', () => {
		const views = buildProviders(input({ rows: [gatewayRow()], fal: { keySet: true, masked: '', enabled: false } }));
		expect(addableKinds(byId(views, 'fal'))).toEqual([]);
		expect(addableKinds(byId(views, 'gw:gw-1'))).toEqual([]);
		expect(byId(views, 'fal').credentials[0]).toMatchObject({ on: false, status: 'off', switchable: true });
	});

	it('numbers draft names after existing ones', () => {
		expect(draftName('openai', 'key', [{ name: 'OpenAI API key' }])).toBe('OpenAI API key 2');
		expect(draftName('grok', 'device', [])).toBe('SuperGrok subscription');
	});
});

describe('plan label', () => {
	it('capitalizes the plan the worker reports', () => {
		expect(planLabel('pro')).toBe('Pro');
		expect(planLabel('supergrok_heavy')).toBe('SuperGrok Heavy');
		expect(planLabel('prolite')).toBe('Pro Lite');
		expect(planLabel('unknown')).toBeNull();
		expect(planLabel('')).toBeNull();
		expect(planLabel(null)).toBeNull();
	});
});

describe('claudeGateNotice', () => {
	const gate = { ready: true, mode: 'subscription' as const, claude_credential_ready: true, reason: null, license_allows_runs: true };

	it('is quiet when Claude is ready or off', () => {
		expect(claudeGateNotice(claudeSummary({ gate }))).toBeNull();
		expect(claudeGateNotice(claudeSummary({ mode: 'off', gate: { ...gate, claude_credential_ready: false } }))).toBeNull();
	});

	it('warns about Claude even while another provider keeps the gate ready', () => {
		const summary = claudeSummary({ mode: 'subscription', gate: { ...gate, ready: true, claude_credential_ready: false } });
		expect(claudeGateNotice(summary)).toBe('Claude cannot run: the Claude Code CLI is not signed in on this worker.');
	});

	it('names the missing key in API key mode', () => {
		const summary = claudeSummary({ mode: 'api_key', gate: { ...gate, mode: 'api_key', claude_credential_ready: false } });
		expect(claudeGateNotice(summary)).toBe('Claude cannot run: no Anthropic API key is stored on this worker.');
	});

	it('shows the license reason', () => {
		const summary = claudeSummary({ gate: { ...gate, ready: false, license_allows_runs: false, reason: 'License expired.' } });
		expect(claudeGateNotice(summary)).toBe('License expired.');
	});
});
