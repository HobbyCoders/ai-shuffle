/**
 * Default models: remembered generation choices, app tasks (text tasks,
 * dictation, read-aloud, voice calls) and the fal.ai catalog. A collection:
 * every row lives in one item's detail.
 *
 * Item ids: `gen-image`, `gen-video`, `gen-audio`, `gen-3d`, `text`, `dict`,
 * `tts`, `voicecall`, `catalog`. The removed per-tool media items redirect
 * (MOVED_MODEL_ITEMS in navigation.svelte.ts).
 */
import type { SearchItem, SettingEntry } from '../types';

const TEXT = { category: 'models', section: 'Model', item: 'text' } as const;
const OPTIONS = { category: 'models', section: 'Options', tab: 'Settings' } as const;

export const entries: SettingEntry[] = [
	{
		...TEXT,
		id: 'models.textModel',
		label: 'Model',
		hint: 'Titles, commits, PRs, branches',
		keywords: [
			'text tasks',
			'text generation model',
			'generated text',
			'session titles',
			'commit messages',
			'pr descriptions',
			'branch names',
			'agent generation'
		]
	},
	{
		...TEXT,
		id: 'models.textEffort',
		label: 'Effort',
		keywords: ['reasoning effort', 'text generation effort', 'thinking'],
		default: ''
	},
	{
		id: 'models.dictation',
		category: 'models',
		section: 'Model',
		item: 'dict',
		label: 'Model',
		hint: 'Chat mic and hotkey',
		keywords: ['dictation model', 'transcription model', 'voice input', 'whisper', 'speech to text', 'microphone'],
		default: 'fal-ai/whisper'
	},
	{
		id: 'models.voiceCallModel',
		category: 'models',
		section: 'Model',
		item: 'voicecall',
		label: 'Model',
		hint: 'Selected in Voice settings',
		keywords: ['voice calls model', 'gpt live', 'gpt-live-1', 'realtime']
	},
	{
		...OPTIONS,
		id: 'models.ttsVoice',
		item: 'tts',
		label: 'Voice',
		keywords: ['text to speech voice', 'tts voice', 'kokoro', 'af_heart', 'read aloud']
	},
	{ ...OPTIONS, id: 'models.ttsSpeed', item: 'tts', label: 'Speed', keywords: ['text to speech speed', 'tts rate'] },
	{ ...OPTIONS, id: 'models.ttsFormat', item: 'tts', label: 'Output format', keywords: ['text to speech format', 'wav', 'mp3'] }
];

const GENERATION: [item: string, title: string, keywords: string][] = [
	['gen-image', 'Images', 'image generation text to image remembered choice source model fal supergrok chatgpt plan'],
	['gen-video', 'Video', 'video generation text to video remembered choice source model fal supergrok'],
	['gen-audio', 'Audio and sound', 'audio generation music sound effects remembered choice source model fal'],
	['gen-3d', '3D models', '3d generation text to 3d mesh remembered choice source model fal']
];

/** One search item per task, landing on its detail. */
export function searchSource(): SearchItem[] {
	return [
		{
			title: 'Text tasks',
			where: 'Default models, App tasks',
			keywords: 'text model effort generated text titles commits',
			target: { category: 'models', item: 'text' }
		},
		...GENERATION.map(([item, title, keywords]) => ({
			title,
			where: 'Default models, Generation',
			keywords: `${keywords} ask each time default`,
			target: { category: 'models' as const, item }
		})),
		{
			title: 'Read aloud',
			where: 'Default models, App tasks',
			keywords: 'tts text to speech model voice read aloud fal',
			target: { category: 'models', item: 'tts' }
		},
		{
			title: 'Read aloud model',
			where: 'Default models, Read aloud',
			keywords: 'tts model pick recommended',
			target: { category: 'models', item: 'tts', tab: 'Model' }
		},
		{
			title: 'Dictation',
			where: 'Default models, App tasks',
			keywords: 'dictation transcription voice input whisper',
			target: { category: 'models', item: 'dict' }
		},
		{
			title: 'Voice calls',
			where: 'Default models, App tasks',
			keywords: 'gpt live realtime voice calls model',
			target: { category: 'models', item: 'voicecall' }
		},
		{
			title: 'All fal.ai models',
			where: 'Default models, Catalog',
			keywords: 'catalog browse fal models refresh pin image editing image to video image to 3d audio to audio speech to text',
			target: { category: 'models', item: 'catalog' }
		}
	];
}
