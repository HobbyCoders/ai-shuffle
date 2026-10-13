// Small, dependency-free statistics for the perf rig (scripts/perf/journeys.mjs).

/** Nearest-rank percentile: the smallest value with at least p% of samples at
 * or below it (rank = ceil(p/100 * n), 1-based). Never interpolates, so the
 * result is always an observed sample. Returns null for an empty input. */
export function nearestRank(values, p) {
	const xs = values.filter((v) => typeof v === 'number' && Number.isFinite(v)).sort((a, b) => a - b);
	if (xs.length === 0) return null;
	if (!(p > 0 && p <= 100)) throw new RangeError(`percentile out of range: ${p}`);
	const rank = Math.ceil((p / 100) * xs.length);
	return xs[Math.max(rank, 1) - 1];
}

export function median(values) {
	return nearestRank(values, 50);
}

/** n, nearest-rank p50/p75, min and max of the finite samples. */
export function summarize(values) {
	const xs = values.filter((v) => typeof v === 'number' && Number.isFinite(v));
	if (xs.length === 0) return { n: 0, p50: null, p75: null, min: null, max: null };
	return {
		n: xs.length,
		p50: nearestRank(xs, 50),
		p75: nearestRank(xs, 75),
		min: Math.min(...xs),
		max: Math.max(...xs)
	};
}
