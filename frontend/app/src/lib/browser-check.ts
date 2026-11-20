/**
 * The classic script `/browser-check.js` (built from this file by `vite.config.ts`), run by `app.html` in <head>
 * after the appearance boot script and before the app's stylesheets and modules. A browser that lacks what the app
 * needs, known without waiting, stops loading the document there, so none of the app is fetched or run, and is shown
 * the page that says why (`browser-support.ts`). Whether a GPU adapter exists takes an answer; the app's `init` hook
 * waits for it (`hooks.client.ts`).
 */
import { missingAtOnce, showUnsupported } from './browser-support.ts';

const reason = missingAtOnce();
if (reason !== null) {
	window.stop();
	showUnsupported(reason);
}
