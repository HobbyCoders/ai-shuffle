import { expect, it } from 'vitest';
import { CATEGORIES } from '$lib/settings/categories';
import { isCategoryId } from '$lib/settings/types';
it('retires Archived chats including direct category navigation', () => {
    expect(isCategoryId('archived')).toBe(false);
    expect(CATEGORIES.some(category => category.id === ('archived' as string))).toBe(false);
});
