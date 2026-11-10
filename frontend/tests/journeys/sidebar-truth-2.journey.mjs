// Second half of sidebar-truth.cases.mjs: every case its FIRST_HALF does not name.
import { runJourney } from './runner.mjs';
import { secondPart } from './lib/split.mjs';
import { FIRST_HALF, journey } from './sidebar-truth.cases.mjs';

await runJourney(secondPart(journey, FIRST_HALF));
