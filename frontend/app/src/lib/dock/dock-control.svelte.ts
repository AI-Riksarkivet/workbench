/**
 * What a panel may ask of the dock it sits in: bring another panel in front of the person (the tree's
 * topic lists its rows in the Results panel, the graph's clip plays in the Player), and whether a panel is
 * maximized (a maximized panel stands in for the page it replaced, so it shows what that page showed).
 *
 * A module and not a context, because a popped-out panel is mounted in another window, outside the page's
 * component tree; the same module instance serves it. The dock attaches itself while it is mounted.
 */
import type { Id } from 'horizon-layout';
import type { PanelId } from './layout.ts';

interface Attached {
	reveal: (id: PanelId) => void;
	maximized: () => Id | undefined;
}

class DockControl {
	#attached = $state.raw<Attached | null>(null);

	/** Attach the mounted dock; returns the detach. */
	attach(dock: Attached): () => void {
		this.#attached = dock;
		return () => {
			if (this.#attached === dock) this.#attached = null;
		};
	}

	/** Bring `id` in front: restore a maximized other panel, front its tab, or open it. */
	reveal(id: PanelId): void {
		this.#attached?.reveal(id);
	}

	/** The panel maximized now, read reactively; none while no dock is mounted. */
	get maximized(): Id | undefined {
		return this.#attached?.maximized();
	}
}

export const dockControl = new DockControl();
