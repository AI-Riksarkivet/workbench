/**
 * The saved-views state model of one dataset — the list, WHICH ONE IS ACTIVE, which one is the DEFAULT,
 * and whether what is on screen has DIVERGED from the active one. All readable by the UI without polling.
 * A view is the arrangement, a plain table's set-ups of its Atlas, Graph and Tree, and the rows it listed
 * (`ViewSnapshot`); applying one checks each part as it was read from storage (`select`).
 *
 * THE DECISION THE BACKLOG SKIPPED, made here and pinned by tests:
 *
 *   Once a view is loaded and the user drags a sash, autosave keeps writing the implicit DRAFT. The
 *   named view stays frozen until an explicit Save.
 *
 * The two alternatives are both worse and both silent. Overwriting the view on every drag means you
 * can never open a saved arrangement to look at it without destroying it — the exact opposite of what
 * saving one is for. Silently forking (auto-creating "Compare two runs (2)") fills the sidebar with
 * layouts nobody asked for and makes the name you chose stop meaning anything. So: the draft absorbs
 * every change, the view is a snapshot, and the gap between them is shown rather than resolved
 * behind the user's back — `dirty` is that gap, and the UI renders it as a modified marker beside the
 * active view's name.
 *
 * `dirty` is computed by comparing the live layout against the loaded snapshot rather than by setting
 * a flag on every mutation. A flag would drift the moment anything changed a layout without going
 * through this class — and the honest question is not "did something happen?" but "does what is on
 * screen still match what was saved?", which only a comparison can answer. Serialized layouts are
 * plain JSON of bounded size, so the comparison is cheap and, unlike a flag, self-correcting: drag a
 * panel away and back, and the marker clears itself.
 */
import type { ApiResult } from '#lib/api/client.ts';
import type { SharedView, SharedViewsListing } from '#lib/api/views.api.ts';
import { parseSetups } from '#lib/plain/setup.ts';
import { told } from '#lib/projects/event-words.ts';
import { readStoredLayout } from './layout.ts';
import type { StoredView, ViewSnapshot, ViewsStore } from './store.ts';

/** One dataset's shared views (ADR 0022): kept by a service, listed only to their owner, opened by their link. */
export interface SharedViewsDoor {
	list(): Promise<ApiResult<SharedViewsListing>>;
	share(name: string, snapshot: ViewSnapshot): Promise<ApiResult<SharedView>>;
	stop(viewId: string, name: string): Promise<ApiResult<null>>;
}

/** Applying a view: `ok` with what it holds, `absent` for a view no longer stored, or `unreadable` with why. */
export type ViewRead =
	| { readonly status: 'ok'; readonly view: ViewSnapshot }
	| { readonly status: 'absent' }
	| { readonly status: 'unreadable'; readonly detail: string };

const isNode = (value: unknown): value is ViewSnapshot['node'] =>
	value === null ||
	value === undefined ||
	(typeof value === 'object' &&
		typeof (value as Record<string, unknown>).id === 'string' &&
		['graph', 'tree'].includes(String((value as Record<string, unknown>).of)));

/** What a stored view holds, each part checked as a link's is; one part that does not read spoils it. */
function readView(view: StoredView): ViewRead {
	const layout = readStoredLayout(view.layout);
	if (layout.status !== 'ok') {
		return {
			status: 'unreadable',
			detail: layout.status === 'unreadable' ? layout.detail : 'it holds no layout',
		};
	}
	const setups = parseSetups(view.setups ?? {});
	if ('problem' in setups) {
		return {
			status: 'unreadable',
			detail: `its set-up is not one this workbench can read (${setups.problem})`,
		};
	}
	if (!isNode(view.node))
		return {
			status: 'unreadable',
			detail: 'the node it listed is not one this workbench can read',
		};
	return {
		status: 'ok',
		view: {
			layout: layout.layout,
			setups: setups.setups,
			table: typeof view.table === 'string' ? view.table : null,
			q: typeof view.q === 'string' ? view.q : null,
			node: view.node ?? null,
		},
	};
}

/** A shared view as the stored record its snapshot is read from: nothing in it is trusted for coming from the server,
 *  and each part is checked as a view kept in this browser is (`readView`). */
function fromShared(view: SharedView): StoredView {
	const snapshot = view.snapshot;
	return {
		id: view.view_id,
		name: view.name,
		dataset: view.dataset,
		layout: snapshot.layout,
		setups: snapshot.setups,
		table: typeof snapshot.table === 'string' ? snapshot.table : null,
		q: typeof snapshot.q === 'string' ? snapshot.q : null,
		node: snapshot.node,
		updated: view.shared_at ?? '',
	};
}

/** What a shared view holds, checked part by part as a stored view is. */
export const readSharedView = (view: SharedView): ViewRead => readView(fromShared(view));

/** JSON with SORTED object keys, recursively — the divergence baseline's serializer. A layout built
 *  by the dock and the same layout read back from storage can list their keys in different orders,
 *  and key order is not layout. */
function stableStringify(value: unknown): string {
	return JSON.stringify(value, (_key, v: unknown) => {
		if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
			const rec = v as Record<string, unknown>;
			return Object.fromEntries(
				Object.keys(rec)
					.sort()
					.map((k) => [k, rec[k]]),
			);
		}
		return v;
	});
}

/** What the UI needs from a view row, without exposing the layout payload to it. */
export interface ViewSummary {
	readonly id: string;
	readonly name: string;
	readonly updated: string;
	readonly isDefault: boolean;
}

/** A shared view's row: its id is the one its link names. */
export interface SharedSummary {
	readonly id: string;
	readonly name: string;
}

export type ViewsPhase = 'loading' | 'ready' | 'unreadable';

export class DockViews {
	#store: ViewsStore;
	/** Reads what is on screen now. */
	#snapshot: () => ViewSnapshot | null;
	#door: SharedViewsDoor | null;

	/** The person's own shared views of the dataset, newest first, from the service that keeps them. */
	#shared = $state.raw<SharedView[]>([]);
	/** Whether the person shares more views of the dataset than the listing answered. */
	#sharedMore = $state(false);
	/** Why the shared views could not be listed, or null. */
	#sharedProblem = $state<string | null>(null);
	/** The shared view whose link the sidebar shows: the one just shared, or one whose link was asked for. */
	#linkShown = $state.raw<SharedSummary | null>(null);
	/** What the last action did, told in the past tense; cleared by the next action. */
	#notice = $state<string | null>(null);

	#views = $state<StoredView[]>([]);
	#activeId = $state<string | null>(null);
	/** The layout as it was when the active view was loaded or last saved — the divergence baseline. */
	#baseline = $state<string | null>(null);
	/** Bumped whenever the dock's layout changes, so `dirty` re-derives. */
	#revision = $state(0);
	#phase = $state<ViewsPhase>('loading');
	#detail = $state<string | null>(null);
	#busy = $state(false);
	/** The last refused write, or null — the sidebar renders it instead of failing silently. */
	#lastError = $state<string | null>(null);

	constructor(
		store: ViewsStore,
		snapshot: () => ViewSnapshot | null,
		door: SharedViewsDoor | null = null,
	) {
		this.#store = store;
		this.#snapshot = snapshot;
		this.#door = door;
	}

	/** Whether views can be shared here: a service keeps them. */
	get canShare(): boolean {
		return this.#door !== null;
	}

	/** The person's own shared views of the dataset, newest first. */
	get shared(): SharedSummary[] {
		return this.#shared.map(({ view_id, name }) => ({ id: view_id, name }));
	}

	/** Whether the shared views listed are the newest of more. */
	get sharedCut(): boolean {
		return this.#sharedMore;
	}

	get sharedProblem(): string | null {
		return this.#sharedProblem;
	}

	get linkShown(): SharedSummary | null {
		return this.#linkShown;
	}

	/** Show the link of the shared view `id`. */
	showLink(id: string): void {
		const view = this.#shared.find((v) => v.view_id === id);
		this.#linkShown = view === undefined ? null : { id, name: view.name };
	}

	get notice(): string | null {
		return this.#notice;
	}

	/** Say what an action the page carried out for the sidebar did (a link copied). */
	tell(done: string): void {
		this.#notice = done;
		this.#lastError = null;
	}

	/** Every saved view, newest first. Layout payloads stay inside — the sidebar renders summaries. */
	get views(): ViewSummary[] {
		return this.#views.map(({ id, name, updated, isDefault }) => ({
			id,
			name,
			updated,
			isDefault: isDefault === true,
		}));
	}

	/** The view the dataset opens with, or null. */
	get defaultId(): string | null {
		return this.#views.find((v) => v.isDefault === true)?.id ?? null;
	}

	get activeId(): string | null {
		return this.#activeId;
	}

	/** Whether the active view is a shared one, which is applied but never saved over. */
	get activeIsShared(): boolean {
		return this.#shared.some((v) => v.view_id === this.#activeId);
	}

	get active(): ViewSummary | null {
		const v = this.#views.find((x) => x.id === this.#activeId);
		return v === undefined
			? null
			: { id: v.id, name: v.name, updated: v.updated, isDefault: v.isDefault === true };
	}

	get phase(): ViewsPhase {
		return this.#phase;
	}

	/** Why the library could not be read. Shown rather than swallowed — see `unreadable`. */
	get detail(): string | null {
		return this.#detail;
	}

	/** True while a write is in flight, so the UI can disable the controls that would race it. */
	get busy(): boolean {
		return this.#busy;
	}

	/** The last refused write's description, cleared by the next success. Review finding: every
	 *  refused save/rename/delete was a silent no-op — the name input just sat there. */
	get lastError(): string | null {
		return this.#lastError;
	}

	/**
	 * Has the live arrangement moved away from the active view?
	 *
	 * False when no view is active: the draft is not "dirty" against nothing, it is simply the draft.
	 */
	get dirty(): boolean {
		if (this.#activeId === null || this.#baseline === null) return false;
		// `#revision` is read so this re-derives when `touch()` fires; the comparison is the answer.
		void this.#revision;
		const live = this.#snapshot();
		return live !== null && stableStringify(live) !== this.#baseline;
	}

	/**
	 * The dock's layout changed. Call on every change of the bound `LayoutConfig`.
	 *
	 * Deliberately NOT debounced, unlike autosave: this writes no bytes, and a modified marker that
	 * appears a second after the drag reads as a bug.
	 */
	touch(): void {
		this.#revision += 1;
	}

	/** Load the library. Safe to call again — a refresh keeps the active selection if it survived. */
	async refresh(): Promise<void> {
		const read = await this.#store.list();
		if (read.status === 'unreadable') {
			// Surfaced, never treated as empty: reporting an unreadable library as "no saved views" would
			// invite the user to recreate work that is still there.
			this.#phase = 'unreadable';
			this.#detail = read.detail;
			return;
		}
		this.#views = read.views;
		this.#phase = 'ready';
		this.#detail = null;
		if (this.#activeId !== null && this.#find(this.#activeId) === undefined) {
			this.clearActive();
		}
	}

	/** List the person's shared views of the dataset; a listing that fails is said, never shown as none. */
	async refreshShared(): Promise<void> {
		if (this.#door === null) return;
		const read = await this.#door.list();
		if (!read.ok) {
			this.#sharedProblem = `Your shared views could not be listed: ${read.detail}`;
			return;
		}
		this.#shared = read.data.views;
		this.#sharedMore = read.data.more;
		this.#sharedProblem = null;
	}

	/** Share the saved view `id`: what it holds, checked as when it is applied, and its name. */
	async share(id: string): Promise<boolean> {
		const door = this.#door;
		const view = this.#views.find((v) => v.id === id);
		if (door === null || view === undefined || this.#busy) return false;
		const read = readView(view);
		if (read.status !== 'ok') {
			this.#lastError = `This view could not be shared: ${read.status === 'unreadable' ? read.detail : 'it is not stored'}.`;
			return false;
		}
		this.#busy = true;
		try {
			const result = await door.share(view.name, read.view);
			if (!result.ok) {
				this.#lastError = `Sharing failed: ${result.detail}`;
				this.#notice = null;
				return false;
			}
			this.#shared = [result.data, ...this.#shared];
			this.#linkShown = { id: result.data.view_id, name: result.data.name };
			this.#lastError = null;
			this.#notice = told('Shared', result.data.name);
			return true;
		} finally {
			this.#busy = false;
		}
	}

	/** Stop sharing the shared view `id`: its link opens nothing after. The person has confirmed it. */
	async stopSharing(id: string): Promise<boolean> {
		const door = this.#door;
		const view = this.#shared.find((v) => v.view_id === id);
		if (door === null || view === undefined || this.#busy) return false;
		this.#busy = true;
		try {
			const result = await door.stop(id, view.name);
			if (!result.ok) {
				this.#lastError = `Stopping sharing failed: ${result.detail}`;
				this.#notice = null;
				return false;
			}
			this.#shared = this.#shared.filter((v) => v.view_id !== id);
			if (this.#linkShown?.id === id) this.#linkShown = null;
			if (this.#activeId === id) this.clearActive();
			this.#lastError = null;
			this.#notice = told('Stopped sharing', view.name);
			return true;
		} finally {
			this.#busy = false;
		}
	}

	/** A view by id: one saved in this browser, or one of the person's shared views. */
	#find(id: string): StoredView | undefined {
		const local = this.#views.find((v) => v.id === id);
		if (local !== undefined) return local;
		const shared = this.#shared.find((v) => v.view_id === id);
		return shared === undefined ? undefined : fromShared(shared);
	}

	/**
	 * The layout to apply for a view, as the dock's own three-outcome contract: a missing view is
	 * `absent` (a stale sidebar click is ordinary, not a crash), and a stored layout this workbench
	 * cannot show is `unreadable`.
	 *
	 * Read-only: the caller applies the layout first, then commits with `activate(id)`, so a click
	 * that does not apply never leaves a checked, unapplied view.
	 */
	select(id: string): ViewRead {
		const view = this.#find(id);
		if (view === undefined) return { status: 'absent' };
		return readView(view);
	}

	/** Commit a view as ACTIVE — call after its layout was actually applied to the dock. */
	activate(id: string): void {
		const view = this.#find(id);
		if (view === undefined) return;
		const read = readView(view);
		this.#activeId = id;
		this.#baseline = read.status === 'ok' ? stableStringify(read.view) : null;
		this.#revision += 1;
	}

	/** Say why something the person asked for did not happen (a view that could not be applied). */
	report(problem: string): void {
		this.#lastError = problem;
	}

	/**
	 * The person's explicit reset of views that could not be read: none are kept, the layout on screen
	 * stays, and saving views works again.
	 */
	async reset(): Promise<boolean> {
		if (this.#busy) return false;
		this.#busy = true;
		try {
			const ok = await this.#store.reset();
			this.#lastError = ok ? null : 'Reset failed — the views were not changed';
			if (ok) {
				this.clearActive();
				await this.refresh();
			}
			return ok;
		} finally {
			this.#busy = false;
		}
	}

	/** Stop tracking a view — the arrangement stays exactly as it is, it just stops being "the view". */
	clearActive(): void {
		this.#activeId = null;
		this.#baseline = null;
	}

	/** Save the current arrangement as a NEW view, and make it active. */
	async saveAs(name: string): Promise<boolean> {
		const snapshot = this.#snapshot();
		if (snapshot === null || name.trim() === '') return false;
		return this.#write('Save view', async () => {
			const created = await this.#store.create(name.trim(), snapshot);
			if (created === null) return false;
			this.#views = [created, ...this.#views];
			this.#activeId = created.id;
			this.#baseline = stableStringify(snapshot);
			return true;
		});
	}

	/** Overwrite the ACTIVE view with the current arrangement — the explicit Save `dirty` invites. */
	async save(): Promise<boolean> {
		const id = this.#activeId;
		const snapshot = this.#snapshot();
		if (id === null || snapshot === null || this.activeIsShared) return false;
		return this.#write('Save', async () => {
			if (!(await this.#store.update(id, snapshot))) return false;
			this.#views = this.#views.map((v) => (v.id === id ? { ...v, ...snapshot } : v));
			this.#baseline = stableStringify(snapshot);
			return true;
		});
	}

	/** Make `id` the dataset's default view, or, when it already is, none. */
	async toggleDefault(id: string): Promise<boolean> {
		const next = this.defaultId === id ? null : id;
		return this.#write('Default', async () => {
			if (!(await this.#store.makeDefault(next))) return false;
			this.#views = this.#views.map((v) => ({ ...v, isDefault: v.id === next }));
			return true;
		});
	}

	async rename(id: string, name: string): Promise<boolean> {
		if (name.trim() === '') return false;
		return this.#write('Rename', async () => {
			if (!(await this.#store.rename(id, name.trim()))) return false;
			this.#views = this.#views.map((v) => (v.id === id ? { ...v, name: name.trim() } : v));
			return true;
		});
	}

	async remove(id: string): Promise<boolean> {
		return this.#write('Delete', async () => {
			if (!(await this.#store.remove(id))) return false;
			this.#views = this.#views.filter((v) => v.id !== id);
			// Deleting the active view leaves the arrangement on screen untouched — it is now the draft
			// again. Clearing the dock would destroy work the user never asked to lose.
			if (this.#activeId === id) this.clearActive();
			return true;
		});
	}

	/** One busy flag, one refusal rule: nothing writes while the library is unreadable. */
	async #write(op: string, fn: () => Promise<boolean>): Promise<boolean> {
		if (this.#phase === 'unreadable') {
			this.#lastError = `${op} refused — the library is unreadable`;
			return false;
		}
		if (this.#busy) return false;
		this.#busy = true;
		try {
			const ok = await fn();
			this.#lastError = ok ? null : `${op} failed — not saved`;
			this.#notice = null;
			return ok;
		} finally {
			this.#busy = false;
		}
	}
}
