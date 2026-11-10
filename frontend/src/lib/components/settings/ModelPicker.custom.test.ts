/**
 * The "Custom…" row only makes sense where something lets the user type the
 * model id it stands for. No caller of ModelPicker renders such a field, so
 * every caller turns the row off; the API user form offered it with nowhere to
 * type (#1337).
 */
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));

import { hydrateModelRegistry } from '$lib/constants/models';
import ModelPicker from './ModelPicker.svelte';

beforeAll(async () => {
	mocks.get.mockResolvedValueOnce({
		models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: false }],
		default: 'sonnet',
		legacy_map: {},
		claude: { auth_kind: 'subscription', plan_type: null },
		providers: []
	});
	await hydrateModelRegistry();
});

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
});

const sources = import.meta.glob('/src/lib/**/*.svelte', { query: '?raw', import: 'default', eager: true }) as Record<
	string,
	string
>;

/** Every `<ModelPicker ...>` tag in the app, with the file it is in. */
function pickerTags(): { file: string; tag: string }[] {
	const tags: { file: string; tag: string }[] = [];
	for (const [file, source] of Object.entries(sources)) {
		for (const match of source.matchAll(/<ModelPicker\b[\s\S]*?\/>/g)) tags.push({ file, tag: match[0] });
	}
	return tags;
}

describe('ModelPicker "Custom…" row (#1337)', () => {
	it('is not offered when allowCustom is false', async () => {
		const { getByRole, queryByRole } = render(ModelPicker, {
			props: { value: 'sonnet', onSelect: vi.fn(), allowEmpty: true, allowCustom: false }
		});
		await fireEvent.click(getByRole('button', { name: 'Sonnet' }));
		expect(queryByRole('option', { name: /Sonnet/ })).not.toBeNull();
		expect(queryByRole('option', { name: /Custom/ })).toBeNull();
	});

	// A caller may offer "Custom…" only when it also renders a field to type the
	// id; none does today, so each must pass allowCustom={false}. A caller that
	// adds such a field should be exempted here by file.
	it('is turned off by every caller, the API user form included, since none has a field to type the id', () => {
		const tags = pickerTags();
		expect(tags.some(({ file }) => file.endsWith('/integrations/ApiUserDetail.svelte'))).toBe(true);
		const offering = tags.filter(({ tag }) => !/\ballowCustom=\{false\}/.test(tag)).map(({ file }) => file);
		expect(offering).toEqual([]);
	});
});
