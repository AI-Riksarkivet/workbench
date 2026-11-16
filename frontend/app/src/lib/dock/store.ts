/**
 * WHERE the workbench keeps a person's layout and named views — the one module that decides it.
 *
 * In this browser's `localStorage`, one key per signed-in person: `workbench-dock:` and the SHA-256 of
 * the principal's id, so the key never carries the id itself; on a stack that signs nobody in, one
 * fixed key. The value is the last layout of each dataset the person arranged (by the dataset's id, the
 * {@link MAX_LAYOUTS} most recently arranged) and the named views, and nothing else: a layout names only
 * panel ids; a view adds the name the person gave it, the dataset it is of, the set-ups of a plain
 * table's Atlas, Graph and Tree (column names, `#lib/plain/setup.ts`), the table and the search or node it
 * listed, and whether it is that dataset's default. No token and no table data are stored.
 *
 * A layout and a view are kept per dataset because the panels a dataset can feed differ: one arranged
 * around a knowledge graph is not the arrangement a plain table opens with, and a set-up names one table's
 * columns. A dataset may have several views; the one marked default opens when the dataset is opened with
 * no link that says otherwise.
 *
 * Sign-out does not clear it, unlike the activity log (`activity.forget()`): a layout is a preference
 * of the person who signs in on this browser again, while the activity log records what they did.
 *
 * Every access is guarded: where storage is refused (a private window, blocked site data, a full
 * quota) the document lives in memory for the page's life, and `persistent` says so.
 *
 * Reads keep three outcomes. `absent`: nothing stored. `ok`. `unreadable`: something is stored and
 * could not be read; it is reported, never replaced by a default, and never written over until the
 * person resets it.
 */
import type { LayoutConfig } from 'horizon-layout';
import type { Setups } from '#lib/plain/setup.ts';
import type { LayoutStore } from './autosave.ts';
import { readStoredLayout, type LayoutRead } from './layout.ts';
import type { LinkedNode } from './link.ts';

const PREFIX = 'workbench-dock:';
const ANONYMOUS = `${PREFIX}local`;
/** The datasets whose layouts are kept; arranging one more forgets the least recently arranged. */
export const MAX_LAYOUTS = 50;

/** What a view holds of what is on screen: the arrangement, a plain table's set-ups and the rows listed. */
export interface ViewSnapshot {
	layout: LayoutConfig;
	/** The table in view, for a dataset of several tables; null for a stamped dataset. */
	table: string | null;
	setups: Setups;
	/** The search the rows were listed by, or the node they were filtered to. */
	q: string | null;
	node: LinkedNode | null;
}

/** One saved view. `id` is stable across renames; the sidebar addresses views by it. */
export interface StoredView {
	readonly id: string;
	readonly name: string;
	/** The dataset the view is of. */
	readonly dataset: string;
	/** Read with `readStoredLayout` when applied: a view's arrangement is a stored layout like any other. */
	readonly layout: unknown;
	/** Read with `parseSetups` when applied: what is stored is checked as a link's set-up is. */
	readonly setups?: unknown;
	readonly table?: string | null;
	readonly q?: string | null;
	readonly node?: unknown;
	/** The view the dataset opens with. */
	readonly isDefault?: boolean;
	/** ISO-8601, the order the sidebar lists views in. */
	readonly updated: string;
}

export type ViewsRead =
	| { readonly status: 'ok'; readonly views: StoredView[] }
	| { readonly status: 'unreadable'; readonly detail: string };

/** The document under the key. Each layout is kept as stored, so writing views never rewrites one. */
interface Document {
	layouts?: unknown;
	views?: unknown;
}

/** One dataset's stored layout. */
interface StoredLayout {
	dataset: string;
	layout: unknown;
}

const isStoredLayout = (value: unknown): value is StoredLayout =>
	typeof value === 'object' &&
	value !== null &&
	typeof (value as Record<string, unknown>).dataset === 'string' &&
	'layout' in value;

/** The stored layouts, least recently arranged first: a list, so the order is the list's and no dataset id
 *  is ever read as an object's key. Anything else stored there is none. */
function layoutsOf(doc: Document): StoredLayout[] {
	return Array.isArray(doc.layouts) ? doc.layouts.filter(isStoredLayout) : [];
}

/** `layouts` with `dataset`'s set to `layout`, moved to the most recent, the oldest beyond the bound dropped. */
function withLayout(
	layouts: StoredLayout[],
	dataset: string,
	layout: LayoutConfig,
): StoredLayout[] {
	const rest = layouts.filter((entry) => entry.dataset !== dataset);
	return [...rest.slice(-(MAX_LAYOUTS - 1)), { dataset, layout }];
}

function isView(value: unknown): value is StoredView {
	if (typeof value !== 'object' || value === null) return false;
	const v = value as Record<string, unknown>;
	return (
		typeof v.id === 'string' &&
		typeof v.name === 'string' &&
		typeof v.dataset === 'string' &&
		typeof v.updated === 'string'
	);
}

type Read = { readonly doc: Document } | { readonly broken: string };

export class WorkbenchStore {
	readonly #key: string;
	readonly #storage: () => Storage;
	/** The document as this page last read or wrote it; the record itself while storage is refused. */
	#doc: Document = {};
	#persistent = true;
	/** Told after this page's own reset, which another tab hears as a `storage` event. */
	readonly #resetListeners = new Set<() => void>();

	constructor(key: string, storage: () => Storage) {
		this.#key = key;
		this.#storage = storage;
		this.#read();
	}

	/** False when this browser refuses storage: the layout and views last until the tab closes. */
	get persistent(): boolean {
		return this.#persistent;
	}

	/**
	 * The stored document as it is NOW. Read on every load and before every write, because another
	 * tab of the same person writes the same key: a write merges into what is stored, never into what
	 * this page read earlier.
	 */
	#read(): Read {
		if (!this.#persistent) return { doc: this.#doc };
		let raw: string | null;
		try {
			raw = this.#storage().getItem(this.#key);
		} catch {
			this.#persistent = false;
			return { doc: this.#doc };
		}
		if (raw === null) return { doc: {} };
		try {
			const parsed: unknown = JSON.parse(raw);
			if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
				throw new Error('not an object');
			}
			this.#doc = parsed as Document;
			return { doc: this.#doc };
		} catch (e) {
			return {
				broken: `the stored workbench is not JSON this app can read (${e instanceof Error ? e.message : String(e)})`,
			};
		}
	}

	loadLayout(dataset: string): LayoutRead {
		const read = this.#read();
		if ('broken' in read) return { status: 'unreadable', detail: read.broken };
		const stored = layoutsOf(read.doc).find((entry) => entry.dataset === dataset);
		if (stored === undefined) return { status: 'absent' };
		return readStoredLayout(stored.layout);
	}

	loadViews(): ViewsRead {
		const read = this.#read();
		if ('broken' in read) return { status: 'unreadable', detail: read.broken };
		const views = read.doc.views ?? [];
		if (!Array.isArray(views) || !views.every(isView)) {
			return { status: 'unreadable', detail: 'the stored views are not a list of named layouts' };
		}
		return { status: 'ok', views };
	}

	/** Store `dataset`'s live layout; the other layouts and the views are kept as they are. Refused over
	 *  a document that is not JSON. */
	saveLayout(dataset: string, layout: LayoutConfig): boolean {
		return this.#update((doc) => ({
			...doc,
			layouts: withLayout(layoutsOf(doc), dataset, layout),
		}));
	}

	/** Store the named views; the stored layout is kept as it is. */
	saveViews(views: readonly StoredView[]): boolean {
		return this.#update((doc) => ({ ...doc, views: [...views] }));
	}

	/**
	 * The person's explicit reset of `dataset`'s layout: its stored layout becomes `layout`. A document
	 * that is not JSON is replaced whole, its views with it, since nothing in it could be shown.
	 */
	reset(dataset: string, layout: LayoutConfig): boolean {
		return this.#update(
			(doc) => ({ ...doc, layouts: withLayout(layoutsOf(doc), dataset, layout) }),
			true,
		);
	}

	/** One dataset's layout, as the autosave reads and writes it. */
	layoutOf(dataset: string): LayoutStore {
		return {
			loadLayout: () => this.loadLayout(dataset),
			saveLayout: (layout) => this.saveLayout(dataset, layout),
			reset: (layout) => this.reset(dataset, layout),
		};
	}

	/** The person's explicit reset of the named views: none are kept, and the layout stays. */
	resetViews(): boolean {
		return this.#update((doc) => ({ ...doc, views: [] }), true);
	}

	/**
	 * Call `listener` when this person's document changes under the page: another tab wrote it, or
	 * this page reset it. Returns the unsubscribe.
	 */
	onChange(listener: () => void): () => void {
		this.#resetListeners.add(listener);
		if (typeof window === 'undefined') return () => this.#resetListeners.delete(listener);
		const handle = (event: StorageEvent): void => {
			if (event.key === this.#key || event.key === null) listener();
		};
		window.addEventListener('storage', handle);
		return () => {
			this.#resetListeners.delete(listener);
			window.removeEventListener('storage', handle);
		};
	}

	#update(change: (doc: Document) => Document, isReset = false): boolean {
		const read = this.#read();
		if ('broken' in read && !isReset) return false;
		const next = change('broken' in read ? {} : read.doc);
		this.#doc = next;
		if (this.#persistent) {
			try {
				this.#storage().setItem(this.#key, JSON.stringify(next));
			} catch {
				// Refused now (a full quota, site data blocked mid-session): the page keeps working from
				// memory, and says so.
				this.#persistent = false;
			}
		}
		if (isReset) for (const listener of this.#resetListeners) listener();
		return true;
	}
}

/**
 * A per-person storage key under `prefix`: the prefix and the SHA-256 of the principal's id, so the key never
 * carries the id itself; `<prefix>local` where nobody signs in. Throws where there is no hash to key by (an
 * insecure context has no `crypto.subtle`).
 */
export async function personKey(prefix: string, principal: string | null): Promise<string> {
	if (principal === null) return `${prefix}local`;
	const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(principal));
	const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
	return `${prefix}${hex}`;
}

const keyFor = (principal: string | null): Promise<string> => personKey(PREFIX, principal);

/** The store for one person (`principal`, the signed-in subject; null where nobody signs in). */
export async function openWorkbenchStore(
	principal: string | null,
	storage: () => Storage = () => window.localStorage,
): Promise<WorkbenchStore> {
	try {
		return new WorkbenchStore(await keyFor(principal), storage);
	} catch {
		// No hash to key by (an insecure context has no `crypto.subtle`): memory only.
		return new WorkbenchStore(ANONYMOUS, () => {
			throw new Error('no storage key');
		});
	}
}

/** One dataset's named views' operations, as the sidebar's state model calls them. */
export interface ViewsStore {
	list(): Promise<ViewsRead>;
	/** Create a view, newest first. Returns the stored record, or null when the write was refused. */
	create(name: string, snapshot: ViewSnapshot): Promise<StoredView | null>;
	update(id: string, snapshot: ViewSnapshot): Promise<boolean>;
	rename(id: string, name: string): Promise<boolean>;
	remove(id: string): Promise<boolean>;
	/** Make `id` the dataset's default view (its others are not), or none with null. */
	makeDefault(id: string | null): Promise<boolean>;
	/** The person's explicit reset of views that could not be read: none are kept. */
	reset(): Promise<boolean>;
}

const newId = (): string =>
	typeof crypto !== 'undefined' && 'randomUUID' in crypto
		? crypto.randomUUID()
		: `v-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

/** `dataset`'s views in `store`, each change a read-modify-write of every dataset's views that refuses while
 *  the views are unreadable and leaves other datasets' views as they are. */
export function viewsOf(
	store: WorkbenchStore,
	dataset: string,
	now = (): string => new Date().toISOString(),
): ViewsStore {
	const mutate = async (fn: (views: StoredView[]) => StoredView[]): Promise<boolean> => {
		const read = store.loadViews();
		if (read.status === 'unreadable') return false;
		const others = read.views.filter((v) => v.dataset !== dataset);
		const mine = read.views.filter((v) => v.dataset === dataset);
		return store.saveViews([...fn(mine), ...others]);
	};
	const stored = (snapshot: ViewSnapshot) => ({
		layout: snapshot.layout,
		setups: snapshot.setups,
		table: snapshot.table,
		q: snapshot.q,
		node: snapshot.node,
	});
	return {
		async list() {
			const read = store.loadViews();
			return read.status === 'ok'
				? { status: 'ok', views: read.views.filter((v) => v.dataset === dataset) }
				: read;
		},
		async create(name, snapshot) {
			const view: StoredView = { id: newId(), name, dataset, ...stored(snapshot), updated: now() };
			return (await mutate((views) => [view, ...views])) ? view : null;
		},
		update: (id, snapshot) =>
			mutate((views) =>
				views.map((v) => (v.id === id ? { ...v, ...stored(snapshot), updated: now() } : v)),
			),
		rename: (id, name) =>
			mutate((views) => views.map((v) => (v.id === id ? { ...v, name, updated: now() } : v))),
		remove: (id) => mutate((views) => views.filter((v) => v.id !== id)),
		makeDefault: (id) => mutate((views) => views.map((v) => ({ ...v, isDefault: v.id === id }))),
		reset: async () => store.resetViews(),
	};
}
