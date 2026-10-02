import { describe, expect, it } from 'vitest';
import {
	addableKinds,
	attentionCount,
	buildProviders,
	classifyRow,
	CLAUDE_KEY_ITEM,
	CLAUDE_SUB_ITEM,
	claudeHeaderStatus,
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
		expect(connected.map((p) => p.name)).toEqual(['Claude', 'OpenAI', 'xAI Grok', 'fal.ai', 'Homelab vLLM']);
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
		const views = buildProviders(
			input({ rows: [chatgptRow(), row({ id: 'k1' }), row({ id: 'k2', enabled: false })] })
		);
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
		expect(sub).toMatchObject({ status: 'warn', statusText: 'Reconnect needed', on: false, enabled: true, attention: true });
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

	it('shows the selected credential even when it is not ready', () => {
		const summary = claudeSummary({
			mode: 'subscription',
			cli: { installed: true, signed_in: false, email: null, org_name: null, plan: null, auth_method_label: null, error: null },
			api_key: { set: false, last4: null },
			gate: { ready: false, mode: 'subscription', claude_credential_ready: false, reason: 'Sign in', license_allows_runs: true }
		});
		const views = buildProviders(input({ claude: summary }));
		const claude = byId(views, 'claude');
		expect(claude.credentials.map((c) => [c.item, c.status, c.statusText])).toEqual([[CLAUDE_SUB_ITEM, 'warn', 'Not signed in']]);
		expect(attentionCount(views)).toBe(1);
		expect(claudeHeaderStatus(summary).status).toBe('warn');
	});

	it('is not connected when off with nothing signed in or stored', () => {
		const summary = claudeSummary({
			mode: 'off',
			cli: { installed: true, signed_in: false, email: null, org_name: null, plan: null, auth_method_label: null, error: null },
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
