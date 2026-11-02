// Only cross-transport duplicates are suppressed. Identical successive events
// on one transport remain legitimate (e.g. two clears of the same session).
const seen = new Map<string, { source: string; time: number }>();
// A task chip count repeats (1, 0, 1), so matching copies by content could drop
// the newest and keep a late one. Each carries "<worker run>:<number>", and the
// number only rises within a run: newest wins per chat in each run. A restarted
// worker is a new run, so its lower numbers are not mistaken for old ones.
const chipCountSeqs = new Map<string, number>();
// Late copies arrive within moments, so forgetting the longest-quiet chat is safe.
const MAX_CHIP_COUNT_SEQS = 1000;
export function acceptUserEvent(data: Record<string, unknown>, source: 'chat' | 'global'): boolean {
 if (!['user_session_created', 'user_session_updated', 'user_session_deleted', 'user_session_cleared', 'notification'].includes(String(data.type))) return true;
 const seq = (data.updates as Record<string, unknown> | undefined)?.pending_task_chips_seq;
 if (typeof seq === 'string') {
  const split = seq.lastIndexOf(':');
  const key = `${String(data.session_id)} ${seq.slice(0, split)}`;
  const number = Number(seq.slice(split + 1));
  if (number <= (chipCountSeqs.get(key) ?? 0)) return false;
  chipCountSeqs.delete(key);
  chipCountSeqs.set(key, number);
  if (chipCountSeqs.size > MAX_CHIP_COUNT_SEQS) chipCountSeqs.delete(chipCountSeqs.keys().next().value as string);
  return true;
 }
 const now = Date.now();
 for (const [key, entry] of seen) if (now - entry.time > 5000) seen.delete(key);
 const key = JSON.stringify(data);
 const previous = seen.get(key);
 if (previous && previous.source !== source) { seen.delete(key); return false; }
 seen.set(key, { source, time: now });
 return true;
}
export function resetUserEventDedupe() { seen.clear(); chipCountSeqs.clear(); }
