/**
 * Model tuning: what each picker model starts a new chat with (effort, Fast
 * mode, the Standard context window) and, for a model on a local gateway,
 * its sampling and thinking switch.
 *
 * Items are the picker's models, so their ids are dynamic: search reaches
 * them through `searchSource()`. The one registry entry is the list itself.
 */
import { collectTuningModels } from '$lib/components/settings/tuning/tuning-model';
import type { SearchItem, SettingEntry } from '../types';

export const TUNING_HELP =
	'What a new chat on each model starts with. These are the same per-model choices the chat composer remembers when you change effort, Fast mode or the context window there, saved to your account and shared by every device. An open chat keeps its own settings.';

export const entries: SettingEntry[] = [
	{
		id: 'tuning.models',
		category: 'tuning',
		section: 'Models',
		label: 'Model tuning',
		hint: 'Per-model new chat defaults',
		help: TUNING_HELP,
		keywords: [
			'effort',
			'reasoning effort',
			'thinking',
			'fast mode',
			'service tier',
			'context window',
			'standard context',
			'sampling',
			'temperature',
			'top p',
			'top k',
			'min p',
			'repeat penalty',
			'local model',
			'llama.cpp',
			'vllm',
			'qwen3 preset'
		]
	}
];

export function searchSource(): SearchItem[] {
	return collectTuningModels().map((m) => ({
		title: m.label,
		where: `Model tuning, ${m.group}`,
		keywords: `${m.key} ${m.routes.map((r) => r.group).join(' ')} effort fast context tuning`,
		target: { category: 'tuning', item: m.item }
	}));
}
