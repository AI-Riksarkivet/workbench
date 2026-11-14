/**
 * The dataset page's shared state: which dataset, the search the reader ran, and the item they picked.
 *
 * A module and not a panel's state, because the dock renders only the active tab of a group and
 * remounts a panel when it is moved, maximized or popped out, and a popped-out panel is mounted in
 * another window, outside the page's component tree: what a panel must show again after that lives
 * here (the dock's first invariant). The reads themselves are shared instances (`reads.svelte.ts`), so
 * a remounted panel finds its answer held.
 */
class Bench {
	/** The dataset the page shows. */
	dataset = $state<string | null>(null);
	/** The search last submitted; empty before the first. */
	query = $state('');
	/** The doc id the reader picked from the results. */
	selected = $state<string | null>(null);
	/** The picked item's media type, when its row named one. */
	selectedMime = $state<string | null>(null);

	/** A new dataset starts with no search and no pick. */
	open(dataset: string): void {
		if (this.dataset === dataset) return;
		this.dataset = dataset;
		this.query = '';
		this.selected = null;
		this.selectedMime = null;
	}
}

export const bench = new Bench();
