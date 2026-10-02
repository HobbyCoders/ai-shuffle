/**
 * Default models that are not fal.ai media tools: Text tasks (the one model +
 * effort behind every internal one-shot call), Dictation (`voice_input_model`)
 * and Voice calls (read-only, fixed to GPT Live).
 */
import { getContext, setContext } from 'svelte';
import { loadState } from '$lib/settings/access.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';
import { sessionOwnerEpoch } from '$lib/stores/session-owner';
import { notify } from '$lib/stores/notifications';
import { loadTextGenerationSettings, saveTextGenerationSettings } from '$lib/api/text-generation';
import { loadVoiceSettings, saveVoiceSettings, type VoiceSettings } from '$lib/api/voice';
import {
	decodeProviderModel,
	getClaudeAuth,
	getDefaultModel,
	getModelLabel,
	getProviderDisplay,
	getProviders,
	isPresetModel
} from '$lib/constants/models';
import { EFFORT_OPTIONS, modelEffortOptions, reconcileModelEffort, resolveModelEffort } from '$lib/constants/model-effort';
import type { MediaTools } from './media-tools.svelte';

type LoadStatus = 'loading' | 'ok' | 'forbidden' | 'error';

/** What text generation is used for (shown as chips). */
export const TEXT_TASK_USES = ['Session titles', 'Commit messages', 'PR descriptions', 'Branch names', 'Agent generation'];

export class TextTasks {
	/** Raw setting: '' means the registry default. */
	model = $state('');
	effort = $state('');
	saving = $state(false);
	status = $state<LoadStatus>('loading');

	#owner = sessionOwnerEpoch();
	#disposed = false;
	#tools: MediaTools | null;

	constructor(tools: MediaTools | null = null) {
		this.#tools = tools;
	}

	isCurrent = (): boolean => !this.#disposed && this.#owner === sessionOwnerEpoch();

	dispose(): void {
		this.#disposed = true;
	}

	/** The model shown: an unset setting resolves to the registry default at call time. */
	get value(): string {
		return this.model || getDefaultModel();
	}

	get label(): string {
		return getModelLabel(this.value);
	}

	get isClaude(): boolean {
		return !decodeProviderModel(this.value) && isPresetModel(this.value);
	}

	get providerName(): string {
		const composite = decodeProviderModel(this.value);
		if (!composite) return this.isClaude ? 'Claude' : '';
		const provider = getProviders().find((p) => p.id === composite.providerId);
		return provider ? getProviderDisplay(provider).name : '';
	}

	/** Claude Code mode is Off on this worker (from the server or the model registry). */
	get claudeOff(): boolean {
		if (this.#tools?.claudeMode === 'off') return true;
		return (getClaudeAuth() as { enabled?: boolean }).enabled === false;
	}

	/** Text tasks would run on Claude while Claude is off. */
	get blocked(): boolean {
		return this.isClaude && this.claudeOff;
	}

	get effortOptions(): { value: string; label: string }[] {
		return [{ value: '', label: 'Provider default' }, ...modelEffortOptions(this.value, this.effort)];
	}

	get effortLabel(): string {
		if (!this.effort) return 'Provider default';
		return EFFORT_OPTIONS.find((o) => o.value === this.effort)?.label ?? this.effort;
	}

	async load(): Promise<void> {
		const result = await loadState(loadTextGenerationSettings);
		if (!this.isCurrent()) return;
		if (result.status === 'ok') {
			this.model = result.data.text_generation_model;
			this.effort = result.data.text_generation_effort;
			this.status = 'ok';
		} else if (this.status === 'loading') {
			this.status = result.status;
		}
	}

	/** Remote change: adopt it unless a save is running or the value moved meanwhile. */
	async refresh(): Promise<void> {
		const before = JSON.stringify([this.model, this.effort]);
		const result = await loadState(loadTextGenerationSettings);
		if (!this.isCurrent() || result.status !== 'ok') return;
		if (!this.saving && before === JSON.stringify([this.model, this.effort])) {
			this.model = result.data.text_generation_model;
			this.effort = result.data.text_generation_effort;
		}
	}

	/** A new model keeps the effort only when the model still supports it. */
	setModel(value: string): Promise<void> {
		return this.save({
			text_generation_model: value,
			text_generation_effort: reconcileModelEffort(this.effort || null, resolveModelEffort(value)) ?? ''
		});
	}

	setEffort(value: string): Promise<void> {
		return this.save({ text_generation_effort: value });
	}

	async save(patch: { text_generation_model?: string; text_generation_effort?: string }): Promise<void> {
		const previous = { model: this.model, effort: this.effort };
		if (patch.text_generation_model !== undefined) this.model = patch.text_generation_model;
		if (patch.text_generation_effort !== undefined) this.effort = patch.text_generation_effort;
		this.saving = true;
		try {
			const settled = await saveTextGenerationSettings(patch);
			if (!this.isCurrent()) return;
			this.model = settled.text_generation_model;
			this.effort = settled.text_generation_effort;
			flashSaved();
		} catch (e) {
			if (!this.isCurrent()) return;
			// Roll back so the controls never claim a binding the server refused.
			this.model = previous.model;
			this.effort = previous.effort;
			notify.error(e instanceof Error ? e.message : 'Could not save the text model');
		} finally {
			if (this.isCurrent()) this.saving = false;
		}
	}
}

/** Default dictation model when none is stored (server default). */
export const DEFAULT_DICTATION_MODEL = 'fal-ai/whisper';
export const VOICE_CALL_MODEL = 'gpt-live-1';

const PROVIDER_LABELS: Record<string, string> = { openai: 'OpenAI', fal: 'fal.ai', 'fal-ai': 'fal.ai' };

export function providerLabel(provider: string): string {
	return PROVIDER_LABELS[provider] ?? provider;
}

export class VoiceDefaults {
	settings = $state<VoiceSettings | null>(null);
	status = $state<LoadStatus>('loading');
	saving = $state(false);

	#owner = sessionOwnerEpoch();
	#disposed = false;

	isCurrent = (): boolean => !this.#disposed && this.#owner === sessionOwnerEpoch();

	dispose(): void {
		this.#disposed = true;
	}

	async load(force = false): Promise<void> {
		const result = await loadState(() => loadVoiceSettings(force));
		if (!this.isCurrent()) return;
		if (result.status === 'ok') {
			if (!this.saving) this.settings = result.data;
			this.status = 'ok';
		} else if (this.status === 'loading') {
			this.status = result.status;
		}
	}

	get inputModel(): string {
		return this.settings?.voice_input_model || DEFAULT_DICTATION_MODEL;
	}

	get inputModels(): { id: string; name: string; provider: string }[] {
		return this.settings?.voice_input_models ?? [];
	}

	inputModelName(id: string = this.inputModel): string {
		return this.inputModels.find((m) => m.id === id)?.name ?? id;
	}

	/** The dictation model is billed to fal.ai. */
	get inputOnFal(): boolean {
		const provider = this.inputModels.find((m) => m.id === this.inputModel)?.provider;
		return provider ? provider === 'fal' || provider === 'fal-ai' : this.inputModel.startsWith('fal-ai/');
	}

	inputModelProvider(id: string = this.inputModel): string {
		const provider = this.inputModels.find((m) => m.id === id)?.provider;
		return provider ? providerLabel(provider) : '';
	}

	/** Grouped by provider, in server order. */
	get inputModelGroups(): { label: string; options: { value: string; label: string }[] }[] {
		const groups: { label: string; options: { value: string; label: string }[] }[] = [];
		for (const m of this.inputModels) {
			const label = providerLabel(m.provider);
			let group = groups.find((g) => g.label === label);
			if (!group) groups.push((group = { label, options: [] }));
			group.options.push({ value: m.id, label: m.name });
		}
		if (!this.inputModels.some((m) => m.id === this.inputModel)) {
			groups.unshift({ label: '', options: [{ value: this.inputModel, label: this.inputModel }] });
		}
		return groups;
	}

	/** Saving also drops the cached live-transcription answer (see saveVoiceSettings). */
	async setInputModel(id: string): Promise<void> {
		if (!this.settings || id === this.inputModel) return;
		const previous = this.settings;
		this.settings = { ...previous, voice_input_model: id };
		this.saving = true;
		try {
			const updated = await saveVoiceSettings({ voice_input_model: id });
			if (!this.isCurrent()) return;
			this.settings = updated;
			flashSaved();
		} catch (e) {
			if (!this.isCurrent()) return;
			this.settings = previous;
			notify.error(e instanceof Error ? e.message : 'Could not save the dictation model');
		} finally {
			if (this.isCurrent()) this.saving = false;
		}
	}

	get callVoiceLabel(): string {
		const id = this.settings?.voice_voice ?? '';
		const label = this.settings?.model_details?.voices.find((v) => v.id === id)?.label;
		return label ?? (id ? id.charAt(0).toUpperCase() + id.slice(1) : '');
	}

	/** Name of the pinned voice provider, or the automatic choice. */
	get callProviderName(): string {
		const id = this.settings?.voice_provider_id;
		if (!id) return 'OpenAI API key';
		return getProviders().find((p) => p.id === id)?.name ?? 'OpenAI API key';
	}
}

/** Everything the Default models collection shares between its list and details. */
export interface ModelsState {
	tools: MediaTools;
	text: TextTasks;
	voice: VoiceDefaults;
}

const KEY = Symbol('settings-models');

export function setModelsState(state: ModelsState): ModelsState {
	return setContext(KEY, state);
}

export function getModelsState(): ModelsState {
	const state = getContext<ModelsState | undefined>(KEY);
	if (!state) throw new Error('Default models state is missing (render inside ModelsCollection)');
	return state;
}
