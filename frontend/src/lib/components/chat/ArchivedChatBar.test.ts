/** Archived metadata cannot hide the editor after retirement. */
import { get } from 'svelte/store';
import { describe, expect, it } from 'vitest';
import { archivedChats } from '$lib/stores/archived-chats';
import { uiComposer } from '$lib/utils/ui-signature';
import { cardActions } from '$lib/components/layout/session-card-data';
import { isCategoryId } from '$lib/settings/types';

describe('archive retirement', () => {
    it('ignores old metadata on both shells', () => {
        archivedChats.mark('legacy', true);
        expect(get(archivedChats).has('legacy')).toBe(false);
        for (const mobile of [false, true]) {
            expect(uiComposer({ archived: true, emptyHero: false, mobile })).toBe('bottom');
        }
    });
    it('has only membership actions and no archived settings destination', () => {
        expect(cardActions({ status: 'active', is_active: true })).toEqual(['settle']);
        expect(cardActions({ status: 'archived', is_active: false })).toEqual(['activate']);
        expect(isCategoryId('archived')).toBe(false);
    });
});
