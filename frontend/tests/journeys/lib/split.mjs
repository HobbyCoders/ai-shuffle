// One journey's cases run as two journey files, so neither is a whole CI shard.
// The first file names its cases; the second takes every other one, so no case
// can fall between them.

function check(journey, first) {
	const names = new Set(journey.cases.map((c) => c.name));
	const unknown = first.filter((name) => !names.has(name));
	if (unknown.length) throw new Error(`${journey.name}: no such case: ${unknown.join(', ')}`);
	if (new Set(first).size !== first.length) throw new Error(`${journey.name}: a case is listed twice`);
	if (!first.length || first.length === names.size) throw new Error(`${journey.name}: both halves need a case`);
}

/** The journey spec for the cases named in `first`. */
export function firstPart(journey, first) {
	check(journey, first);
	return { ...journey, cases: journey.cases.filter((c) => first.includes(c.name)) };
}

/** The journey spec, named `<name>-2`, for every case `first` does not name. */
export function secondPart(journey, first) {
	check(journey, first);
	return { ...journey, name: `${journey.name}-2`, cases: journey.cases.filter((c) => !first.includes(c.name)) };
}
