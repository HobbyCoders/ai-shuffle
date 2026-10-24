import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { getSpendingLimits, updateSpendingLimits } from './spending-limits';

vi.mock('./client', () => ({ api: { get: vi.fn(), patch: vi.fn() } }));
beforeEach(() => vi.clearAllMocks());

describe('owner spending limits', () => {
    it('reads the neutral owner endpoint', async () => {
        const limits = { daily_spend_limit_usd: 20 };
        vi.mocked(api.get).mockResolvedValue(limits);
        expect(await getSpendingLimits()).toEqual(limits);
        expect(api.get).toHaveBeenCalledWith('/owner/limits');
    });

    it('preserves explicit confirmation when disabling the cap', async () => {
        const patch = { spend_limit_enabled: false, confirm_spend_limit_off: true };
        await updateSpendingLimits(patch);
        expect(api.patch).toHaveBeenCalledWith('/owner/limits', patch);
    });
});
