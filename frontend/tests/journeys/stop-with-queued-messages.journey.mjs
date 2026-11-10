// First half of stop-with-queued-messages.cases.mjs; stop-with-queued-messages-2.journey.mjs runs the rest.
import { runJourney } from './runner.mjs';
import { firstPart } from './lib/split.mjs';
import { FIRST_HALF, journey } from './stop-with-queued-messages.cases.mjs';

await runJourney(firstPart(journey, FIRST_HALF));
