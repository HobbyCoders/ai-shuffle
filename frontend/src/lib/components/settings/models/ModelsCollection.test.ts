import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { CatalogModel } from '$lib/types/ai-models';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	patch: vi.fn(),
	put: vi.fn(),
	post: vi.fn(),
	invalidate: vi.fn(),
	loadVoice: vi.fn(),
	saveVoice: vi.fn()
}));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, patch: mocks.patch, put: mocks.put, post: mocks.post },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
vi.mock('$lib/services/sttStream', () => ({ invalidateSttCapability: mocks.invalidate }));
vi.mock('$lib/api/voice', () => ({ loadVoiceSettings: mocks.loadVoice, saveVoiceSettings: mocks.saveVoice }));

import { catalogStore } from '$lib/stores/catalog';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import ModelsCollection from './ModelsCollection.svelte';

const model = (endpoint_id: string, over: Partial<CatalogModel> = {}): CatalogModel => ({
	endpoint_id,
	display_name: endpoint_id,
	category: 'text-to-image',
	description: '',
	status: 'active',
	tags: [],
	tool_type: 'image_generation',
	output_type: 'image',
	...over
});
const catalog = [
	model('fal-ai/flux-pro/v1.1-ultra', { display_name: 'FLUX1.1 [pro] ultra' }),
	model('fal-ai/hidream', { display_name: 'HiDream', description: 'Sharp photoreal images', tags: ['photo'] }),
	model('fal-ai/ltx-2', { display_name: 'LTX-2', category: 'text-to-video', tool_type: 'video_generation', output_type: 'video' }),
	model('fal-ai/birefnet', { display_name: 'BiRefNet', category: 'image-to-image', tool_type: 'generic', tags: ['Lofi'] })
];
const categories = [
	{ category: 'text-to-image', count: 2, tool_type: 'image_generation' },
	{ category: 'text-to-video', count: 1, tool_type: 'video_generation' },
	{ category: 'image-to-image', count: 1, tool_type: 'generic' }
];
let integrations: Record<string, unknown>;
let textGen: { text_generation_model: string; text_generation_effort: string };
const voiceSettings = {
	voice_mode_enabled: true,
	voice_model: 'gpt-live-1',
	voice_voice: 'marin',
	voice_provider_id: null,
	voice_max_call_minutes: 240,
	voice_app_tools_enabled: false,
	voice_input_model: 'gpt-4o-transcribe',
	voice_input_models: [
		{ id: 'gpt-4o-transcribe', name: 'GPT-4o Transcribe', provider: 'openai' },
		{ id: 'fal-ai/whisper', name: 'Whisper (Large v3)', provider: 'fal' }
	],
	model_details: { model: 'gpt-live-1', voices: [{ id: 'marin', label: 'Marin' }] }
};

beforeEach(async () => {
	vi.clearAllMocks();
	settingsNav.reset();
	settingsNav.selectCategory('models');
	integrations = {
		image_model: 'fal-ai/flux-pro/v1.1-ultra',
		tts_model: 'fal-ai/kokoro/american-english',
		tts_voice: 'af_heart',
		tts_speed: 1,
		tts_output_format: 'wav',
		stt_model: 'fal-ai/whisper',
		fal_enabled: true,
		claude_auth_mode: 'subscription'
	};
	textGen = { text_generation_model: 'sonnet', text_generation_effort: 'low' };
	mocks.get.mockImplementation(async (url: string) => {
		if (url === '/settings/integrations') return integrations;
		if (url === '/settings/text-generation') return textGen;
		if (url === '/catalog/categories') return { categories, total_models: 4 };
		if (url.startsWith('/catalog/models')) return { models: catalog, total: 4, catalog_size: 4 };
		if (url.startsWith('/settings/integrations/model-params')) return { params: {} };
		if (url.startsWith('/catalog/schema')) return { found: false, endpoint_id: '' };
		return {};
	});
	mocks.patch.mockResolvedValue({ success: true });
	mocks.put.mockImplementation(async (_url: string, body: Record<string, string>) => ({ ...textGen, ...body }));
	mocks.loadVoice.mockResolvedValue(voiceSettings);
	mocks.saveVoice.mockImplementation(async (patch: Record<string, unknown>) => ({ ...voiceSettings, ...patch }));
	await catalogStore.load(true);
});
afterEach(cleanup);

const item = (name: string) => screen.getByRole('button', { name: new RegExp(`^${name}`) });
async function open(name: string) {
	render(ModelsCollection);
	await waitFor(() => expect(item('Text tasks').textContent).toContain('Sonnet'));
	await fireEvent.click(item(name));
}

describe('ModelsCollection', () => {
	it('lists every task by group with its model, and a hollow dot only when none is chosen', async () => {
		render(ModelsCollection);
		expect(screen.getByText('13 tasks')).toBeTruthy();
		const labels = [...document.querySelectorAll('.st-lg > span:first-child')].map((e) => e.textContent);
		expect(labels).toEqual(['Text', 'Image', 'Video', '3D', 'Audio', 'Catalog']);
		await waitFor(() => expect(item('Text tasks').textContent).toContain('Sonnet, low effort'));
		expect(item('Text to image').textContent).toContain('Flux Pro Ultra');
		await waitFor(() => expect(item('Dictation').textContent).toContain('GPT-4o Transcribe'));
		expect(item('Voice calls').textContent).toContain('gpt-live-1, fixed');
		expect(item('All fal.ai models').textContent).toContain('4 models');
		const a2a = item('Audio to audio');
		expect(a2a.textContent).toContain('No model chosen');
		expect(a2a.querySelector('.st-dot.off')).toBeTruthy();
		expect(document.querySelectorAll('[data-settings-list] .st-dot.off')).toHaveLength(1);
	});

	it('filters tasks by name, model and group', async () => {
		render(ModelsCollection);
		await fireEvent.input(screen.getByPlaceholderText('Filter default models'), { target: { value: 'speech' } });
		const names = [...document.querySelectorAll('[data-settings-item]')].map((e) => e.getAttribute('data-settings-item'));
		expect(names).toEqual(['tts', 'stt']);
	});

	it('reaches the text-to-speech voice in two clicks and saves it', async () => {
		await open('Text to speech');
		const detail = screen.getByRole('region', { name: 'Details' });
		expect(within(detail).getByRole('heading', { level: 2, name: 'Text to speech' })).toBeTruthy();
		const voice = within(detail).getByRole('combobox', { name: 'Voice' }) as HTMLSelectElement;
		expect(voice.value).toBe('af_heart');
		expect(voice.selectedOptions[0].textContent).toBe('Heart (female)');
		await fireEvent.change(voice, { target: { value: 'af_bella' } });
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/tts', { voice: 'af_bella' });
		expect(within(detail).getByRole('slider', { name: 'Speed' })).toBeTruthy();
		expect(within(detail).getByRole('combobox', { name: 'Output format' })).toBeTruthy();
		expect(detail.querySelector('[data-setting-anchor="models.ttsVoice"]')).toBeTruthy();
	});

	it('changes a model from the Model tab and resets its options', async () => {
		await open('Text to speech');
		await fireEvent.click(screen.getByRole('tab', { name: 'Model' }));
		const minimax = screen.getByRole('button', { name: /MiniMax Speech 2\.8 HD/ });
		await fireEvent.click(minimax);
		await waitFor(() => expect(minimax.getAttribute('aria-pressed')).toBe('true'));
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/audio/tts', { model: 'fal-ai/minimax/speech-2.8-hd' });
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/model-params', { key: 'tts_model', params: {} });
		expect(mocks.patch).toHaveBeenLastCalledWith('/settings/integrations/tts', { voice: 'Wise_Woman' });
		await waitFor(() => expect(item('Text to speech').textContent).toContain('MiniMax Speech 2.8 HD'));
	});

	it('offers catalog models under the recommended ones and links the full catalog', async () => {
		await open('Text to image');
		await fireEvent.click(screen.getByRole('tab', { name: 'Model' }));
		expect(screen.getByRole('heading', { name: /Recommended/ })).toBeTruthy();
		expect(screen.getByRole('heading', { name: /More from fal\.ai/ })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: /HiDream/ }));
		await waitFor(() => expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/image/model', { model: 'fal-ai/hidream' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Full catalog' }));
		expect(settingsNav.selectedItem('models')).toBe('catalog');
	});

	it('asks for a model when a tool has none', async () => {
		await open('Audio to audio');
		expect(screen.getAllByText('No model chosen').length).toBeGreaterThan(0);
		await fireEvent.click(screen.getByRole('button', { name: 'Choose model' }));
		expect(screen.getByRole('tab', { name: 'Model' }).getAttribute('aria-selected')).toBe('true');
	});

	it('warns on a tool when fal.ai is off', async () => {
		integrations = { ...integrations, fal_enabled: false };
		await open('Text to image');
		expect(await screen.findByText('fal.ai is off, so this tool cannot run.')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'fal.ai media key' })).toBeTruthy();
	});

	it('saves the text model effort and warns when Claude is off', async () => {
		await open('Text tasks');
		expect(screen.queryByText(/Claude is off\./)).toBeNull();
		const effort = screen.getByRole('combobox', { name: 'Effort' }) as HTMLSelectElement;
		expect(effort.value).toBe('low');
		await fireEvent.change(effort, { target: { value: 'high' } });
		expect(mocks.put).toHaveBeenCalledWith('/settings/text-generation', { text_generation_effort: 'high' });
		cleanup();
		integrations = { ...integrations, claude_auth_mode: 'off' };
		await open('Text tasks');
		expect(await screen.findByText('Claude is off. Pick another model.')).toBeTruthy();
	});

	it('does not warn about Claude for a model from another provider', async () => {
		integrations = { ...integrations, claude_auth_mode: 'off' };
		textGen = { text_generation_model: 'prov:p1:gpt-6.1', text_generation_effort: '' };
		render(ModelsCollection);
		await waitFor(() => expect(item('Text tasks').textContent).toContain('gpt-6.1, default effort'));
		await fireEvent.click(item('Text tasks'));
		expect(screen.queryByText('Claude is off. Pick another model.')).toBeNull();
	});

	it('saves the dictation model through the voice settings', async () => {
		await open('Dictation');
		const select = await screen.findByRole('combobox', { name: 'Model' });
		await fireEvent.change(select, { target: { value: 'fal-ai/whisper' } });
		expect(mocks.saveVoice).toHaveBeenCalledWith({ voice_input_model: 'fal-ai/whisper' });
	});

	it('shows voice calls read-only', async () => {
		await open('Voice calls');
		const detail = screen.getByRole('region', { name: 'Details' });
		expect(within(detail).getAllByText('gpt-live-1').length).toBeGreaterThan(0);
		await waitFor(() => expect(within(detail).getByText('240 min')).toBeTruthy());
		expect(within(detail).queryByRole('combobox')).toBeNull();
	});
});

describe('CatalogDetail', () => {
	const rows = () => [...document.querySelectorAll<HTMLElement>('[data-model-id]')].map((b) => b.dataset.modelId);

	it('filters by category chip (toggling it off again) and by search across name, description, id and tags', async () => {
		await open('All fal.ai models');
		expect(rows()).toHaveLength(4);
		const video = screen.getByRole('button', { name: /Text → Video 1/ });
		expect(screen.getByRole('button', { name: /^All 4/ }).getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(video);
		expect(video.getAttribute('aria-pressed')).toBe('true');
		expect(rows()).toEqual(['fal-ai/ltx-2']);
		await fireEvent.click(video);
		expect(rows()).toHaveLength(4);
		const search = screen.getByRole('searchbox', { name: 'Search catalog' });
		for (const [q, expected] of [
			['photoreal', ['fal-ai/hidream']],
			['LTX-2', ['fal-ai/ltx-2']],
			['lofi', ['fal-ai/birefnet']]
		] as const) {
			await fireEvent.input(search, { target: { value: q } });
			expect(rows()).toEqual(expected);
		}
		await fireEvent.input(search, { target: { value: 'nothing-like-this' } });
		expect(screen.getByText(/No models match/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));
		expect(rows()).toHaveLength(4);
	});

	it('marks defaults, greys out models no tool uses, and sets a pick as its tool default', async () => {
		await open('All fal.ai models');
		const ultra = document.querySelector<HTMLElement>('[data-model-id="fal-ai/flux-pro/v1.1-ultra"]')!;
		expect(ultra.getAttribute('aria-pressed')).toBe('true');
		expect(ultra.textContent).toContain('Default');
		const generic = document.querySelector<HTMLElement>('[data-model-id="fal-ai/birefnet"]')!;
		expect(generic.getAttribute('aria-disabled')).toBe('true');
		expect(generic.textContent).toContain('No tool uses this');
		await fireEvent.click(generic);
		expect(mocks.patch).not.toHaveBeenCalled();
		const ltx = document.querySelector<HTMLElement>('[data-model-id="fal-ai/ltx-2"]')!;
		await fireEvent.click(ltx);
		await waitFor(() => expect(ltx.getAttribute('aria-pressed')).toBe('true'));
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/video/model', { model: 'fal-ai/ltx-2' });
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/model-params', { key: 'video_model', params: {} });
	});

	it('refreshes from fal.ai', async () => {
		mocks.post.mockResolvedValue({ success: true, models_fetched: 9, categories });
		await open('All fal.ai models');
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		expect(mocks.post).toHaveBeenCalledWith('/catalog/refresh', {});
		await waitFor(() => expect(screen.getByPlaceholderText('Search 9 models')).toBeTruthy());
	});

	it('shows a load failure with a working retry', async () => {
		await open('All fal.ai models');
		const get = mocks.get.getMockImplementation()!;
		mocks.get.mockImplementation(async (url: string) => {
			if (url.startsWith('/catalog/models')) throw new Error('catalog offline');
			return get(url);
		});
		await catalogStore.load(true).catch(() => {});
		expect(await screen.findByText('catalog offline')).toBeTruthy();
		mocks.get.mockImplementation(get);
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await waitFor(() => expect(screen.queryByText('catalog offline')).toBeNull());
		expect(rows()).toHaveLength(4);
	});
});

describe('Default models registry coverage', () => {
	it('renders a row for every entry where the entry says it lives', async () => {
		await assertCategoryCoverage(
			'models',
			async (where) => {
				settingsNav.reset();
				settingsNav.go({ category: 'models', ...where });
				return render(ModelsCollection).container;
			},
			{ settle: () => new Promise((r) => setTimeout(r, 20)) }
		);
	});
});
