// First part of chat-position-stability.cases.mjs.
import { runJourney } from './runner.mjs';
import { firstPart } from './lib/split.mjs';
import { FIRST_PART, journey } from './chat-position-stability.cases.mjs';

await runJourney(firstPart(journey, FIRST_PART));
