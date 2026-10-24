import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { CatalogModel } from '$lib/types/ai-models';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	patch: vi.fn(),
	put: vi.fn(),
	post: vi.fn(),
	del: vi.fn(),
	invalidate: vi.fn(),
	loadVoice: vi.fn(),
	saveVoice: vi.fn()
}));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, patch: mocks.patch, put: mocks.put, post: mocks.post, delete: mocks.del },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
vi.mock('$lib/services/sttStream', () => ({ invalidateSttCapability: mocks.invalidate }));
vi.mock('$lib/api/voice', async (importOriginal) => ({ ...await importOriginal<typeof import('$lib/api/voice')>(), loadVoiceSettings: mocks.loadVoice, saveVoiceSettings: mocks.saveVoice }));

import { catalogStore } from '$lib/stores/catalog';
import { generationChoices } from '$lib/stores/generation-choices.svelte';
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
	model('fal-ai/birefnet', { display_name: 'BiRefNet', category: 'image-to-image', tool_type: 'generic', tags: ['Lofi'] }),
	model('fal-ai/edit-pro', { display_name: 'Edit Pro', category: 'image-to-image', tool_type: 'image_editing' })
];
const categories = [
	{ category: 'text-to-image', count: 2, tool_type: 'image_generation' },
	{ category: 'text-to-video', count: 1, tool_type: 'video_generation' },
	{ category: 'image-to-image', count: 2, tool_type: 'generic' }
];
const SUPERGROK = { source: 'supergrok', account: 'grok-1', worker: 'worker-a', model: 'grok-imagine-image', options: { size: '1k' } };
const OPTIONS = [
	{ source: 'fal', account: 'fal-ai', worker: 'worker-a', model: 'fal-ai/flux/dev', kind: 'image', verified: false,
		options: { image_size: 'square' }, price: { currency: 'USD', unit: 'item', amount: '0.025', total: '0.025', status: 'estimated' },
		quick_settings: { image_size: 'square' }, schema: { fields: [{ name: 'prompt', type: 'string', is_primary_text: true },
			{ name: 'image_size', type: 'enum', enum: ['square', 'landscape_16_9'], default: 'square' }] }, available: true, reason: null },
	{ ...SUPERGROK, kind: 'image', verified: true, price: { currency: 'USD', unit: 'item', amount: '0', total: '0', status: 'included' },
		quick_settings: { size: '1k' }, schema: null, available: true, reason: null }
];
const USD = (amount: string | null, status = 'estimated') => ({ currency: 'USD', unit: 'item', amount, total: amount, status });
const CATALOG = [
	{ source: 'fal', account: 'fal-ai', worker: 'worker-a', model: 'fal-ai/flux/dev', kind: 'image', verified: false, options: { image_size: 'square' },
		price: USD('0.025'), status: 'available', available: true, reason: null, schema: null },
	{ source: 'openai', account: 'prov-2', worker: 'worker-a', model: 'unavailable', kind: 'image', verified: false, options: {},
		price: USD(null, 'unknown'), status: 'unavailable', available: false, reason: 'No verified media schema is available for this account.', schema: null },
	{ source: 'chatgpt', account: 'prov-1', worker: 'worker-a', model: 'gpt-image-2', kind: 'image', verified: true, options: { size: '1024x1024' },
		price: USD('0', 'included'), status: 'available', available: true, reason: null, schema: null },
	{ source: 'supergrok', account: 'grok-9', worker: 'worker-b', model: 'grok-imagine-image', kind: 'image', verified: true, options: {},
		price: USD(null, 'unknown'), status: 'unpriced', available: true, reason: null, schema: null }
];
const INSTALLS = [
	{ id: 'worker-a', device_id: 'worker-a', device_name: 'ServerWorker', is_self: true, online: true, base_url: null, platform: null, version: null, capabilities: {}, last_seen: null },
	{ id: 'worker-b', device_id: 'worker-b', device_name: 'DesktopWorker', is_self: false, online: false, base_url: null, platform: null, version: null, capabilities: {}, last_seen: null }
];
let catalogEntries: typeof CATALOG;
let favourites: Record<string, unknown>;
let remembered: Record<string, unknown>;
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
	remembered = { video: { source: 'fal', account: 'fal-ai', worker: 'worker-a', model: 'fal-ai/ltx-2', options: {}, origin: 'migrated' } };
	generationChoices.remembered = {};
	generationChoices.last = {};
	generationChoices.favourites = {};
	catalogEntries = CATALOG;
	favourites = {};
	integrations.image_edit_model = 'fal-ai/edit-pro';
	mocks.get.mockImplementation(async (url: string) => {
		if (url === '/generation/choices') return { remembered, last: {}, migrated: true, worker: 'worker-a' };
		if (url.startsWith('/generation/options/')) return { kind: 'image', options: OPTIONS };
		if (url.startsWith('/generation/catalog')) {
			const kind = new URLSearchParams(url.split('?')[1]).get('kind');
			return { entries: kind === 'image' ? catalogEntries : [], favourites, kinds: [kind], limit: 15 };
		}
		if (url === '/model-providers') return { providers: [{ id: 'prov-1', name: 'Work plan' }, { id: 'prov-2', name: 'Team key' }] };
		if (url === '/cluster/installs') return { installs: INSTALLS, self_instance_id: 'worker-a' };
		if (url === '/settings/integrations') return integrations;
		if (url === '/settings/text-generation') return textGen;
		if (url === '/catalog/categories') return { categories, total_models: 5 };
		if (url.startsWith('/catalog/models')) return { models: catalog, total: 5, catalog_size: 5 };
		if (url.startsWith('/settings/integrations/model-params')) return { params: {} };
		if (url.startsWith('/catalog/schema')) return { found: false, endpoint_id: '' };
		return {};
	});
	mocks.patch.mockResolvedValue({ success: true });
	mocks.put.mockImplementation(async (url: string, body: Record<string, unknown>) => {
		if (url.startsWith('/generation/favourites/')) return { kind: url.split('/').pop(), favourite: body };
		return url.startsWith('/generation/choices/') ? { choice: { ...(body.choice as object), origin: body.origin } } : { ...textGen, ...body };
	});
	mocks.del.mockResolvedValue({ forgotten: true });
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
	it('lists remembered generation choices, app tasks and the catalog, with no per-tool media defaults', async () => {
		render(ModelsCollection);
		expect(screen.getByText('8 tasks')).toBeTruthy();
		const labels = [...document.querySelectorAll('.st-lg > span:first-child')].map((e) => e.textContent);
		expect(labels).toEqual(['Generation', 'App tasks', 'Catalog']);
		await waitFor(() => expect(item('Video').textContent).toContain('fal.ai · LTX 2'));
		expect(item('Images').textContent).toContain('Asks each time');
		expect(item('Images').querySelector('.st-dot.off')).toBeTruthy();
		expect(item('Video').querySelector('.st-dot.off')).toBeNull();
		await waitFor(() => expect(item('Text tasks').textContent).toContain('Sonnet, low effort'));
		await waitFor(() => expect(item('Dictation').textContent).toContain('GPT-4o Transcribe'));
		expect(item('Read aloud').textContent).toContain('Kokoro');
		expect(item('Voice calls').textContent).toContain('gpt-live-1');
		expect(item('Generation catalog').textContent).toContain('5 fal.ai models');
		for (const removed of ['Text to image', 'Image editing', 'Text to video', 'Image to video', 'Text to 3D', 'Speech to text', 'Audio to audio']) {
			expect(screen.queryByRole('button', { name: new RegExp(`^${removed}`) })).toBeNull();
		}
	});

	it('filters by name, choice and group', async () => {
		render(ModelsCollection);
		await fireEvent.input(screen.getByPlaceholderText('Filter default models'), { target: { value: 'generation' } });
		const names = () => [...document.querySelectorAll('[data-settings-item]')].map((e) => e.getAttribute('data-settings-item'));
		expect(names()).toEqual(['gen-image', 'gen-video', 'gen-audio', 'gen-3d', 'catalog']);
		await fireEvent.input(screen.getByPlaceholderText('Filter default models'), { target: { value: 'aloud' } });
		expect(names()).toEqual(['tts']);
	});

	it('redirects the removed media tool items', () => {
		settingsNav.go({ category: 'models', item: 't2i' });
		expect(settingsNav.selectedItem('models')).toBe('gen-image');
		settingsNav.go({ category: 'models', item: 'i2v' });
		expect(settingsNav.selectedItem('models')).toBe('catalog');
		expect(settingsNav.tabOf('models', 'catalog', ['accounts', 'fal'])).toBe('fal');
	});

	it('picks and remembers a source for a kind with the choice card', async () => {
		await open('Images');
		const detail = screen.getByRole('region', { name: 'Details' });
		expect(within(detail).getByRole('heading', { level: 2, name: 'Images' })).toBeTruthy();
		expect(within(detail).getAllByText('Asks each time').length).toBeGreaterThan(0);
		const grok = await within(detail).findByRole('radio', { name: /SuperGrok/ });
		await fireEvent.click(grok);
		expect(grok.getAttribute('aria-checked')).toBe('true');
		expect(within(grok).getByText('Included')).toBeTruthy();
		await fireEvent.click(within(detail).getByRole('button', { name: /Remember this/ }));
		await waitFor(() => expect(mocks.put).toHaveBeenCalledWith('/generation/choices/image', {
			choice: SUPERGROK, remember: true, origin: 'settings'
		}));
		await waitFor(() => expect(item('Images').textContent).toContain('SuperGrok · Grok Imagine Image'));
	});

	it('shows a migrated choice and clears it so the agent asks again', async () => {
		await open('Video');
		const detail = screen.getByRole('region', { name: 'Details' });
		expect(await within(detail).findByText(/Brought over from your earlier Default models setting/)).toBeTruthy();
		await fireEvent.click(within(detail).getByRole('button', { name: 'Ask every time' }));
		expect(mocks.del).toHaveBeenCalledWith('/generation/choices/video');
		await waitFor(() => expect(item('Video').textContent).toContain('Asks each time'));
	});

	it('reaches the read-aloud voice in two clicks and saves it', async () => {
		await open('Read aloud');
		const detail = screen.getByRole('region', { name: 'Details' });
		expect(within(detail).getByRole('heading', { level: 2, name: 'Read aloud' })).toBeTruthy();
		const voice = within(detail).getByRole('combobox', { name: 'Voice' }) as HTMLSelectElement;
		expect(voice.value).toBe('af_heart');
		expect(voice.selectedOptions[0].textContent).toBe('Heart (female)');
		await fireEvent.change(voice, { target: { value: 'af_bella' } });
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/tts', { voice: 'af_bella' });
		expect(within(detail).getByRole('slider', { name: 'Speed' })).toBeTruthy();
		expect(within(detail).getByRole('combobox', { name: 'Output format' })).toBeTruthy();
		expect(detail.querySelector('[data-setting-anchor="models.ttsVoice"]')).toBeTruthy();
	});

	it('changes the read-aloud model from the Model tab and resets its options', async () => {
		await open('Read aloud');
		await fireEvent.click(screen.getByRole('tab', { name: 'Model' }));
		const minimax = screen.getByRole('button', { name: /MiniMax Speech 2\.8 HD/ });
		await fireEvent.click(minimax);
		await waitFor(() => expect(minimax.getAttribute('aria-pressed')).toBe('true'));
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/audio/tts', { model: 'fal-ai/minimax/speech-2.8-hd' });
		expect(mocks.patch).toHaveBeenCalledWith('/settings/integrations/model-params', { key: 'tts_model', params: {} });
		expect(mocks.patch).toHaveBeenLastCalledWith('/settings/integrations/tts', { voice: 'Wise_Woman' });
		await waitFor(() => expect(item('Read aloud').textContent).toContain('MiniMax Speech 2.8 HD'));
	});

	it('warns on read aloud when fal.ai is off', async () => {
		integrations = { ...integrations, fal_enabled: false };
		await open('Read aloud');
		expect(await screen.findByText('fal.ai is off, so this tool cannot run.')).toBeTruthy();
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

describe('CatalogDetail, fal.ai models', () => {
	const rows = () => [...document.querySelectorAll<HTMLElement>('[data-model-id]')].map((b) => b.dataset.modelId);
	async function openFal() {
		await open('Generation catalog');
		await fireEvent.click(screen.getByRole('tab', { name: 'fal.ai models' }));
	}

	it('filters by category chip (toggling it off again) and by search across name, description, id and tags', async () => {
		await openFal();
		expect(rows()).toHaveLength(5);
		const video = screen.getByRole('button', { name: /Text → Video 1/ });
		expect(screen.getByRole('button', { name: /^All 5/ }).getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(video);
		expect(video.getAttribute('aria-pressed')).toBe('true');
		expect(rows()).toEqual(['fal-ai/ltx-2']);
		await fireEvent.click(video);
		expect(rows()).toHaveLength(5);
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
		expect(rows()).toHaveLength(5);
	});

	it('marks remembered and pinned models, greys out models no tool uses, and remembers a generation pick', async () => {
		await openFal();
		const ltx = document.querySelector<HTMLElement>('[data-model-id="fal-ai/ltx-2"]')!;
		await waitFor(() => expect(ltx.getAttribute('aria-pressed')).toBe('true'));
		expect(ltx.textContent).toContain('Remembered');
		const edit = document.querySelector<HTMLElement>('[data-model-id="fal-ai/edit-pro"]')!;
		expect(edit.getAttribute('aria-pressed')).toBe('true');
		expect(edit.textContent).toContain('Pinned');
		const generic = document.querySelector<HTMLElement>('[data-model-id="fal-ai/birefnet"]')!;
		expect(generic.getAttribute('aria-disabled')).toBe('true');
		expect(generic.textContent).toContain('No tool uses this');
		await fireEvent.click(generic);
		expect(mocks.patch).not.toHaveBeenCalled();
		const hidream = document.querySelector<HTMLElement>('[data-model-id="fal-ai/hidream"]')!;
		await fireEvent.click(hidream);
		await waitFor(() => expect(hidream.getAttribute('aria-pressed')).toBe('true'));
		expect(mocks.put).toHaveBeenCalledWith('/generation/choices/image', {
			choice: { source: 'fal', account: 'fal-ai', worker: 'worker-a', model: 'fal-ai/hidream', options: {} },
			remember: true,
			origin: 'settings'
		});
		expect(mocks.patch).not.toHaveBeenCalledWith('/settings/integrations/image/model', expect.anything());
	});

	it('refreshes from fal.ai', async () => {
		mocks.post.mockResolvedValue({ success: true, models_fetched: 9, categories });
		await openFal();
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		expect(mocks.post).toHaveBeenCalledWith('/catalog/refresh', {});
		await waitFor(() => expect(screen.getByPlaceholderText('Search 9 models')).toBeTruthy());
	});

	it('shows a load failure with a working retry', async () => {
		await openFal();
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
		expect(rows()).toHaveLength(5);
	});
});

describe('CatalogDetail, all accounts', () => {
	const catalogRows = () => [...document.querySelectorAll<HTMLElement>('[data-catalog-row]')];
	const rowFor = (model: string) => catalogRows().find((r) => r.dataset.catalogRow!.endsWith(`|${model}`))!;
	const groupTitles = () => [...document.querySelectorAll('.acat .st-gt > span:first-child')].map((e) => e.textContent);

	async function openAccounts() {
		await open('Generation catalog');
		await waitFor(() => expect(catalogRows()).toHaveLength(4));
	}

	it('opens on all accounts and lists every route by holding worker, with status, price and verification', async () => {
		await openAccounts();
		const detail = screen.getByRole('region', { name: 'Details' });
		expect(within(detail).getByRole('heading', { level: 2, name: 'Generation catalog' })).toBeTruthy();
		expect(screen.getByRole('tab', { name: 'All accounts' }).getAttribute('aria-selected')).toBe('true');
		expect(mocks.get).toHaveBeenCalledWith('/generation/catalog?kind=image&limit=15');
		// This worker first, then the peer; names from the install registry.
		await waitFor(() => expect(groupTitles()).toEqual(['ServerWorker', 'DesktopWorker']));
		const groups = [...document.querySelectorAll<HTMLElement>('.acat .st-grp')];
		expect(groups[0].textContent).toContain('This worker');
		expect(groups[1].textContent).toContain('Offline');
		// Runnable first inside a worker; unavailable last.
		expect(groups[0].querySelectorAll('[data-catalog-row]')[2].getAttribute('data-status')).toBe('unavailable');

		const plan = rowFor('gpt-image-2');
		expect(plan.textContent).toContain('ChatGPT plan · Work plan');
		expect(plan.textContent).toContain('Ready');
		expect(plan.textContent).toContain('Verified');
		expect(plan.textContent).toContain('Included');
		const fal = rowFor('fal-ai/flux/dev');
		expect(fal.textContent).toContain('$0.025');
		expect(fal.textContent).not.toContain('Verified');
		const grok = rowFor('grok-imagine-image');
		expect(grok.closest('.st-grp')!.textContent).toContain('DesktopWorker');
		expect(grok.textContent).toContain('No price');
		expect(grok.textContent).toContain('No known price, so it cannot run.');
		expect(grok.textContent).toContain('Unknown');
		const api = rowFor('unavailable');
		expect(api.textContent).toContain('OpenAI API · Team key');
		expect(api.textContent).toContain('Unavailable');
		expect(api.textContent).toContain('No verified media schema is available for this account.');
		expect(within(api).getByRole('button', { name: /^Favourite/ }).hasAttribute('disabled')).toBe(true);

		const facts = Object.fromEntries(
			[...detail.querySelectorAll('.st-fact')].map((f) => [f.querySelector('dt')!.textContent!.trim(), f.querySelector('dd')!.textContent!.trim()])
		);
		expect(facts).toEqual({ Accounts: '4', Workers: '2', Ready: '2 of 4', Favourite: 'None' });
	});

	it('keeps the favourite apart from the remembered choice: star, replace and remove', async () => {
		remembered = { image: { source: 'fal', account: 'fal-ai', worker: 'worker-a', model: 'fal-ai/flux/dev', options: {} } };
		await openAccounts();
		const card = () => document.querySelector<HTMLElement>('.acat .fav')!;
		expect(card().textContent).toContain('None. Star a row below');
		await waitFor(() => expect(rowFor('fal-ai/flux/dev').textContent).toContain('Remembered'));
		expect(card().textContent).toContain('remembered choice (fal.ai · Flux Dev');

		await fireEvent.click(within(rowFor('grok-imagine-image')).getByRole('button', { name: /^Favourite: SuperGrok/ }));
		await waitFor(() => expect(mocks.put).toHaveBeenCalledWith('/generation/favourites/image', {
			source: 'supergrok', account: 'grok-9', worker: 'worker-b', model: 'grok-imagine-image', options: {}
		}));
		await waitFor(() => expect(card().textContent).toContain('SuperGrok · Grok Imagine Image'));
		expect(card().textContent).toContain('on DesktopWorker');
		expect(card().textContent).toContain('No price');
		// A favourite never changes the remembered choice.
		expect(mocks.put).not.toHaveBeenCalledWith(expect.stringMatching(/^\/generation\/choices\//), expect.anything());
		expect(generationChoices.remembered.image?.model).toBe('fal-ai/flux/dev');
		expect(rowFor('grok-imagine-image').querySelector('.star')!.getAttribute('aria-pressed')).toBe('true');

		// One per kind: starring another replaces it.
		await fireEvent.click(within(rowFor('gpt-image-2')).getByRole('button', { name: /^Favourite: ChatGPT/ }));
		await waitFor(() => expect(card().textContent).toContain('ChatGPT plan · GPT Image 2'));
		expect(rowFor('grok-imagine-image').querySelector('.star')!.getAttribute('aria-pressed')).toBe('false');

		// Starring the favourite again removes it; so does Remove.
		await fireEvent.click(within(rowFor('gpt-image-2')).getByRole('button', { name: /^Remove favourite/ }));
		await waitFor(() => expect(mocks.del).toHaveBeenCalledWith('/generation/favourites/image'));
		await waitFor(() => expect(card().textContent).toContain('None.'));
		await fireEvent.click(within(rowFor('fal-ai/flux/dev')).getByRole('button', { name: /^Favourite/ }));
		await waitFor(() => expect(within(card()).getByRole('button', { name: 'Remove' })).toBeTruthy());
		await fireEvent.click(within(card()).getByRole('button', { name: 'Remove' }));
		await waitFor(() => expect(card().textContent).toContain('None.'));
	});

	it('shows a saved favourite from the server, switches kind and searches', async () => {
		favourites = { image: { source: 'chatgpt', account: 'prov-1', worker: 'worker-a', model: 'gpt-image-2', options: {} } };
		await openAccounts();
		const card = document.querySelector<HTMLElement>('.acat .fav')!;
		await waitFor(() => expect(card.textContent).toContain('ChatGPT plan · GPT Image 2'));
		expect(card.textContent).toContain('Included');

		await fireEvent.click(screen.getByRole('button', { name: 'Video' }));
		await waitFor(() => expect(mocks.get).toHaveBeenCalledWith('/generation/catalog?kind=video&limit=15'));
		expect(await screen.findByText('No connected account can make videos yet.')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Video' }).getAttribute('aria-pressed')).toBe('true');

		await fireEvent.click(screen.getByRole('button', { name: 'Images' }));
		await waitFor(() => expect(catalogRows()).toHaveLength(4));
		catalogEntries = [CATALOG[2]];
		await fireEvent.input(screen.getByRole('searchbox', { name: 'Search accounts and models' }), { target: { value: 'gpt' } });
		await waitFor(() => expect(mocks.get).toHaveBeenCalledWith('/generation/catalog?kind=image&limit=15&query=gpt'));
		await waitFor(() => expect(catalogRows()).toHaveLength(1));
	});

	it('reports a load failure with a working retry', async () => {
		const get = mocks.get.getMockImplementation()!;
		mocks.get.mockImplementation(async (url: string) => {
			if (url.startsWith('/generation/catalog')) throw new Error('peers unreachable');
			return get(url);
		});
		await open('Generation catalog');
		expect(await screen.findByText('peers unreachable')).toBeTruthy();
		mocks.get.mockImplementation(get);
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await waitFor(() => expect(catalogRows()).toHaveLength(4));
		expect(screen.queryByText('peers unreachable')).toBeNull();
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
