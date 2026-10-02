/**
 * Save semantics of the media tool defaults. Ported from the old
 * AIToolsSettings, ModelBrowser and setting-editor-ownership tests.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CatalogModel } from '$lib/types/ai-models';

const mocks = vi.hoisted(() => ({ owner: 0, get: vi.fn(), patch: vi.fn(), post: vi.fn(), invalidate: vi.fn(), error: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, patch: mocks.patch, post: mocks.post },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
vi.mock('$lib/stores/session-owner', () => ({ sessionOwnerEpoch: () => mocks.owner }));
vi.mock('$lib/services/sttStream', () => ({ invalidateSttCapability: mocks.invalidate }));
vi.mock('$lib/stores/notifications', () => ({ notify: { error: mocks.error } }));

import { catalogStore } from '$lib/stores/catalog';
import { ALL_TOOLS, MediaTools, toolByItem, TOOL_META } from './media-tools.svelte';

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
	model('fal-ai/new-image', { display_name: 'New Image' }),
	model('fal-ai/wizper', { display_name: 'Wizper', category: 'speech-to-text', tool_type: 'stt', output_type: 'text' }),
	model('fal-ai/image-to-text', { display_name: 'Captioner', category: 'image-to-text', tool_type: 'generic', output_type: null })
];
let settings: Record<string, unknown>;
const tool = (item: string) => toolByItem(item)!;
const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(async () => {
	mocks.owner = 0;
	vi.clearAllMocks();
	settings = { image_model: 'fal-ai/flux/dev', tts_voice: 'am_adam', tts_speed: 1.25, fal_enabled: true, claude_auth_mode: 'subscription' };
	mocks.get.mockImplementation(async (url: string) => {
		if (url === '/settings/integrations') return settings;
		if (url === '/catalog/categories') return { categories: [{ category: 'text-to-image', count: 1, tool_type: 'image_generation' }], total_models: 3 };
		if (url.startsWith('/catalog/models')) return { models: catalog, total: 3, catalog_size: 3 };
		return {};
	});
	mocks.patch.mockResolvedValue({ success: true });
	await catalogStore.load(true);
});
afterEach(() => vi.useRealTimers());

async function loaded(): Promise<MediaTools> {
	const tools = new MediaTools();
	await tools.load();
	return tools;
}

describe('MediaTools', () => {
	it('maps every registry tool to a list item', () => {
		expect(ALL_TOOLS.map((t) => TOOL_META[t.id]?.item)).toEqual(['t2i', 'edit', 't2v', 'i2v', 't23d', 'i23d', 'tts', 'stt', 't2a', 'a2a']);
	});

	it('loads saved selections over the registry defaults, plus the fal.ai and Claude flags', async () => {
		const tools = await loaded();
		expect(tools.status).toBe('ok');
		expect(tools.displayName(tool('t2i'))).toBe('Flux Dev');
		expect(tools.displayName(tool('edit'))).toBe('Flux Kontext Pro');
		expect(tools.modelId(tool('a2a'))).toBe('');
		expect(tools.displayName(tool('a2a'))).toBe('');
		expect(tools.values.tts_voice).toBe('am_adam');
		expect(tools.falEnabled).toBe(true);
		expect(tools.claudeMode).toBe('subscription');
	});

	it('keeps a local change the server has not confirmed when settings reload', async () => {
		const tools = await loaded();
		await tools.setOption(tool('tts'), tool('tts').settings[0], 'af_bella');
		mocks.patch.mockClear();
		settings = { ...settings, image_model: 'fal-ai/flux-pro/kontext', tts_voice: 'am_adam' };
		await tools.load();
		expect(tools.values.tts_voice).toBe('af_bella');
		expect(tools.modelId(tool('t2i'))).toBe('fal-ai/flux-pro/kontext');
	});

	it('changes a model: PATCH, clears its params, resets non-slider options and takes the first TTS voice', async () => {
		const tools = await loaded();
		const tts = tool('tts');
		expect(await tools.selectModel(tts, 'fal-ai/minimax/speech-2.8-hd')).toBe(true);
		expect(mocks.patch.mock.calls).toEqual([
			['/settings/integrations/audio/tts', { model: 'fal-ai/minimax/speech-2.8-hd' }],
			['/settings/integrations/model-params', { key: 'tts_model', params: {} }],
			['/settings/integrations/tts', { voice: '', output_format: '' }],
			['/settings/integrations/tts', { voice: 'Wise_Woman' }]
		]);
		expect(tools.modelId(tts)).toBe('fal-ai/minimax/speech-2.8-hd');
		expect(tools.values.tts_voice).toBe('Wise_Woman');
		expect(tools.values.tts_output_format).toBe('');
		// The speed slider keeps its value.
		expect(tools.values.tts_speed).toBe(1.25);
		expect(mocks.invalidate).not.toHaveBeenCalled();
		expect(tools.saving).toBeNull();
	});

	it('ignores picking the model that is already selected', async () => {
		const tools = await loaded();
		expect(await tools.selectModel(tool('t2i'), 'fal-ai/flux/dev')).toBe(false);
		expect(mocks.patch).not.toHaveBeenCalled();
	});

	it('persists a catalog pick to its tool and marks it default', async () => {
		const tools = await loaded();
		expect(tools.isDefault('fal-ai/new-image')).toBe(false);
		await tools.selectFromCatalog(catalog[0]);
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/image/model', { model: 'fal-ai/new-image' });
		expect(tools.isDefault('fal-ai/new-image')).toBe(true);
		expect(tools.displayName(tool('t2i'))).toBe('New Image');
		expect(mocks.invalidate).not.toHaveBeenCalled();
	});

	it('drops the cached speech-to-text capability when the STT model changes', async () => {
		const tools = await loaded();
		await tools.selectFromCatalog(catalog[1]);
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/audio/stt', { model: 'fal-ai/wizper' });
		expect(mocks.invalidate).toHaveBeenCalledOnce();
	});

	it('keeps the previous default when saving the pick fails', async () => {
		const tools = await loaded();
		mocks.patch.mockRejectedValueOnce(new Error('no key'));
		expect(await tools.selectFromCatalog(catalog[0])).toBe(false);
		expect(mocks.patch).toHaveBeenCalledOnce();
		expect(mocks.error).toHaveBeenCalledWith('no key');
		expect(tools.modelId(tool('t2i'))).toBe('fal-ai/flux/dev');
		expect(tools.saving).toBeNull();
	});

	it('refuses a model no tool uses (the catalog generic fallback)', async () => {
		const tools = await loaded();
		expect(tools.toolForCatalogModel(catalog[2])).toBeUndefined();
		expect(await tools.selectFromCatalog(catalog[2])).toBe(false);
		expect(mocks.patch).not.toHaveBeenCalled();
		expect(tools.toolForCatalogModel(catalog[1])?.id).toBe('stt');
	});

	it('lists catalog models per tool without the recommended ones', async () => {
		const tools = await loaded();
		expect(tools.moreModels(tool('t2i')).map((m) => m.endpoint_id)).toEqual(['fal-ai/new-image']);
		expect(tools.moreModels(tool('t2v'))).toEqual([]);
	});

	it('flags tools blocked by fal.ai being off, but not OpenAI speech models', async () => {
		settings = { ...settings, fal_enabled: false, tts_model: 'openai/gpt-4o-mini-tts' };
		const tools = await loaded();
		expect(tools.blockedByFal(tool('t2i'))).toBe(true);
		expect(tools.blockedByFal(tool('a2a'))).toBe(false);
		expect(tools.runsOnFal(tool('tts'))).toBe(false);
		expect(tools.blockedByFal(tool('tts'))).toBe(false);
	});

	it('saves options at once and sliders after a pause', async () => {
		const tools = await loaded();
		const t2i = tool('t2i');
		await tools.setOption(t2i, t2i.settings[0], '16:9');
		expect(mocks.patch).toHaveBeenLastCalledWith('/settings/integrations/image/settings', { aspect_ratio: '16:9' });
		vi.useFakeTimers();
		const tts = tool('tts');
		tools.setSlider(tts, tts.settings[1], 1.5);
		tools.setSlider(tts, tts.settings[1], 1.75);
		expect(tools.values.tts_speed).toBe(1.75);
		await vi.advanceTimersByTimeAsync(350);
		expect(mocks.patch).toHaveBeenLastCalledWith('/settings/integrations/tts', { speed: 1.75 });
		expect(mocks.patch).toHaveBeenCalledTimes(2);
	});

	for (const boundary of ['account', 'dispose'] as const) {
		it(`cancels slider saves after ${boundary}`, async () => {
			const tools = await loaded();
			vi.useFakeTimers();
			const tts = tool('tts');
			tools.setSlider(tts, tts.settings[1], 1.5);
			if (boundary === 'account') mocks.owner++;
			else tools.dispose();
			await vi.advanceTimersByTimeAsync(400);
			expect(mocks.patch).not.toHaveBeenCalled();
		});
	}

	it('sends no model-reset follow-ups after the account changes during a save', async () => {
		const tools = await loaded();
		let resolve!: (value: unknown) => void;
		mocks.patch.mockImplementationOnce(() => new Promise((r) => (resolve = r)));
		const pending = tools.selectModel(tool('tts'), 'fal-ai/minimax/speech-2.8-hd');
		expect(mocks.patch).toHaveBeenCalledTimes(1);
		mocks.owner++;
		resolve({});
		expect(await pending).toBe(false);
		await flush();
		expect(mocks.patch).toHaveBeenCalledTimes(1);
		expect(tools.modelId(tool('tts'))).toBe('fal-ai/kokoro/american-english');
	});
});
