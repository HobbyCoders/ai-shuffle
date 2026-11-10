// Second half of queue-while-busy.cases.mjs: every case its FIRST_HALF does not name.
import { runJourney } from './runner.mjs';
import { secondPart } from './lib/split.mjs';
import { FIRST_HALF, journey } from './queue-while-busy.cases.mjs';

await runJourney(secondPart(journey, FIRST_HALF));
