/**
 * Exploring one dataset: the search the reader ran (text, image, topic or voice), its hits and their
 * paging, the documents browsed before any search, the item open in the player, and how the results
 * are laid out.
 *
 * A module and not a panel's state, because the dock renders only a group's active tab and remounts a
 * panel when it is moved, maximized or popped out (a popped-out panel lives in another window): what a
 * panel must show again after that lives here (the dock's first invariant). The dataset page opens it on
 * every navigation (`open`), which starts a new dataset with nothing searched.
 *
 * Reads go through the app's data layer (`#lib/api/explore.api.ts`); a search that carries a file goes
 * to the client directly, on the same transport. Only the latest search may write the hits: searches
 * overlap (a filter applies on change), so each takes a sequence number and a superseded answer is
 * dropped.
 *
 * Nothing re-reads on its own when the lakehouse changes: the reader searches again, or pages, to see
 * newer rows.
 */
import {
	ApiError,
	activeView,
	search,
	voiceSimilarUpload,
	type Document,
	type Hit,
	type SearchSpec,
	type VoiceAnchor,
	type VoiceHit,
	type VoiceQueryInfo,
	type VoiceSimilarResponse,
} from '@workbench/explorer-api';
import { atlasRows } from '#lib/api/atlas.api.ts';
import { documentsPage, searchHits, voiceHits } from '#lib/api/explore.api.ts';
import { hitKey } from './utils.ts';
import { voiceSearch, type VoiceRequest } from './voice-search.svelte.ts';

/** How many more hits one "show more" asks for, and how many a voice search starts with. */
export const PAGE_STEP = 30;
/** The documents one browse page lists. */
export const PER_PAGE = 24;
/** The rows the viewer answers for one atlas selection (`POST /api/atlas/chunks`). */
export const MAP_ROWS_MAX = 1000;
/** The viewer's own cap on a voice search's `n` (`voice_service._MAX_N`): paging past it grows nothing. */
const VOICE_MAX_N = 100;

export type ResultsLayout = 'list' | 'grid' | 'table';

const failureOf = (e: unknown): string =>
	e instanceof ApiError ? e.detail : e instanceof Error ? e.message : 'unknown error';

/** A voice search answers one hit per speaker turn, and two turns can share the chunk they overlap
 *  most: keep the best-ranked (first) hit per row, so a keyed list holds no duplicate. */
function dedupeVoiceHits(hits: VoiceHit[]): VoiceHit[] {
	const seen = new Set<string>();
	return hits.filter((hit) => {
		const key = hitKey(hit);
		if (seen.has(key)) return false;
		seen.add(key);
		return true;
	});
}

class Explore {
	/** The dataset being explored. */
	dataset = $state<string | null>(null);
	/** The search as last submitted. */
	spec = $state<SearchSpec>({ q: '', n: 100, mode: 'fts' });
	hits = $state.raw<Hit[]>([]);
	/** The item open in the player. */
	active = $state.raw<Hit | null>(null);
	loadingHits = $state(false);
	loadingMore = $state(false);
	error = $state<string | null>(null);
	/** A page answered fewer hits than asked for: nothing more to page. */
	allLoaded = $state(false);
	/** Bumped when the search is replaced from outside the search bar (a saved view, a deep link), so
	 *  the bar adopts it. */
	adopt = $state(0);

	voiceActive = $state(false);
	voiceQuery = $state.raw<VoiceQueryInfo | null>(null);
	voiceLabel = $state<string | null>(null);
	#voiceN = PAGE_STEP;
	/** The last voice answer's raw hit count: a voice page short of `n` is routine (turns that overlap
	 *  no chunk are dropped), so exhaustion is read from growth between pages. */
	#voiceRawCount = 0;
	#voiceAnchor: VoiceAnchor | null = null;
	#voiceUpload: File | null = null;
	/** The text results voice mode replaced, restored when the reader leaves it. */
	#beforeVoice: { hits: Hit[]; active: Hit | null; allLoaded: boolean } | null = null;
	#seq = 0;

	layout = $state<ResultsLayout>('list');
	docsPage = $state(1);
	/** The atlas selection shown as the results instead of a search: how many points it holds. The rows
	 *  listed are its first {@link MAP_ROWS_MAX}, the most the viewer answers for one selection. */
	mapSelection = $state<{ points: number } | null>(null);
	#mapRowids: number[] = [];

	/** Browsing documents: nothing is searched. */
	get browsing(): boolean {
		return (
			!this.spec.q &&
			!this.spec.image &&
			!this.spec.topic &&
			!this.voiceActive &&
			this.mapSelection === null
		);
	}

	/** The query the results are highlighted by: none for voice hits, which no text matched. */
	get resultsQuery(): string {
		return this.voiceActive ? '' : this.spec.q;
	}

	/** Another dataset starts with nothing searched, nothing open and the first browse page. */
	open(dataset: string): void {
		if (this.dataset === dataset) return;
		this.#seq++;
		this.dataset = dataset;
		this.spec = { q: '', n: 100, mode: 'fts' };
		this.hits = [];
		this.active = null;
		this.loadingHits = false;
		this.loadingMore = false;
		this.error = null;
		this.allLoaded = false;
		this.docsPage = 1;
		this.mapSelection = null;
		this.#leaveVoice();
		this.#beforeVoice = null;
		voiceSearch.reset();
	}

	/** The browse page's documents, read for the current dataset. */
	documents() {
		return this.dataset === null
			? null
			: documentsPage({ dataset: this.dataset, page: this.docsPage, perPage: PER_PAGE });
	}

	/** Open a browsed document in the player, from its start. */
	openDocument(doc: Document): void {
		const view = activeView();
		this.active = this.playerRow(view.docId(doc), 0, view.duration(doc) ?? 0, doc);
	}

	/** A row the player can open: the doc key, the other key fields zeroed, and a time span. */
	playerRow(docId: string, start: number, end: number, extra?: Hit): Hit {
		const view = activeView();
		const row: Hit = { ...extra, _score: 0, alignments: [] };
		for (const key of view.keyFields) if (!(key in row)) row[key] = 0;
		row[view.docKeyField] = docId;
		const time = view.descriptor.declared.time;
		if (time) {
			row[time.start] = start;
			row[time.end] = end;
		}
		return row;
	}

	/** Apply a search the bar did not submit (a saved view, a deep link): the bar adopts it. */
	apply(next: SearchSpec): void {
		void this.runSearch(next);
		this.adopt += 1;
	}

	async runSearch(next: SearchSpec): Promise<void> {
		const seq = ++this.#seq;
		this.#beforeVoice = null;
		this.#leaveVoice();
		this.mapSelection = null;
		this.spec = { ...next, n: next.n ?? PAGE_STEP };
		this.allLoaded = false;
		if (!this.spec.q && !this.spec.image && !this.spec.topic) {
			this.hits = [];
			this.loadingHits = false;
			return;
		}
		this.loadingHits = true;
		this.error = null;
		try {
			const requested = this.spec.n ?? PAGE_STEP;
			const result = await this.#search(this.spec);
			if (seq !== this.#seq) return;
			this.hits = result;
			this.active = null;
			if (result.length < requested) this.allLoaded = true;
		} catch (e) {
			if (seq !== this.#seq) return;
			this.hits = [];
			this.error = failureOf(e);
		} finally {
			if (seq === this.#seq) this.loadingHits = false;
		}
	}

	/** Ask for {@link PAGE_STEP} more hits, by searching again with a larger `n`. */
	async loadMore(): Promise<void> {
		if (this.loadingMore || this.allLoaded) return;
		this.loadingMore = true;
		const seq = ++this.#seq;
		try {
			if (this.voiceActive && (this.#voiceAnchor || this.#voiceUpload)) {
				const nextN = this.#voiceN + PAGE_STEP;
				const res = await this.#voice(nextN);
				if (res && seq === this.#seq) {
					this.hits = dedupeVoiceHits(res.hits);
					this.#voiceN = nextN;
					if (res.hits.length <= this.#voiceRawCount || nextN >= VOICE_MAX_N) {
						this.allLoaded = true;
					}
					this.#voiceRawCount = res.hits.length;
				}
				return;
			}
			const nextN = (this.spec.n ?? PAGE_STEP) + PAGE_STEP;
			const result = await this.#search({ ...this.spec, n: nextN });
			if (seq === this.#seq) {
				this.hits = result;
				this.spec = { ...this.spec, n: nextN };
				if (result.length < nextN) this.allLoaded = true;
			}
		} catch (e) {
			// The hits already shown stay; the header says the page could not be read.
			if (seq === this.#seq) this.error = `More results could not be read: ${failureOf(e)}`;
		} finally {
			this.loadingMore = false;
		}
	}

	/** Show the rows of an atlas selection as the results, by the points' stable row ids (`points` is the
	 *  selection's whole size, of which the viewer answers the first {@link MAP_ROWS_MAX}). */
	async showMapSelection(rowids: number[], points: number): Promise<void> {
		const seq = ++this.#seq;
		this.#beforeVoice = null;
		this.#leaveVoice();
		this.mapSelection = { points };
		this.#mapRowids = rowids;
		this.loadingHits = true;
		this.error = null;
		try {
			const rows = await atlasRows(rowids.slice(0, MAP_ROWS_MAX));
			if (seq !== this.#seq) return;
			this.hits = rows;
			this.active = null;
			this.allLoaded = true;
		} catch (e) {
			if (seq !== this.#seq) return;
			this.hits = [];
			this.error = failureOf(e);
		} finally {
			if (seq === this.#seq) this.loadingHits = false;
		}
	}

	/** Leave the atlas selection for the search it replaced. */
	dismissMapSelection(): void {
		void this.runSearch(this.spec);
	}

	/** Search again for what is shown, the reader's own refresh: newer rows appear. */
	async refresh(): Promise<void> {
		if (this.voiceActive) {
			const anchor = this.#voiceAnchor;
			if (anchor) await this.runVoice(anchor, this.voiceLabel ?? undefined);
			return;
		}
		if (this.mapSelection !== null) {
			await this.showMapSelection(this.#mapRowids, this.mapSelection.points);
			return;
		}
		if (this.browsing) {
			await this.documents()?.refresh();
			return;
		}
		await this.runSearch(this.spec);
	}

	/** A voice search by anchor: a speaker turn, a speaker, or a moment of a document. `fallback` is
	 *  tried once when a moment falls where no turn covers it (chunk spans and diarized turns disagree
	 *  at their edges). */
	async runVoice(anchor: VoiceAnchor, label?: string, fallback?: VoiceAnchor): Promise<void> {
		const seq = ++this.#seq;
		this.#enterVoice(label);
		this.#voiceAnchor = anchor;
		this.#voiceUpload = null;
		try {
			const requested = this.#voiceN;
			const res = await this.#voice(requested);
			if (seq !== this.#seq || res === null) return;
			this.#applyVoice(res, requested);
		} catch (e) {
			if (seq !== this.#seq) return;
			if (e instanceof ApiError && e.status === 404 && 't' in anchor) {
				if (fallback) {
					void this.runVoice(fallback, label);
					return;
				}
				this.error = 'No diarized speaker at that moment: try a segment in the Speakers timeline.';
			} else {
				this.error = failureOf(e);
			}
			if (!this.voiceQuery) this.#leaveVoice();
		} finally {
			if (seq === this.#seq) this.loadingHits = false;
		}
	}

	/** A voice search by an uploaded clip's own voiceprint. */
	async runVoiceUpload(file: File, label: string): Promise<void> {
		const seq = ++this.#seq;
		this.#enterVoice(label);
		this.#voiceAnchor = null;
		this.#voiceUpload = file;
		voiceSearch.beginUpload();
		try {
			const requested = this.#voiceN;
			const res = await this.#voice(requested);
			if (seq !== this.#seq || res === null) return;
			this.#applyVoice(res, requested);
		} catch (e) {
			if (seq !== this.#seq) return;
			this.error = failureOf(e);
			if (!this.voiceQuery) this.#leaveVoice();
		} finally {
			voiceSearch.endUpload();
			if (seq === this.#seq) this.loadingHits = false;
		}
	}

	/** Leave voice mode for the text results it replaced. */
	dismissVoice(): void {
		this.#seq++;
		this.loadingHits = false;
		this.error = null;
		this.#leaveVoice();
	}

	/** Take a voice request made from any panel (a hit's "find this voice", the speakers timeline). */
	take(request: VoiceRequest): void {
		this.#voiceN = PAGE_STEP;
		if ('upload' in request) void this.runVoiceUpload(request.upload, request.label);
		else void this.runVoice(request.anchor, request.label, request.fallback);
	}

	/** The "include the same document" setting changed: an anchored voice search runs again with it. */
	sameDocChanged(): void {
		if (this.voiceActive && this.#voiceAnchor) void this.runVoice(this.#voiceAnchor);
	}

	/** A search through the data layer, or straight to the client when it carries an image. */
	#search(spec: SearchSpec): Promise<Hit[]> {
		if (spec.image) return search(spec);
		const { image: _image, ...keyed } = spec;
		return searchHits({ dataset: this.dataset ?? '', spec: keyed });
	}

	async #voice(n: number): Promise<VoiceSimilarResponse | null> {
		if (this.#voiceUpload) return voiceSimilarUpload(this.#voiceUpload, { n });
		const anchor = this.#voiceAnchor;
		if (!anchor) return null;
		return voiceHits({
			dataset: this.dataset ?? '',
			anchor,
			n,
			excludeSameDoc: !voiceSearch.includeSameDoc,
		});
	}

	#enterVoice(label: string | undefined): void {
		this.mapSelection = null;
		if (!this.voiceActive) {
			this.#beforeVoice = { hits: this.hits, active: this.active, allLoaded: this.allLoaded };
		}
		this.voiceActive = true;
		if (label !== undefined) this.voiceLabel = label;
		this.loadingHits = true;
		this.error = null;
	}

	#applyVoice(res: VoiceSimilarResponse, requested: number): void {
		this.voiceQuery = res.query;
		this.hits = dedupeVoiceHits(res.hits);
		this.active = null;
		this.#voiceRawCount = res.hits.length;
		this.allLoaded = requested >= VOICE_MAX_N;
	}

	#leaveVoice(): void {
		this.voiceActive = false;
		this.voiceQuery = null;
		this.voiceLabel = null;
		this.#voiceAnchor = null;
		this.#voiceUpload = null;
		this.#voiceN = PAGE_STEP;
		this.#voiceRawCount = 0;
		const before = this.#beforeVoice;
		if (before) {
			this.hits = before.hits;
			this.active = before.active;
			this.allLoaded = before.allLoaded;
			this.#beforeVoice = null;
		}
	}
}

export const explore = new Explore();
voiceSearch.listen(explore);
