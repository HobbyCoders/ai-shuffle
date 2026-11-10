// Second half of chat-position-stability.cases.mjs: every case its FIRST_HALF does not name.
import { runJourney } from './runner.mjs';
import { secondPart } from './lib/split.mjs';
import { FIRST_HALF, journey } from './chat-position-stability.cases.mjs';

await runJourney(secondPart(journey, FIRST_HALF));
