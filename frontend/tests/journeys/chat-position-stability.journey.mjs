// First half of chat-position-stability.cases.mjs; chat-position-stability-2.journey.mjs runs the rest.
import { runJourney } from './runner.mjs';
import { firstPart } from './lib/split.mjs';
import { FIRST_HALF, journey } from './chat-position-stability.cases.mjs';

await runJourney(firstPart(journey, FIRST_HALF));
