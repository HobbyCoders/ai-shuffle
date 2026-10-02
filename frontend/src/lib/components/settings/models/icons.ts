/** Lucide icon per Default models task. */
import {
	AudioWaveform,
	Box,
	Clapperboard,
	Cuboid,
	FileAudio,
	Film,
	Image,
	Library,
	Mic,
	Music,
	Phone,
	Speech,
	Text,
	WandSparkles
} from 'lucide-svelte';
import type { SettingsIcon } from '$lib/settings/categories';

/** By registry tool id. */
export const TOOL_ICONS: Record<string, SettingsIcon> = {
	image_generation: Image,
	image_editing: WandSparkles,
	video_generation: Film,
	image_to_video: Clapperboard,
	text_to_3d: Box,
	image_to_3d: Cuboid,
	tts: Speech,
	stt: FileAudio,
	text_to_audio: Music,
	audio_to_audio: AudioWaveform
};

export const TEXT_ICON: SettingsIcon = Text;
export const DICTATION_ICON: SettingsIcon = Mic;
export const VOICE_CALL_ICON: SettingsIcon = Phone;
export const CATALOG_ICON: SettingsIcon = Library;
