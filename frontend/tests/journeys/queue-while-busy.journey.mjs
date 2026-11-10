// First half of queue-while-busy.cases.mjs; queue-while-busy-2.journey.mjs runs the rest.
import { runJourney } from './runner.mjs';
import { firstPart } from './lib/split.mjs';
import { FIRST_HALF, journey } from './queue-while-busy.cases.mjs';

await runJourney(firstPart(journey, FIRST_HALF));
