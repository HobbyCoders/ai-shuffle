/**
 * Media tool defaults (fal.ai image, video, 3D and audio tools): which model
 * each tool uses, its per-model options, and the save semantics.
 *
 * Ported from the old AIToolsSettings / ToolCategoryCard / ModelPickerGrid /
 * ModelBrowser quartet. The rules on a model change are unchanged:
 *   1. PATCH the tool's model endpoint.
 *   2. Clear the stored schema-driven params for that tool, so the new model
 *      starts from its own defaults.
 *   3. A new speech-to-text model drops the cached live-transcription answer.
 *   4. Reset every non-slider option (sliders keep their value).
 *   5. Text to speech takes the new model's first voice.
 * Every step after an `await` checks the session owner and that the store is
 * still alive, so nothing follows an account switch or an unmount.
 */
import { api, getErrorMessage } from '$lib/api/client';
import { loadState } from '$lib/settings/access.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';
import { invalidateSttCapability } from '$lib/services/sttStream';
import { sessionOwnerEpoch } from '$lib/stores/session-owner';
import { catalogStore } from '$lib/stores/catalog';
import { notify } from '$lib/stores/notifications';
import {
	AI_TOOL_CATEGORIES,
	getDefaultModelSelections,
	type AnyModel,
	type CapabilityBadge,
	type ToolDefinition,
	type ToolSetting
} from '$lib/types/ai-tool-registry';
import { ALL_TTS_MODELS, type CatalogModel, type TTSVoice } from '$lib/types/ai-models';

/** List group a tool sits in on the Default models page. */
export type ToolGroup = 'Image' | 'Video' | '3D' | 'Audio';

export interface ToolMeta {
	/** Collection item id (also used by deep links from other pages). */
	item: string;
	name: string;
	group: ToolGroup;
}

/** Registry tool id → list item. Item ids match the spec mockup so links agree across slices. */
export const TOOL_META: Record<string, ToolMeta> = {
	image_generation: { item: 't2i', name: 'Text to image', group: 'Image' },
	image_editing: { item: 'edit', name: 'Image editing', group: 'Image' },
	video_generation: { item: 't2v', name: 'Text to video', group: 'Video' },
	image_to_video: { item: 'i2v', name: 'Image to video', group: 'Video' },
	text_to_3d: { item: 't23d', name: 'Text to 3D', group: '3D' },
	image_to_3d: { item: 'i23d', name: 'Image to 3D', group: '3D' },
	tts: { item: 'tts', name: 'Text to speech', group: 'Audio' },
	stt: { item: 'stt', name: 'Speech to text', group: 'Audio' },
	text_to_audio: { item: 't2a', name: 'Text to audio', group: 'Audio' },
	audio_to_audio: { item: 'a2a', name: 'Audio to audio', group: 'Audio' }
};

export const ALL_TOOLS: readonly ToolDefinition[] = AI_TOOL_CATEGORIES.flatMap((c) => c.tools);

export function toolByItem(item: string | null | undefined): ToolDefinition | undefined {
	return ALL_TOOLS.find((t) => TOOL_META[t.id]?.item === item);
}

export function toolMeta(tool: ToolDefinition): ToolMeta {
	return TOOL_META[tool.id] ?? { item: tool.id, name: tool.label, group: 'Audio' };
}

/** Catalog tool types a pick can be saved to; the catalog's `generic` fallback is not one. */
export const SELECTABLE_TOOL_TYPES: readonly string[] = ALL_TOOLS.map((t) => t.catalogToolType);

/** Registry ids of the curated option rows, by tool id and setting key. */
export const OPTION_ENTRY: Record<string, Record<string, string>> = {
	image_generation: { aspect_ratio: 'models.imageAspect', resolution: 'models.imageResolution' },
	video_generation: {
		aspect_ratio: 'models.videoAspect',
		duration: 'models.videoDuration',
		resolution: 'models.videoResolution'
	},
	tts: { voice: 'models.ttsVoice', speed: 'models.ttsSpeed', output_format: 'models.ttsFormat' }
};

const SLIDER_DEBOUNCE_MS = 300;

type Values = Record<string, string | number>;

function freshValues(settings: Record<string, unknown>): Values {
	return {
		image_aspect_ratio: (settings.image_aspect_ratio as string) || '',
		image_resolution: (settings.image_resolution as string) || '',
		video_aspect_ratio: (settings.video_aspect_ratio as string) || '',
		video_duration: (settings.video_duration as number) || 0,
		video_resolution: (settings.video_resolution as string) || '',
		tts_voice: (settings.tts_voice as string) || 'af_heart',
		tts_speed: (settings.tts_speed as number) || 1.0,
		tts_output_format: (settings.tts_output_format as string) || 'wav'
	};
}

export class MediaTools {
	/** Model per tool, keyed by the backend setting (`image_model`...). */
	selections = $state<Record<string, string>>(getDefaultModelSelections());
	/** Per-model option values, keyed by the backend setting (`tts_voice`...). */
	values = $state<Values>({});
	status = $state<'loading' | 'ok' | 'forbidden' | 'error'>('loading');
	/** `fal_enabled` from GET /settings/integrations; null when the server does not send it. */
	falEnabled = $state<boolean | null>(null);
	/** Claude Code mode from the same response: `subscription`, `api_key` or `off`. */
	claudeMode = $state<string | null>(null);
	/** Tool id whose model change is in flight. */
	saving = $state<string | null>(null);
	/** Bumps when the shared fal.ai catalog changes. */
	catalogVersion = $state(0);

	#owner = sessionOwnerEpoch();
	#disposed = false;
	#loaded = false;
	#saved: Record<string, unknown> = {};
	#sliderTimer: ReturnType<typeof setTimeout> | null = null;
	#unsubCatalog: (() => void) | null = null;

	isCurrent = (): boolean => !this.#disposed && this.#owner === sessionOwnerEpoch();

	/** Load settings and the catalog; returns the cleanup. */
	start(): () => void {
		void this.load();
		void catalogStore.load().then(() => this.catalogVersion++);
		this.#unsubCatalog = catalogStore.subscribe(() => this.catalogVersion++);
		return () => this.dispose();
	}

	dispose(): void {
		this.#disposed = true;
		if (this.#sliderTimer) clearTimeout(this.#sliderTimer);
		this.#sliderTimer = null;
		this.#unsubCatalog?.();
		this.#unsubCatalog = null;
	}

	/**
	 * (Re)load from GET /settings/integrations. On a reload a value the user
	 * changed here and the server has not confirmed is kept.
	 */
	async load(): Promise<void> {
		const before: Record<string, unknown> = { ...this.selections, ...this.values };
		const wasLoaded = this.#loaded;
		const result = await loadState(() => api.get<Record<string, unknown>>('/settings/integrations'));
		if (!this.isCurrent()) return;
		if (result.status !== 'ok') {
			if (!wasLoaded) this.status = result.status;
			return;
		}
		const settings = result.data;
		const selections = { ...this.selections };
		for (const [key, defaultVal] of Object.entries(getDefaultModelSelections())) {
			const fresh = (settings[key] as string) || defaultVal;
			if (!wasLoaded || (selections[key] === before[key] && before[key] === this.#saved[key])) selections[key] = fresh;
			this.#saved[key] = fresh;
		}
		this.selections = selections;
		const values = { ...this.values };
		for (const [key, fresh] of Object.entries(freshValues(settings))) {
			if (!wasLoaded || (values[key] === before[key] && before[key] === this.#saved[key])) values[key] = fresh;
			this.#saved[key] = fresh;
		}
		this.values = values;
		this.falEnabled = typeof settings.fal_enabled === 'boolean' ? settings.fal_enabled : null;
		this.claudeMode = typeof settings.claude_auth_mode === 'string' ? settings.claude_auth_mode : null;
		this.#loaded = true;
		this.status = 'ok';
	}

	// ── Reads ────────────────────────────────────────────────────────────

	modelId(tool: ToolDefinition): string {
		return this.selections[tool.modelSettingKey] || tool.defaultModel;
	}

	/** The selected model when it is one of the tool's recommended (curated) models. */
	curated(tool: ToolDefinition): AnyModel | undefined {
		const id = this.modelId(tool);
		return tool.getModels().find((m) => m.id === id);
	}

	/** fal.ai catalog models for the tool (reactive on catalog changes). */
	catalogModels(tool: ToolDefinition): CatalogModel[] {
		void this.catalogVersion;
		return catalogStore.getModelsForTool(tool.catalogToolType);
	}

	/** Catalog models that are not already recommended. */
	moreModels(tool: ToolDefinition): CatalogModel[] {
		const curated = new Set(tool.getModels().map((m) => m.id));
		return this.catalogModels(tool).filter((m) => !curated.has(m.endpoint_id));
	}

	catalogModel(tool: ToolDefinition): CatalogModel | undefined {
		const id = this.modelId(tool);
		return this.catalogModels(tool).find((m) => m.endpoint_id === id);
	}

	/** Display name of the selected model; '' when none is chosen. */
	displayName(tool: ToolDefinition): string {
		const id = this.modelId(tool);
		if (!id) return '';
		return this.curated(tool)?.displayName ?? this.catalogModel(tool)?.display_name ?? id;
	}

	price(tool: ToolDefinition): string {
		const model = this.curated(tool);
		return model ? tool.formatPrice(model) : '';
	}

	capabilities(tool: ToolDefinition, model: AnyModel | undefined = this.curated(tool)): CapabilityBadge[] {
		if (!model || !tool.getCapabilities) return [];
		return tool.getCapabilities(model);
	}

	settingOptions(tool: ToolDefinition, setting: ToolSetting): string[] {
		const model = this.curated(tool);
		if (!model || !setting.getOptions) return [];
		return setting.getOptions(model);
	}

	voiceLabel(tool: ToolDefinition, voiceId: string): string {
		const model = this.curated(tool);
		if (!model) return voiceId;
		const tts = ALL_TTS_MODELS.find((m) => m.id === model.id);
		const voice = tts?.voices.find((v: TTSVoice) => v.id === voiceId);
		return voice ? `${voice.name}${voice.gender ? ` (${voice.gender})` : ''}` : voiceId;
	}

	/** What the selected model runs on: a fal.ai key, an OpenAI API key (speech
	 * models), or the ChatGPT subscription signed in on the worker (images). */
	runsOn(tool: ToolDefinition): 'fal' | 'openai' | 'chatgpt' {
		const id = this.modelId(tool);
		if (!id) return 'fal';
		const provider = (this.curated(tool) as { provider?: string } | undefined)?.provider;
		if (provider === 'openai' || provider === 'chatgpt') return provider;
		if (provider) return 'fal';
		// Uncurated `openai/` ids are OpenAI speech models only; an image id
		// such as `openai/gpt-image-2.5/flare/edit` is a fal.ai catalog model.
		const speech = tool.catalogToolType === 'tts' || tool.catalogToolType === 'stt';
		return speech && id.startsWith('openai/') ? 'openai' : 'fal';
	}

	/** True when the selected model is billed to fal.ai. */
	runsOnFal(tool: ToolDefinition): boolean {
		return this.runsOn(tool) === 'fal';
	}

	/** fal.ai is switched off and this tool needs it. */
	blockedByFal(tool: ToolDefinition): boolean {
		return this.falEnabled === false && !!this.modelId(tool) && this.runsOnFal(tool);
	}

	/** True when some tool uses this catalog model. */
	isDefault(endpointId: string): boolean {
		return ALL_TOOLS.some((t) => this.modelId(t) === endpointId);
	}

	toolForCatalogModel(model: CatalogModel): ToolDefinition | undefined {
		if (!model.tool_type || !SELECTABLE_TOOL_TYPES.includes(model.tool_type)) return undefined;
		return ALL_TOOLS.find((t) => t.catalogToolType === model.tool_type);
	}

	// ── Writes ───────────────────────────────────────────────────────────

	#set(key: string, value: string | number): void {
		if (key in this.selections) this.selections = { ...this.selections, [key]: String(value) };
		else this.values = { ...this.values, [key]: value };
	}

	/** Change a tool's model. Same id is a no-op. Resolves true when saved. */
	async selectModel(tool: ToolDefinition, modelId: string): Promise<boolean> {
		if (!this.isCurrent() || this.saving === tool.id || modelId === this.modelId(tool)) return false;
		this.saving = tool.id;
		try {
			await api.patch<{ success: boolean }>(tool.modelEndpoint, { model: modelId });
			if (!this.isCurrent()) return false;

			// Clear stored schema-driven params so the new model starts from its
			// own defaults rather than inheriting the previous model's values.
			try {
				await api.patch('/settings/integrations/model-params', { key: tool.modelSettingKey, params: {} });
			} catch {
				// Non-critical
			}
			if (!this.isCurrent()) return false;

			// The live-transcription probe caches whether the current STT model can
			// stream; a model swap invalidates that answer immediately.
			if (tool.modelSettingKey === 'stt_model') invalidateSttCapability();

			this.#set(tool.modelSettingKey, modelId);

			// Reset per-model settings (sliders keep their value).
			for (const setting of tool.settings) {
				if (setting.type !== 'slider') this.#set(setting.settingKey, '');
			}
			if (tool.settingsEndpoint && tool.settings.length > 0) {
				const reset: Values = {};
				for (const setting of tool.settings) {
					if (setting.type === 'slider') continue;
					reset[setting.key] = '';
				}
				if (Object.keys(reset).length > 0) {
					try {
						await api.patch(tool.settingsEndpoint, reset);
					} catch {
						// Non-critical: settings reset failed
					}
				}
			}
			if (!this.isCurrent()) return false;

			// Text to speech: take the new model's first voice.
			if (tool.id === 'tts') {
				const next = ALL_TTS_MODELS.find((m) => m.id === modelId);
				if (next?.voices.length) {
					const first = next.voices[0].id;
					this.#set('tts_voice', first);
					try {
						await api.patch('/settings/integrations/tts', { voice: first });
					} catch {
						// Non-critical
					}
				}
			}
			if (!this.isCurrent()) return false;
			flashSaved();
			return true;
		} catch (e: unknown) {
			if (this.isCurrent()) notify.error(getErrorMessage(e, 'Could not change the model'));
			return false;
		} finally {
			if (this.isCurrent()) this.saving = null;
		}
	}

	/** Pick a model from the fal.ai catalog: it becomes the default of its tool. */
	async selectFromCatalog(model: CatalogModel): Promise<boolean> {
		const tool = this.toolForCatalogModel(model);
		if (!tool) return false;
		return this.selectModel(tool, model.endpoint_id);
	}

	/** Pills and selects: save at once. */
	async setOption(tool: ToolDefinition, setting: ToolSetting, value: string): Promise<void> {
		if (!this.isCurrent()) return;
		if (value === this.values[setting.settingKey]) return;
		this.#set(setting.settingKey, value);
		if (!tool.settingsEndpoint) return;
		try {
			await api.patch(tool.settingsEndpoint, { [setting.key]: value });
			if (this.isCurrent()) flashSaved();
		} catch (e: unknown) {
			if (this.isCurrent()) notify.error(getErrorMessage(e, 'Could not save'));
		}
	}

	/** Sliders: update now, save after a short pause. */
	setSlider(tool: ToolDefinition, setting: ToolSetting, value: number): void {
		if (!this.isCurrent()) return;
		this.#set(setting.settingKey, value);
		if (this.#sliderTimer) clearTimeout(this.#sliderTimer);
		this.#sliderTimer = setTimeout(async () => {
			this.#sliderTimer = null;
			if (!this.isCurrent() || !tool.settingsEndpoint) return;
			try {
				await api.patch(tool.settingsEndpoint, { [setting.key]: value });
				if (this.isCurrent()) flashSaved();
			} catch (e: unknown) {
				if (this.isCurrent()) notify.error(getErrorMessage(e, 'Could not save'));
			}
		}, SLIDER_DEBOUNCE_MS);
	}
}
