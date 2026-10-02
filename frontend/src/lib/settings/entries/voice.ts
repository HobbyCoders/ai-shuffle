/**
 * Voice: Dictation (global input, hotkey, mic mode, silence, cues, model link)
 * and Voice calls. VoicePage.svelte renders one `<SettingRow id>` per entry.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'voice.globalInput',
		category: 'voice',
		section: 'Dictation',
		label: 'Global voice input',
		keywords: ['dictation anywhere', 'speech to text', 'microphone'],
		default: false
	},
	{
		id: 'voice.hotkey',
		category: 'voice',
		section: 'Dictation',
		label: 'Voice input hotkey',
		keywords: ['shortcut', 'keyboard', 'dictation'],
		default: { key: 's', cmdOrCtrl: true, shift: true, alt: false }
	},
	{
		id: 'voice.micMode',
		category: 'voice',
		section: 'Dictation',
		label: 'Microphone mode',
		keywords: ['composer', 'auto', 'toggle', 'push to talk'],
		default: 'auto'
	},
	{
		id: 'voice.silence',
		category: 'voice',
		section: 'Dictation',
		label: 'Stop after silence',
		keywords: ['silence timeout', 'seconds', 'pause'],
		default: 5
	},
	{
		id: 'voice.cues',
		category: 'voice',
		section: 'Dictation',
		label: 'Sound cues',
		keywords: ['beep', 'tone', 'audio feedback'],
		default: true
	},
	{
		id: 'voice.dictationModel',
		category: 'voice',
		section: 'Dictation',
		label: 'Dictation model',
		hint: 'Chat mic and hotkey',
		keywords: ['transcription model', 'voice input', 'whisper', 'speech to text']
	},
	{
		id: 'voice.calls',
		category: 'voice',
		section: 'Voice calls',
		label: 'Voice calls',
		hint: 'Applies to new calls',
		keywords: ['enable voice mode', 'gpt live', 'realtime', 'call'],
		default: false
	},
	{
		id: 'voice.voice',
		category: 'voice',
		section: 'Voice calls',
		label: 'Voice',
		keywords: ['choose a voice', 'preview', 'sample', 'marin', 'cedar'],
		default: 'marin'
	},
	{
		id: 'voice.callsRunOn',
		category: 'voice',
		section: 'Voice calls',
		label: 'Calls run on',
		keywords: ['voice provider', 'credential', 'openai api key', 'billing']
	},
	{
		id: 'voice.maxCall',
		category: 'voice',
		section: 'Voice calls',
		label: 'Max call length',
		keywords: ['max call minutes', 'limit', 'duration'],
		default: 240
	},
	{
		id: 'voice.opening',
		category: 'voice',
		section: 'Voice calls',
		label: 'Opening words',
		hint: 'Blank waits for you',
		keywords: ['greeting', 'first words'],
		default: ''
	},
	{
		id: 'voice.prompts',
		category: 'voice',
		section: 'Voice calls',
		label: 'Call prompts',
		keywords: ['conversation prompt', 'chat agent prompt', 'instructions', 'restore default']
	}
];
