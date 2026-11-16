// The app's page-to-page view transitions, and whether one is running now. While a transition runs, the browser draws
// the old and new pages as `::view-transition` pseudo-elements over the document and hit-tests every pointer press to
// the root element, so nothing on the new page can take a press until the transition has finished. Measured
// 2026-11-16 on the labeling canvas: a press in that window landed on `<html>` and the person's first stroke was lost.
// A page that takes pointer input reads `viewTransitions.running` before it calls itself ready.

let running = $state(0);

export const viewTransitions = {
	/** True while a transition started by `track` has not finished. */
	get running(): boolean {
		return running > 0;
	},
};

/** Count `transition` as running until its animation has finished, skipped or failed. */
export function track(transition: ViewTransition): void {
	running += 1;
	void transition.finished.finally(() => {
		running -= 1;
	});
}
