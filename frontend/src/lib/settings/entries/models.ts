/**
 * Default models: text tasks, image, video, 3D, audio, dictation, voice calls,
 * and the fal.ai catalog. A collection: every row lives in one task's detail.
 *
 * Item ids: `text`, `dict`, `voicecall`, `catalog`, and one per media tool
 * (`t2i`, `edit`, `t2v`, `i2v`, `t23d`, `i23d`, `tts`, `stt`, `t2a`, `a2a`).
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
		hint: 'Fixed for new calls',
		keywords: ['voice calls model', 'gpt live', 'gpt-live-1', 'realtime']
	},
	{ ...OPTIONS, id: 'models.imageAspect', item: 't2i', label: 'Aspect ratio', keywords: ['image aspect ratio', 'text to image'] },
	{ ...OPTIONS, id: 'models.imageResolution', item: 't2i', label: 'Resolution', keywords: ['image resolution', 'text to image', 'size'] },
	{ ...OPTIONS, id: 'models.videoAspect', item: 't2v', label: 'Aspect ratio', keywords: ['video aspect ratio', 'text to video'] },
	{ ...OPTIONS, id: 'models.videoDuration', item: 't2v', label: 'Duration', keywords: ['video duration', 'length', 'seconds'] },
	{ ...OPTIONS, id: 'models.videoResolution', item: 't2v', label: 'Resolution', keywords: ['video resolution', 'text to video'] },
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

const TOOLS: [item: string, title: string, group: string, keywords: string][] = [
	['t2i', 'Text to image', 'Image', 'image model image_model generation fal'],
	['edit', 'Image editing', 'Image', 'image edit model image_edit_model fal'],
	['t2v', 'Text to video', 'Video', 'video model video_model fal'],
	['i2v', 'Image to video', 'Video', 'animate i2v_model fal'],
	['t23d', 'Text to 3D', '3D', '3d model model3d_model mesh fal'],
	['i23d', 'Image to 3D', '3D', '3d model image_to_3d_model mesh fal'],
	['tts', 'Text to speech', 'Audio', 'tts model tts_model voice read aloud fal'],
	['stt', 'Speech to text', 'Audio', 'stt model stt_model transcribe audio files fal'],
	['t2a', 'Text to audio', 'Audio', 'music sound effects audio_model fal'],
	['a2a', 'Audio to audio', 'Audio', 'remix stems audio_to_audio_model fal']
];

/** One search item per task, landing on its detail. */
export function searchSource(): SearchItem[] {
	return [
		{
			title: 'Text tasks',
			where: 'Default models, Text',
			keywords: 'text model effort generated text titles commits',
			target: { category: 'models', item: 'text' }
		},
		...TOOLS.map(([item, title, group, keywords]) => ({
			title,
			where: `Default models, ${group}`,
			keywords: `${keywords} media model default`,
			target: { category: 'models' as const, item }
		})),
		{
			title: 'Text to speech model',
			where: 'Default models, Text to speech',
			keywords: 'tts model pick recommended',
			target: { category: 'models', item: 'tts', tab: 'Model' }
		},
		{
			title: 'Model parameters',
			where: 'Default models, catalog models',
			keywords: 'dynamic schema fal options',
			target: { category: 'models', item: 't2i', tab: 'Settings' }
		},
		{
			title: 'Dictation',
			where: 'Default models, Audio',
			keywords: 'dictation transcription voice input whisper',
			target: { category: 'models', item: 'dict' }
		},
		{
			title: 'Voice calls',
			where: 'Default models, Audio',
			keywords: 'gpt live realtime voice calls model',
			target: { category: 'models', item: 'voicecall' }
		},
		{
			title: 'All fal.ai models',
			where: 'Default models, Catalog',
			keywords: 'catalog browse fal models refresh',
			target: { category: 'models', item: 'catalog' }
		}
	];
}
