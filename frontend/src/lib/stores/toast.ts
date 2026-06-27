/**
 * Backward-compatibility shim.
 *
 * The toast store has been unified into the single notification system in
 * `notifications.ts`. This module re-exports the legacy `toast` facade (and its
 * types) so existing `import { toast } from '$lib/stores/toast'` call sites keep
 * working. Prefer importing `notify` from `$lib/stores/notifications` in new code.
 */

export { toast, type ToastTone, type ToastAction } from './notifications';

/** @deprecated Use `AppNotification` from `$lib/stores/notifications`. */
export type { AppNotification as Toast } from './notifications';
