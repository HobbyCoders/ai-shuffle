import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { getAgentIdentities, updateAgentIdentity } from './agent-identity';

vi.mock('./client', () => ({ api: { get: vi.fn(), patch: vi.fn() } }));
beforeEach(() => vi.clearAllMocks());

describe('owner agent identities', () => {
    it('reads the neutral owner endpoint', async () => {
        const identities = [{ id: 'agent', name: 'Helper', avatar_id: 'spark' }];
        vi.mocked(api.get).mockResolvedValue(identities);
        expect(await getAgentIdentities()).toEqual(identities);
        expect(api.get).toHaveBeenCalledWith('/owner/agents');
    });

    it('encodes the agent ID and preserves the identity-only patch', async () => {
        const patch = { name: 'Helper', avatar_id: 'spark' };
        await updateAgentIdentity('agent/with space', patch);
        expect(api.patch).toHaveBeenCalledWith(
            '/owner/agents/agent%2Fwith%20space/identity', patch
        );
    });
});
