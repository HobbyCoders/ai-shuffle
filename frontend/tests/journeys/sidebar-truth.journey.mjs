// First half of sidebar-truth.cases.mjs; sidebar-truth-2.journey.mjs runs the rest.
import { runJourney } from './runner.mjs';
import { firstPart } from './lib/split.mjs';
import { FIRST_HALF, journey } from './sidebar-truth.cases.mjs';

await runJourney(firstPart(journey, FIRST_HALF));
