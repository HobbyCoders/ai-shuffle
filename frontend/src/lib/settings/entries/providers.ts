/**
 * Providers: providers and their credentials.
 *
 * Registry entries cover the rows that live at stable places (Claude's mode,
 * the Claude subscription's Policy tab, the Anthropic key, the fal.ai media
 * key). Credentials that are model_providers rows have dynamic ids, so search
 * reaches them through `searchSource()`. The index badge counts credentials
 * that need the user (reconnect, sign in, missing key).
 */
import { providers as providersStore } from '$lib/stores/providers.svelte';
import {
	attentionCount,
	buildProviders,
	claudeUsageRecorded,
	CLAUDE_KEY_ITEM,
	CLAUDE_PROVIDER_ITEM,
	CLAUDE_SUB_ITEM,
	FAL_KEY_ITEM,
	isConnected,
	kindsOf,
	supportsNativeImages,
	type ProviderView
} from '$lib/components/settings/providers/providers-model';
import type { Attention, SearchItem, SettingEntry } from '../types';

export const MODE_HELP =
	'Claude models run through the Claude Code CLI on this worker. Subscription uses the CLI’s own sign-in; API key passes the stored Anthropic key. Every Claude session here uses the one you pick; nothing switches on its own. Off removes Claude models from the picker. Prefer to pay per use? An API key suits scheduled and unattended runs.';
export const LOOP_HELP =
	'While Claude runs on your subscription, work that starts without you (automations, wake-ups, connectors, and the workers they start) counts against an hourly budget. Past it, the next turn waits for you under Waiting for you. Nothing you start yourself is held or timed, including the workers your chats start.';
const LOOP_WORKER_LIMIT_HELP =
	'Stops a worker that an automation, wake-up or connector started once it has run this long. Workers your own chats start have no limit. A change reaches running workers within a minute.';
export const FAL_BALANCE_HELP =
	'Read with the media key itself, at most once every five minutes. Usage shows the same reading.';
export const NATIVE_IMAGE_HELP =
	'GenerateNativeImage uses the session’s own provider and credential, separate from fal.ai, and never switches providers. Images are saved with the session. Session cost estimates leave these charges out.';

export const entries: SettingEntry[] = [
	{
		id: 'providers.claudeMode',
		category: 'providers',
		section: 'Claude Code mode',
		label: 'Runs on',
		hint: 'Every Claude session here',
		help: MODE_HELP,
		item: CLAUDE_PROVIDER_ITEM,
		keywords: ['claude code mode', 'subscription', 'api key', 'off', 'turn claude off', 'disable claude', 'which credential', 'pay per use']
	},
	{
		id: 'providers.loopGuard',
		category: 'providers',
		section: 'Loop guard',
		label: 'Hold unattended turns',
		help: LOOP_HELP,
		item: CLAUDE_SUB_ITEM,
		tab: 'Policy',
		keywords: ['loop guard', 'unattended', 'budget', 'automations', 'wake-ups', 'runaway'],
		default: true
	},
	{
		id: 'providers.loopSession',
		category: 'providers',
		section: 'Loop guard',
		label: 'Per session',
		hint: 'Unattended turns only',
		item: CLAUDE_SUB_ITEM,
		tab: 'Policy',
		keywords: ['loop guard', 'turns per hour', 'session budget'],
		default: 12
	},
	{
		id: 'providers.loopWorker',
		category: 'providers',
		section: 'Loop guard',
		label: 'Per worker',
		hint: 'Unattended turns only',
		item: CLAUDE_SUB_ITEM,
		tab: 'Policy',
		keywords: ['loop guard', 'turns per hour', 'worker budget'],
		default: 60
	},
	{
		id: 'providers.loopBackground',
		category: 'providers',
		section: 'Loop guard',
		label: 'Unattended worker limit',
		hint: '0 turns it off',
		help: LOOP_WORKER_LIMIT_HELP,
		item: CLAUDE_SUB_ITEM,
		tab: 'Policy',
		keywords: ['background worker', 'unattended', 'time limit', 'minutes'],
		default: 60
	},
	{
		id: 'providers.cliSignOut',
		category: 'providers',
		section: 'Danger zone',
		label: 'Sign out of the Claude CLI',
		hint: 'Runs claude auth logout',
		item: CLAUDE_SUB_ITEM,
		keywords: ['logout', 'switch account', 'claude cli']
	},
	{
		id: 'providers.anthropicKey',
		category: 'providers',
		section: 'Key',
		label: 'Replace key',
		item: CLAUDE_KEY_ITEM,
		tab: 'Overview',
		keywords: ['anthropic api key', 'sk-ant', 'claude api key']
	},
	{
		id: 'providers.falKey',
		category: 'providers',
		section: 'Key',
		label: 'Replace key',
		item: FAL_KEY_ITEM,
		keywords: ['fal.ai', 'fal key', 'media key', 'image video audio 3d']
	},
	{
		id: 'providers.falBalance',
		category: 'providers',
		section: 'Balance',
		label: 'Credit left',
		hint: 'Read with this key',
		help: FAL_BALANCE_HELP,
		item: FAL_KEY_ITEM,
		keywords: ['fal.ai balance', 'credits', 'billing']
	}
];

function currentViews(): ProviderView[] {
	const s = providersStore.current;
	return buildProviders({
		rows: s.providers,
		claude: s.claude,
		anthropicKeyMasked: s.anthropicKeyMasked,
		fal: { keySet: s.falKeySet, masked: s.falKeyMasked, enabled: s.falEnabled },
		usage: s.usage?.providers,
		claudeUsed: claudeUsageRecorded(s.usage)
	});
}

/** Providers, credentials and their tabs, from the live store. */
export function searchSource(): SearchItem[] {
	const out: SearchItem[] = [];
	for (const p of currentViews()) {
		const connected = isConnected(p);
		out.push({
			title: p.name,
			where: connected ? 'Providers' : 'Providers, Not connected',
			keywords: `provider ${kindsOf(p.id).map((k) => k.label).join(' ')}`,
			target: { category: 'providers', item: p.item }
		});
		for (const c of p.credentials) {
			if (c.draft) continue;
			const where = `Providers, ${p.name}`;
			out.push({ title: c.name, where, keywords: `${c.kind} credential`, target: { category: 'providers', item: c.item } });
			const at = (title: string, tab: string, keywords: string) =>
				out.push({ title, where: `${where}, ${c.name}, ${tab}`, keywords, target: { category: 'providers', item: c.item, tab } });
			if (c.flavor === 'device') {
				at('Banked resets', 'Usage', 'codex rate limit reset use');
				at('Weekly allowance', 'Usage', 'subscription usage limit');
			}
			if (c.flavor === 'device' || c.flavor === 'key' || c.flavor === 'gateway') {
				at('Model curation', 'Models', 'offered models picker checklist');
			}
			if (c.flavor === 'gateway' || c.flavor === 'key') {
				at('Connection', 'Overview', 'endpoint token dialect context window max output');
			}
			if (c.flavor === 'key' && c.row?.provider_type === 'openrouter') at('OpenRouter credits', 'Usage', 'balance billing');
			if (supportsNativeImages(c.row)) at('Native image generation', 'Overview', 'generatenativeimage');
			if (c.flavor === 'cli') {
				at('Held turns', 'Policy', 'waiting for confirmation confirm dismiss loop guard');
				at('Claude CLI sign-in', 'Overview', 'login terminal auth check again');
				at('Plan windows', 'Usage', '5-hour weekly limit usage');
			}
		}
	}
	return out;
}

/** Credentials that need the user. */
export function attention(): Attention | null {
	const count = attentionCount(currentViews());
	return count ? { count, tone: 'warn' } : null;
}
