/** The cloud provider or gateway form logic (#1862). */
import { describe, expect, it } from 'vitest';
import {
	buildValues,
	canSave,
	initialChoice,
	initialInputs,
	methodOf,
	missingFields,
	placeholderFor,
	visibleFields
} from './claude-cloud';
import { cloudConfig } from './fixtures';

const empty = cloudConfig();
const bedrock = methodOf(empty, 'bedrock')!;
const foundry = methodOf(empty, 'foundry')!;
const gateway = methodOf(empty, 'gateway')!;
const vertex = methodOf(empty, 'vertex')!;

describe('visibleFields', () => {
	it('shows only the chosen alternative', () => {
		expect(visibleFields(bedrock, 'keys').map((f) => f.name)).toEqual([
			'region',
			'access_key_id',
			'secret_access_key',
			'session_token'
		]);
		expect(visibleFields(bedrock, 'bearer').map((f) => f.name)).toEqual(['region', 'bearer_token']);
		expect(visibleFields(foundry, 'base_url').map((f) => f.name)).toEqual(['base_url', 'api_key']);
		expect(visibleFields(gateway, null).map((f) => f.name)).toEqual(['base_url', 'auth_token']);
	});
});

describe('initialChoice and initialInputs', () => {
	it('starts on the first alternative with nothing saved, secrets blank', () => {
		expect(initialChoice(empty, bedrock)).toBe('keys');
		expect(initialChoice(empty, gateway)).toBeNull();
		expect(initialInputs(empty, gateway)).toEqual({ base_url: '', auth_token: '' });
	});

	it('starts on the saved alternative with the saved non-secret values', () => {
		const saved = cloudConfig({
			method: 'bedrock',
			values: { region: 'us-east-1' },
			secrets: { bearer_token: { set: true }, access_key_id: { set: false } }
		});
		expect(initialChoice(saved, bedrock)).toBe('bearer');
		expect(initialInputs(saved, bedrock)).toEqual({
			region: 'us-east-1',
			access_key_id: '',
			secret_access_key: '',
			session_token: '',
			bearer_token: ''
		});
		const f = cloudConfig({ method: 'foundry', values: { base_url: 'https://x.example' }, secrets: {} });
		expect(initialChoice(f, foundry)).toBe('base_url');
	});

	it('ignores values saved for another method', () => {
		const saved = cloudConfig({ method: 'gateway', values: { base_url: 'https://gw.example' } });
		expect(initialInputs(saved, foundry).base_url).toBe('');
	});
});

describe('buildValues', () => {
	it('sends only the visible fields, trimmed, blanks left out', () => {
		const inputs = {
			region: ' us-west-2 ',
			access_key_id: 'FAKEKEYID',
			secret_access_key: '',
			session_token: '',
			bearer_token: 'fake-bearer'
		};
		expect(buildValues(bedrock, 'keys', inputs)).toEqual({ region: 'us-west-2', access_key_id: 'FAKEKEYID' });
		expect(buildValues(bedrock, 'bearer', inputs)).toEqual({ region: 'us-west-2', bearer_token: 'fake-bearer' });
	});

	it('never sends another method\'s fields', () => {
		const out = buildValues(gateway, null, { base_url: 'https://gw.example', auth_token: 't', region: 'x' });
		expect(Object.keys(out)).toEqual(['base_url', 'auth_token']);
	});
});

describe('missingFields and canSave', () => {
	it('needs every required value and the acknowledgement', () => {
		const inputs = { base_url: 'https://gw.example', auth_token: '' };
		expect(missingFields(empty, gateway, null, inputs)).toEqual(['Auth token']);
		expect(canSave(empty, gateway, null, inputs, true)).toBe(false);
		const full = { ...inputs, auth_token: 'fake-token' };
		expect(canSave(empty, gateway, null, full, false)).toBe(false);
		expect(canSave(empty, gateway, null, full, true)).toBe(true);
		expect(canSave(empty, null, null, full, true)).toBe(false);
	});

	it('counts a stored secret of the same method as present', () => {
		const saved = cloudConfig({
			method: 'gateway',
			values: { base_url: 'https://gw.example' },
			secrets: { auth_token: { set: true } }
		});
		const inputs = { base_url: 'https://gw.example', auth_token: '' };
		expect(missingFields(saved, gateway, null, inputs)).toEqual([]);
		expect(canSave(saved, gateway, null, inputs, true)).toBe(true);
		// A secret stored for the gateway does not satisfy Foundry.
		expect(missingFields(saved, foundry, 'resource', { resource: 'r', api_key: '' })).toEqual(['API key']);
	});

	it('treats the Bedrock session token as optional', () => {
		const inputs = { region: 'us-east-1', access_key_id: 'A', secret_access_key: 'B', session_token: '' };
		expect(missingFields(empty, bedrock, 'keys', inputs)).toEqual([]);
	});
});

describe('placeholderFor', () => {
	it('says a stored secret is kept, never shows it', () => {
		const saved = cloudConfig({ method: 'gateway', secrets: { auth_token: { set: true } } });
		const token = gateway.fields.find((f) => f.name === 'auth_token')!;
		expect(placeholderFor(saved, gateway, token)).toBe('Stored. Leave blank to keep.');
		expect(placeholderFor(empty, gateway, token)).toBe('');
		expect(placeholderFor(empty, gateway, gateway.fields[0])).toBe('https://');
		expect(placeholderFor(empty, vertex, vertex.fields[0])).toBe('us-east5');
	});
});
