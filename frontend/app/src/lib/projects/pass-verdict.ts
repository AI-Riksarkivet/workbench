/**
 * The last verdict of a review pass, carried from the canvas to the project's Review tab, where a pass with nothing
 * left returns: the tab says it once. It is held in the page's memory and taken when read, so a reload, or a later
 * visit to the tab, does not say it again.
 */

let held: { projectId: string; text: string } | null = null;

/** Hold ``text`` for the Review tab of ``projectId``. */
export function carryVerdict(projectId: string, text: string): void {
	held = { projectId, text };
}

/** The verdict held for the Review tab of ``projectId``, given up as it is read; null when none is. */
export function takeVerdict(projectId: string): string | null {
	if (held?.projectId !== projectId) return null;
	const { text } = held;
	held = null;
	return text;
}
