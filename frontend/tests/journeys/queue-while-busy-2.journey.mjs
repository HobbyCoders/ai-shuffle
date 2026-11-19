// The cases neither part 1 nor part 3 names, including newly added cases.
import { runJourney } from './runner.mjs';
import { secondPart } from './lib/split.mjs';
import { FIRST_HALF, THIRD_PART, journey } from './queue-while-busy.cases.mjs';

await runJourney(secondPart(journey, [...FIRST_HALF, ...THIRD_PART]));
