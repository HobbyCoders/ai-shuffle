// The sole relaxed reading settle is a phone oversize-singleton transition
// at scroll origin. All entries are counted, including individually small CLS.
export function permitsOversizeOriginSettle({ phone, before, after, settle }) {
	if (!phone || after.rows !== 1 || !(after.maxDrift <= Math.min(20, Math.max(1, before.top))) ||
		settle.transitions.length !== 1 || settle.entries.length !== 1) return false;
	const [transition] = settle.transitions, [entry] = settle.entries;
	return transition.fromRows > 1 && transition.toRows === 1 && transition.reader && transition.scrollTop === 0 &&
		entry.transition === transition.id && entry.scrollTop === 0 && entry.singleton && entry.virtualizedOnly &&
		entry.value > 0 && entry.value < 0.1 && settle.total < 0.1;
}
