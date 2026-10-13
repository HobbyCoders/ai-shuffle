import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock('./client', async (importOriginal) => ({
	...(await importOriginal<typeof import('./client')>()),
	api: { post: mocks.post },
}));
import { buildConnectorCreate, connectorNeedsAgent, createConnector } from './connectors';

beforeEach(() => mocks.post.mockReset());

describe('connector create payload', () => {
	it('sends the chosen agent for a new API user', async () => {
		const data = buildConnectorCreate({ name: 'Bot', type: 'telegram', config: { bot_token: 't' }, events: [], profileId: 'agent-1' });
		await createConnector(data);
		expect(mocks.post).toHaveBeenCalledWith('/connectors', {
			name: 'Bot', type: 'telegram', config: { bot_token: 't' }, events: [], profile_ids: ['agent-1'],
		});
	});

	it('reuses an existing API user without overriding its agents', () => {
		const data = buildConnectorCreate({ name: 'Bot', type: 'discord', config: {}, events: [], apiUserId: 'u1', profileId: 'agent-1' });
		expect(data).toEqual({ name: 'Bot', type: 'discord', config: {}, events: [], api_user_id: 'u1' });
	});

	it('omits agents for connectors that run none', () => {
		expect(buildConnectorCreate({ name: 'Lights', type: 'hue', config: {}, events: [] })).not.toHaveProperty('profile_ids');
	});
});

describe('connectorNeedsAgent', () => {
	it('requires an agent for chat connectors and inbound webhooks only', () => {
		expect(connectorNeedsAgent('telegram', {})).toBe(true);
		expect(connectorNeedsAgent('bluebubbles', {})).toBe(true);
		expect(connectorNeedsAgent('hue', {})).toBe(false);
		expect(connectorNeedsAgent('gmail', {})).toBe(false);
		expect(connectorNeedsAgent('webhook', {})).toBe(false);
		expect(connectorNeedsAgent('webhook', { mode: 'outbound' })).toBe(false);
		expect(connectorNeedsAgent('webhook', { mode: ' Inbound ' })).toBe(true);
		expect(connectorNeedsAgent('webhook', { mode: 'bidirectional' })).toBe(true);
	});
});
