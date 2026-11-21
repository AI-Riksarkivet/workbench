/**
 * The canvas's controller — the reactive facade the toolbar, sidebar, layers and zoom bind to.
 *
 * The engine (`InteractionManager`, `ArrowDataPlugin`, `ImagePlugin`) is framework-agnostic: the picture's
 * plugin is the shared image stage's (`#lib/media/image`), the shapes and tools are the layer `ImageViewer`
 * adds over it, and all three are reachable only from the context `ImageViewer` builds. This class is the
 * Svelte 5 runes layer over it, one per open unit:
 *   1. it captures the engine handle (`attach`),
 *   2. mirrors the engine's plain getters and callbacks into reactive state, so the layout components
 *      stay controlled, and
 *   3. owns the layer grouping and the annotation table the sidebar reads.
 *
 * Its reads and writes go through `#lib/api/annotations.api.ts` (the app's DPoP transport, `?project=`
 * on every call): the task's draft, the one store of its work in progress, served as rows and changed by each save
 * (links included). The layout binds to the controller, never to the engine.
 */
import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import type { Table } from 'apache-arrow';
import type { CommitShape, GeometryUpdate, PixiContext, Tool } from './engine/index.ts';
import { LayerStore, buildBatchTable } from './engine/index.ts';
import type { LabelDelta, LabelOp, LabelOutcome, Selection } from '@workbench/labeling/types';
import { canonicalShapeType, engineToolsFor } from '@workbench/labeling/shape-types';
import { PRODUCERS } from '@workbench/labeling/producers';
import type { Zoomable } from '#lib/media/image/zoom.ts';
import { rowSignature } from '@workbench/labeling/history';
import { remapSpans } from './span-remap.ts';
import { proposalFields } from './proposal-rows.ts';
import { pointLength, pointSlice } from './text/codepoints.ts';
import {
	type InsertRow,
	buildSavePayload,
	makeInsertRow,
	payloadIsEmpty,
} from '@workbench/labeling/annotations-client';
import { loadAnnotations, saveAnnotations } from '#lib/api/annotations.api.ts';
import { type AnnoRow, effectiveField, projectRows, rawField } from './annotation-rows.ts';
import type { PendingEdits } from './pending.ts';
import { ontologyTools, type TaskDetail } from '#lib/projects/types.ts';
import {
	type ClassDef,
	classColours,
	classesFor,
	classForKey,
	matchClasses,
	regionClasses,
	submitBlockers,
} from './classes.ts';
import { boxAt, newTrackId, tracksFrom, type Track, type TrackRow } from './temporal/tracks.ts';
import { type CompareShape, type DiffEntry, diffVersions } from './compare/compare.ts';
import {
	type AssistModel,
	type AssistPoint,
	type AssistUnit,
	requestAssist,
	takes,
} from '#lib/api/assist.api.ts';

type TaskOntology = TaskDetail['ontology'];

export type { AnnoRow } from './annotation-rows.ts';

export type Mode = 'view' | 'edit';

/** One entry of the sidebar's list: a row, or a track standing for all its keyframes through one of them. */
export interface ListEntry {
	row: AnnoRow;
	track: Track | null;
}

/** A model's proposal waiting for a person: a `prediction` row from `model:<name>`. */
const isProposal = (r: AnnoRow): boolean =>
	r.status === 'prediction' && r.source.startsWith('model:');

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
	typeof value === 'object' && value !== null && !Array.isArray(value);

/** A message the canvas says in its live region: the outcome of a save, a relabel, a refusal. */
export interface CanvasNotice {
	tone: 'success' | 'error';
	text: string;
}

export interface BrushOptions {
	radius: number;
	erasing: boolean;
	maskMode: 'instance' | 'semantic';
	output: 'mask' | 'polygon';
}

/** Display-only image adjustments (1 = neutral on every axis) — see `imageAdjust`. */
export interface ImageAdjust {
	brightness: number;
	contrast: number;
	saturation: number;
}

const NEUTRAL_ADJUST: ImageAdjust = { brightness: 1, contrast: 1, saturation: 1 };

/** Fields the sidebar can edit inline. */
export type EditableField = 'label' | 'status' | 'group' | 'text' | 'metadata';

/** A drawn rect below this (5×5 px) is an accidental click, not an annotation. Judged here rather than
 *  in the tool, because what a click means is the canvas's to say. */
const MANUAL_MIN_AREA = 25;

/** One typed per-class attribute, as the ontology declares it (`OutputAttr` server-side). */
export interface AttrSpec {
	name: string;
	type: 'free' | 'int' | 'enum' | 'bool';
	choices: string[];
	required: boolean;
}

/** One reversible field edit (relabel / status / text / group).
 *  `before`/`after` are the effective (overlay-aware) string values. */
interface FieldEdit {
	kind: 'field';
	index: number;
	field: EditableField;
	before: string;
	after: string;
}

/** A drawn shape, reversible by hiding the appended row and dropping it from the save payload. */
interface InsertEdit {
	kind: 'insert';
	/** Row index of the appended row — an insert APPENDS, so this never shifts. */
	index: number;
	/** Position in `_inserts`, so redo can put the same row back where it was. */
	at: number;
	row: InsertRow;
}

/** A delete, reversible by un-hiding the row and dropping its id from the save payload. */
interface DeleteEdit {
	kind: 'delete';
	index: number;
	id: string;
}

/** A geometry move/resize/vertex-drag. `before` is null when the row had no pending edit yet. */
interface GeometryEdit {
	kind: 'geometry';
	index: number;
	before: GeometryUpdate | null;
	after: GeometryUpdate;
}

/** The unit of undo/redo: a field edit, a draw, a delete, a geometry drag or a link, on ONE stack, so Ctrl+Z means
 *  the same thing whatever the last action was and no edit is undoable only by reloading the session. */
/** A link drawn or removed. Reversible on the SAME stack as everything else — a relation is work,
 *  and work that only Ctrl+Z sometimes reaches is work an annotator stops trusting. */
interface LinkEdit {
	kind: 'link';
	link: AnnoLink;
	/** true = the link was CREATED (so undo removes it); false = it was deleted. */
	created: boolean;
}

/** A region's or a keyframe's times moved or resized. `before` is null when the row had no pending time edit yet. */
interface TemporalEdit {
	kind: 'temporal';
	index: number;
	before: { t_start: number; t_end: number } | null;
	after: { t_start: number; t_end: number };
}

/** A text span's edges moved (a drag of an edge, its keys, or the re-anchoring a text edit makes). `before` is null when
 *  the row had no pending offset edit yet. */
interface SpanRangeEdit {
	kind: 'span';
	index: number;
	before: { char_start: number; char_end: number } | null;
	after: { char_start: number; char_end: number };
}

/** Several edits made as one action (a span labelled into text no row held yet, a text edit and the spans it
 *  re-anchors, a version's shape taken in place of one's own): one undo reverses them all, last first. */
interface GroupEdit {
	kind: 'group';
	ops: UndoOp[];
}

type UndoOp =
	| FieldEdit
	| InsertEdit
	| DeleteEdit
	| GeometryEdit
	| LinkEdit
	| TemporalEdit
	| SpanRangeEdit
	| GroupEdit;

/** A version of the item beside the reader's own, for comparing: another replica's rows, or the model's proposals. */
export interface CompareVersion {
	/** What the panel calls it: "r2 · beta", "Model proposals". */
	name: string;
	/** Another task's rows of the unit, read from its own door; null for the proposals on this item's own rows. */
	table: Table | null;
}

/** The class picker: open over one row, because it was just drawn (Escape then decides whether it stays unlabelled) or
 *  because the person asked to relabel it. */
export interface ClassPicker {
	index: number;
	reason: 'drawn' | 'relabel';
}

/** One typed edge between two shapes, addressed by shape ID.
 *
 *  IDs, never row indices: the draft is replaced whole on every save and rows shift when one is
 *  deleted, so an index would silently re-point at a different shape. Mirrors the service's `Link`.
 */
export interface AnnoLink {
	name: string;
	from_shape: string;
	to_shape: string;
}

const STRING_FIELD_CANDIDATES = ['label', 'status', 'source', 'group', 'reviewer'];

/** The links the rows of a unit's work carry: each row's `links` is a JSON array of the relations leaving it,
 *  `[{"name", "to_shape"}]`. A cell that is not that array names no link. */
function linksOf(table: Table): AnnoLink[] {
	const ids = table.getChild('id');
	const cells = table.getChild('links');
	if (!ids || !cells) return [];
	const links: AnnoLink[] = [];
	for (let i = 0; i < table.numRows; i += 1) {
		const from = ids.get(i);
		const cell = cells.get(i);
		if (typeof from !== 'string' || typeof cell !== 'string' || cell === '') continue;
		let parsed: unknown;
		try {
			parsed = JSON.parse(cell);
		} catch {
			continue;
		}
		if (!Array.isArray(parsed)) continue;
		for (const entry of parsed as unknown[]) {
			if (!isPlainObject(entry)) continue;
			const { name, to_shape } = entry;
			if (typeof name === 'string' && typeof to_shape === 'string')
				links.push({ name, from_shape: from, to_shape });
		}
	}
	return links;
}

/** #rrggbb ⇄ 0xRRGGBB — self-contained so the panel needs no engine color util. */
function numToHex(n: number): string {
	return '#' + (n & 0xffffff).toString(16).padStart(6, '0');
}
function hexToNum(hex: string): number {
	return parseInt(hex.replace(/^#/, ''), 16) & 0xffffff;
}

export class AnnotatorController implements Zoomable {
	// ── engine handle + data ──
	ctx = $state<PixiContext | null>(null);
	table = $state<Table | null>(null);

	// ── mirrored engine state ──
	mode = $state<Mode>('edit');
	activeTool = $state<Tool>('select');
	selectedIndex = $state<number | null>(null);
	readonly selectedSet = new SvelteSet<number>();
	// true while multi-selecting (Shift/Ctrl-click) — keeps the sidebar on the list so
	// more can be picked, instead of opening the single-annotation detail.
	multiSelect = $state(false);
	/** The picture's scale, image pixels per CSS pixel (`Zoomable`): the zoom controls show it. */
	scale = $state(1);
	// True when the magnetic corner-snap tool can run — a still image is loaded
	// (video frames don't qualify). Gates their toolbar buttons.
	cvCapable = $state(false);
	// Which CV tools have finished their lazy init (wasm + edge/corner maps) — the
	// toolbar shows loading state until then; the E2E suite waits on it.
	readonly cvReady = new SvelteSet<string>();
	// Live magnetic snap state — true while the cursor is locked onto a detected corner
	// (surfaced on the toolbar; the E2E asserts snapping actually engages).
	magneticSnapped = $state(false);
	/** Bumped on every zoom and pan, so what is drawn in screen space over the canvas (the keyboard's cursor)
	 *  follows the view. */
	viewport = $state(0);
	count = $state(0);
	saving = $state(false);
	saveError = $state<string | null>(null);
	/** The last outcome worth saying (a save, a relabel, a refusal), for the canvas's live region. */
	notice = $state<CanvasNotice | null>(null);
	brushOptions = $state<BrushOptions>({
		radius: 20,
		erasing: false,
		maskMode: 'instance',
		output: 'mask',
	});
	// Image display adjustments (1 = neutral). View-only state, not annotation data: it rides a
	// ColorMatrixFilter on the page sprite and is never saved — faded manuscript scans need a
	// contrast lift to trace at all, and that lift must not survive into anyone's annotations.
	imageAdjust = $state<ImageAdjust>({ ...NEUTRAL_ADJUST });

	// ── layer grouping (mirrored from LayerStore) ──
	readonly layers = new LayerStore();
	groupByColumn = $state('label');
	readonly hiddenGroups = new SvelteSet<string>();
	readonly groupColors = new SvelteMap<string, number>();

	// Local field overlay for sidebar/list display, keyed `${index}:${field}` (the
	// canvas is updated separately via arrow.setFieldOverride). SvelteMap ⇒ edits
	// re-derive `rows` with no manual version counter.
	private readonly _overrides = new SvelteMap<string, string>();

	// Undo/redo of field edits (the review operations: relabel / accept / reject /
	// text). Geometry dirtiness is tracked separately (the engine owns geometry).
	private _undo = $state<UndoOp[]>([]);
	private _redo = $state<UndoOp[]>([]);
	private _geoDirty = $state(false);

	// Structural edits queued for the next Save: shapes drawn (onCommit) and ids
	// deleted. Flushed by save() then reconciled by _reload() — no client-side Arrow
	// surgery (which would corrupt the index-keyed overlay). The sidebar reflects
	// deletes immediately (filtered from `rows`); new shapes render after save+reload.
	private _inserts = $state<InsertRow[]>([]);
	private _deletes = $state<string[]>([]);
	// Row ids of UNDONE inserts — the sidebar's twin of the canvas hide an insert-undo performs.
	// Not `_deletes`: these rows were never saved, so nothing about them may reach the wire.
	private readonly _suppressed = new SvelteSet<string>();
	// Geometry moves of EXISTING shapes, keyed by row index (the canvas already renders
	// them via the plugin's dirty overlay; this queues them for Save).
	private readonly _geoEdits = new SvelteMap<number, GeometryUpdate>();
	// Re-anchored SPAN offsets, keyed by row index — written only by the transcription-edit remap
	// (offsets are deliberately not editable fields; a free-form edit of one is a corruption
	// vector). Display overlay and Save channel in one.
	private readonly _spanEdits = new SvelteMap<number, { char_start: number; char_end: number }>();
	// Moved or resized TIMES of existing rows (a region dragged on the time axis, a keyframe retimed), keyed by row index:
	// the display overlay and the Save's `temporal` channel in one, as the span edits are for offsets.
	private readonly _temporalEdits = new SvelteMap<number, { t_start: number; t_end: number }>();

	private _detachViewport: (() => void) | null = null;
	// POST target for Save (same URL the annotations are GET from). Null ⇒ read-only.
	private _saveUrl = $state<string | null>(null);
	// The Lance version we loaded — echoed on Save for optimistic concurrency (409).
	private _version: number | null = null;

	constructor() {
		this.layers.on(() => this._pullLayers());
		this._pullLayers();
	}

	// ── derived views the layout reads ──

	/** String columns available to group by. */
	readonly groupColumns = $derived.by<string[]>(() => {
		const t = this.table;
		if (!t) return [];
		const names = new Set(t.schema.fields.map((f) => f.name));
		return STRING_FIELD_CANDIDATES.filter((c) => names.has(c));
	});

	/** Flat rows for the sidebar list, overlay-aware (pure projection in annotation-rows). */
	readonly rows = $derived.by<AnnoRow[]>(() => {
		const t = this.table;
		if (!t) return [];
		const projected = projectRows(t, this._overrides, [...this._deletes, ...this._suppressed]);
		// The span-offset overlay (the transcription-edit remap) wins over the table, exactly as
		// the field overlay does inside projectRows — a remapped span must RENDER remapped, or the
		// highlight sits on the wrong characters until the save round-trips.
		if (this._spanEdits.size === 0 && this._temporalEdits.size === 0) return projected;
		return projected.map((r) => {
			const edit = this._spanEdits.get(r.index);
			const time = this._temporalEdits.get(r.index);
			let out = edit ? { ...r, charStart: edit.char_start, charEnd: edit.char_end } : r;
			if (time) out = { ...out, tStart: time.t_start, tEnd: time.t_end };
			return out;
		});
	});

	/** Distinct groups for the current group-by column, with counts: a track counts once, not once per keyframe. */
	readonly groups = $derived.by<{ name: string; count: number }[]>(() => {
		const col = this.groupByColumn;
		const counts = new Map<string, number>();
		for (const r of this.shapes) {
			const key = (r as unknown as Record<string, string>)[col] ?? '';
			counts.set(key, (counts.get(key) ?? 0) + 1);
		}
		return [...counts.entries()]
			.map(([name, count]) => ({ name, count }))
			.sort((a, b) => a.name.localeCompare(b.name));
	});

	/** The currently selected row (single selection), overlay-aware. */
	readonly selected = $derived.by<AnnoRow | null>(() => {
		const i = this.selectedIndex;
		if (i == null) return null;
		return this.rows.find((r) => r.index === i) ?? null;
	});

	/** The review order — predictions first, highest uncertainty first (the active-
	 *  learning queue). The ONE source both the sidebar list and accept-and-advance
	 *  read, so "what to review next" is a sort, not a recompute. */
	readonly reviewQueue = $derived.by<AnnoRow[]>(() =>
		this.rows.toSorted((a, b) => {
			const ap = a.status === 'prediction' ? 0 : 1;
			const bp = b.status === 'prediction' ? 0 : 1;
			if (ap !== bp) return ap - bp;
			return (b.uncertainty ?? -1) - (a.uncertainty ?? -1);
		}),
	);

	/** The rows of the item as a person counts them: a track's keyframes are one object, so they are one entry, at the
	 *  position of the track's first keyframe; every other row is its own. */
	readonly shapes = $derived.by<AnnoRow[]>(() => {
		// The item's own text, held in a row once a span is marked in it, is its content, not a shape on it.
		const own = (r: AnnoRow) => r.shape === 'text' && r.parentId === '' && r.source === 'dataset';
		if (this.tracks.length === 0)
			return this.rows.some(own) ? this.rows.filter((r) => !own(r)) : this.rows;
		const first = new Set(this.tracks.map((tr) => tr.keyframes[0]?.index));
		const keyframe = new Set(this.tracks.flatMap((tr) => tr.keyframes.map((k) => k.index)));
		return this.rows.filter((r) => !keyframe.has(r.index) || first.has(r.index));
	});

	/** The sidebar's list: the review queue with each track folded into one entry, whose row is its keyframe at the
	 *  playhead (so selecting it selects the box drawn there) or else its first, and which says how many keyframes it
	 *  has over what stretch. */
	readonly listEntries = $derived.by<ListEntry[]>(() => {
		const byIndex = new Map<number, Track>();
		for (const tr of this.tracks) for (const k of tr.keyframes) byIndex.set(k.index, tr);
		const seen = new Set<string>();
		const out: ListEntry[] = [];
		for (const r of this.reviewQueue) {
			const track = byIndex.get(r.index);
			if (!track) {
				out.push({ row: r, track: null });
				continue;
			}
			if (seen.has(track.group)) continue;
			seen.add(track.group);
			const at = track.keyframes.find(
				(k) => Math.abs(k.t - this.timeCursor) <= this.frameTolerance,
			);
			const index = (at ?? track.keyframes[0])?.index;
			out.push({ row: this.rows.find((x) => x.index === index) ?? r, track });
		}
		return out;
	});

	/** 1-based position of the selection in the review queue (0 if none) + the total. */
	readonly queuePos = $derived.by<{ at: number; of: number }>(() => {
		const of = this.reviewQueue.length;
		const i = this.selectedIndex;
		const idx = i == null ? -1 : this.reviewQueue.findIndex((r) => r.index === i);
		return { at: idx < 0 ? 0 : idx + 1, of };
	});

	/** The label-class palette — distinct non-empty labels present in the data, so
	 *  "quick label" needs no hardcoded class list (data-derived, like the groups). */
	// The quick-label palette — drawn-shape classes only. Chunk TAG rows (shape_type=
	// "tag", zero geometry) are annotations too but not a drawing class, so they'd just
	// pollute the palette; excluded here (they still show in the review list/table).
	// A task whose ontology names classes offers exactly those a region may take, in the ontology's order (what the
	// number keys pick); an unconstrained canvas offers the labels already on the item, since no list says otherwise.
	readonly labelClasses = $derived.by<string[]>(() =>
		this.classDefs.length > 0
			? regionClasses(this.classDefs).map((c) => c.name)
			: [
					...new Set(
						this.rows
							.filter((r) => r.shape !== 'tag')
							.map((r) => r.label)
							.filter(Boolean),
					),
				].sort((a, b) => a.localeCompare(b)),
	);

	/** The task's classes as its captured ontology declares them; empty on an unconstrained canvas. */
	classDefs = $state.raw<ClassDef[]>([]);
	/** Whether the ontology accepts an item with no annotation (`allow_empty`). */
	allowEmpty = $state(false);
	/** Each class's colour, its own or the palette's: the canvas's shapes, the picker and the lists draw with it. */
	readonly classColour = $derived(classColours(this.classDefs));
	/** A region may stay unlabelled only on a canvas whose ontology names no class; the submit refuses one otherwise
	 *  (`submitBlockers`, and the annotator's own check). */
	readonly allowsUnlabelled = $derived(this.classDefs.length === 0);
	/** The class the picker starts on: the one last chosen, while it is still one the region may take. */
	lastClass = $state<string | null>(null);
	/** The class picker, open over a row, or null. */
	picker = $state<ClassPicker | null>(null);
	/** What keeps the item from being submitted, read from the rows as they are now. */
	readonly blockers = $derived(
		submitBlockers(
			this.classDefs,
			this.rows.map((r) => ({
				index: r.index,
				shape: r.shape,
				label: r.label,
				parentId: r.parentId,
				status: r.status,
			})),
			this.allowEmpty,
		),
	);

	/** The classes the picker offers for a row: those its shape may take, or every label offered when the ontology names
	 *  none. */
	classesForRow(index: number): ClassDef[] {
		const shape = this.rows.find((r) => r.index === index)?.shape ?? '';
		// A text span takes the classes that may be drawn as text, a whole-item tag those that may be drawn as a tag, and
		// a region those that may mark it.
		if (this.classDefs.length > 0)
			return shape === 'text' || shape === 'tag'
				? classesFor(this.classDefs, shape)
				: classesFor(regionClasses(this.classDefs), shape);
		return this.labelClasses.map((name) => ({ name, colour: null, tools: [], required: false }));
	}

	/** What has been typed into the open picker: the classes it narrows to. */
	pickerQuery = $state('');
	/** Which of the classes the typed text narrows to Enter picks. */
	pickerHighlight = $state(0);
	/** The classes the open picker shows: its row's, narrowed by what was typed. */
	readonly pickerShown = $derived(
		this.picker ? matchClasses(this.classesForRow(this.picker.index), this.pickerQuery) : [],
	);

	/** Open the picker over a row: after it is drawn, or to change its class. It starts on the row's own class, else the
	 *  one picked last. */
	openPicker(index: number, reason: ClassPicker['reason'] = 'relabel'): void {
		if (this.readOnly !== null) return;
		const offered = this.classesForRow(index);
		if (offered.length === 0) return;
		const own = this.rows.find((r) => r.index === index)?.label || this.lastClass;
		this.pickerQuery = '';
		this.pickerHighlight = Math.max(
			0,
			offered.findIndex((c) => c.name === own),
		);
		this.picker = { index, reason };
	}

	/** A key while the picker is open, wherever the focus is (a key pressed the moment a shape is drawn may arrive before
	 *  the picker's field has it): a digit picks its class, a letter narrows the list, the arrows move through it, Enter
	 *  picks, Escape closes it. True when the picker took the key. */
	pickerKey(key: string): boolean {
		const open = this.picker;
		if (!open) return false;
		const offered = this.classesForRow(open.index);
		const shown = this.pickerShown;
		if (/^[1-9]$/.test(key) && this.pickerQuery === '') {
			const c = offered[Number(key) - 1];
			if (c) this.pickClass(c.name);
			return true;
		}
		if (key === 'ArrowDown' || key === 'ArrowUp') {
			if (shown.length > 0)
				this.pickerHighlight =
					(this.pickerHighlight + (key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length;
			return true;
		}
		if (key === 'Enter') {
			const c = shown[Math.min(this.pickerHighlight, shown.length - 1)];
			if (c) this.pickClass(c.name);
			return true;
		}
		if (key === 'Escape') {
			this.dismissPicker();
			return true;
		}
		if (key === 'Backspace') {
			this.pickerQuery = this.pickerQuery.slice(0, -1);
			this.pickerHighlight = 0;
			return true;
		}
		if (key.length === 1 && key !== ' ') {
			this.pickerQuery += key;
			this.pickerHighlight = 0;
			return true;
		}
		return false;
	}

	/** Give the picker's row (or the selection, from the keys) a class, and remember it as the next default. */
	pickClass(name: string): void {
		const drawn = this.picker?.reason === 'drawn';
		const target = this.picker?.index ?? this.selectedIndex;
		this.picker = null;
		// On a video a box just drawn and classed is the track worked on next: selected, so the next drag (at another
		// moment) moves it into a keyframe at once; a region of a recording is selected for its transcription. On a still
		// the list stays in front, to draw the next shape.
		// A text span is selected too, so its edges can be moved and its class changed at once.
		const shape = this.rows.find((r) => r.index === target)?.shape;
		const segment = shape === 'segment' || shape === 'text';
		if (drawn && (this.trackMode || segment) && target !== null && target !== undefined)
			this._selectRow(target);
		this.lastClass = name;
		if (target === null || target === undefined) return;
		const picked = [...this.selectedSet];
		if (picked.length > 1 && picked.includes(target)) {
			this.applyLabel(name);
			return;
		}
		this.updateField(target, 'label', name);
		this.notice = { tone: 'success', text: `Labeled “${name}”` };
	}

	/** Close the picker without a class. A region just drawn on a task whose classes are closed is not kept unlabelled:
	 *  its drawing is undone, and the live region says why. */
	dismissPicker(): void {
		const open = this.picker;
		this.picker = null;
		if (!open || open.reason !== 'drawn' || this.allowsUnlabelled) return;
		// The drawing is the last action: an insert, or a group ending in one (a span and the row of text it was the first in).
		const last = this._undo.at(-1);
		const drawn = last?.kind === 'group' ? last.ops.at(-1) : last;
		if (drawn?.kind === 'insert' && drawn.index === open.index) {
			const span = drawn.row.shape_type === 'text';
			this.undo();
			this._redo = this._redo.slice(0, -1);
			// The undo reselects the row it hid; nothing is selected now, so the next press draws rather than grabbing
			// the hidden shape's handles.
			this._selectRow(null);
			this.notice = {
				tone: 'error',
				text: span
					? 'Not kept: every span on this task needs a class. Mark it again and pick one.'
					: 'Not kept: every region on this task needs a class. Draw it again and pick one.',
			};
		}
	}

	/** Relabel the selection with the class a number key names; true when the key named one. */
	classByKey(key: string): boolean {
		// Text selected in a line, and no annotation selected: the key labels that stretch as a new span. A selected
		// annotation takes the key first: selecting one drops a pending text selection (`_selectRow`).
		const chosen = this.textSelection;
		if (chosen !== null && this.selectedIndex === null && this.readOnly === null) {
			const picked = classForKey(classesFor(this.classDefs, 'text'), key);
			if (!picked) return false;
			this.textSelection = null;
			this.addTextSpan(chosen.line, chosen.start, chosen.end, picked.name);
			return true;
		}
		const index = this.selectedIndex;
		const classes = index === null ? regionClasses(this.classDefs) : this.classesForRow(index);
		const picked = classForKey(classes, key);
		if (!picked || this.readOnly !== null) return false;
		if (index === null) {
			// Nothing selected: the key chooses the class the next region starts with.
			this.lastClass = picked.name;
			this.notice = { tone: 'success', text: `Next region: “${picked.name}”` };
			return true;
		}
		this.pickClass(picked.name);
		return true;
	}

	/** The annotations endpoint for this unit (GET/POST/versions) — null when read-only.
	 *  The compare-versions panel reads history + historical snapshots from it. */
	get annotationsUrl(): string | null {
		return this._saveUrl;
	}
	/** The current rows as id→signature, for diffing against a historical version. */
	readonly currentSignatures = $derived.by<Map<string, string>>(() => {
		const m = new Map<string, string>();
		for (const r of this.rows) m.set(r.id, rowSignature(r));
		return m;
	});

	/** The shape types the OPEN TASK permits, canonical names — empty = unconstrained.
	 *
	 *  Enforcement lives server-side at submit and stays there; this is the same contract read
	 *  EARLY so the rail can decline to offer a tool the task will refuse. Discovering a violation
	 *  after drawing is enforcement that punishes; not offering the tool is enforcement that
	 *  teaches, and the 409 remains as the backstop for any caller that is not this canvas. */
	allowedShapeTypes = $state<string[]>([]);

	/** Engine tool names the rail may show, or null for "no restriction". */
	readonly allowedTools = $derived(engineToolsFor(this.allowedShapeTypes));

	/** Whether this task wants TEXT SPANS at all.
	 *
	 *  A span is not drawable with a pointer, so it is not in `allowedTools` and the rail can never
	 *  offer it — the affordance lives in the inspector instead, beside the text it ranges over. An
	 *  unconstrained canvas allows it too: with no task there is no rule saying otherwise, and the
	 *  same reasoning that leaves every drawing tool available applies here. */
	readonly allowsTextSpans = $derived(
		this.allowedShapeTypes.length === 0 || this.allowedShapeTypes.includes('text'),
	);

	/** Can this canvas actually accept a drawn shape?
	 *
	 *  Edit mode is NOT enough, and believing it was is the defect this replaces. `ImageViewer`
	 *  loads the image, then the annotations, and only THEN calls `attach()` — so when the
	 *  annotations read fails (a stale dataset #37, an FGA denial #44, a viewer that is down #64)
	 *  the catch returns and `ctx` is never set. The controller is not bound to the engine and
	 *  nothing you draw can be committed.
	 *
	 *  Refusing to edit there is CORRECT: saving over annotations you could not read would clobber
	 *  them, and `attach` also carries the `version` that drives the OCC handshake. What was wrong
	 *  is that the UI said nothing — every tool rendered enabled, the tool you clicked reported
	 *  `aria-pressed=true`, the mode button read "Edit mode", the AI segment rendered (it gates on
	 *  exactly this flag), and dragging silently did nothing. Verified in a browser: an annotator
	 *  would draw a page of boxes before noticing.
	 *
	 *  `ctx` is the honest test because `attach()` is the only thing that sets it and `detach()` is
	 *  the only thing that clears it — so this tracks the ACTUAL binding rather than a second flag
	 *  that could drift from it. */
	readonly canDraw = $derived(this.mode === 'edit' && this.ctx !== null);

	/** Is this controller BOUND to an engine at all?
	 *
	 *  Distinct from `canDraw`, and the distinction is the point: `canDraw` is false in view mode
	 *  too, and view mode is a deliberate choice rather than a failure. A surface warning about
	 *  read-only-ness must fire only for the second case, or it cries wolf every time someone
	 *  toggles to read. */
	readonly attached = $derived(this.ctx !== null);
	/** Rows queued to be INSERTED on the next save — read-only, so a test can assert that an undo
	 *  actually removed a drawn shape from the payload rather than merely hiding it. */
	readonly pendingInserts = $derived(this._inserts.length);
	/** Rows carrying a pending geometry edit. */
	readonly pendingGeometryEdits = $derived(this._geoEdits.size);

	readonly canUndo = $derived(this._undo.length > 0);
	readonly canRedo = $derived(this._redo.length > 0);
	/** Each change of the links (a draw, a removal, an undo or redo of one): the save sends the whole set when it moved
	 *  past what the last save, or the last read, holds. */
	private _linkRevision = $state(0);
	private _savedLinkRevision = $state(0);

	/** Unsaved-edits flag: pending field edits, a canvas geometry edit, a link edit, or queued
	 *  structural inserts/deletes. */
	readonly dirty = $derived(
		this._linkRevision !== this._savedLinkRevision ||
			this._undo.length > 0 ||
			this._geoDirty ||
			this._inserts.length > 0 ||
			this._deletes.length > 0 ||
			this._spanEdits.size > 0 ||
			this._temporalEdits.size > 0,
	);
	/** Why this reader may not edit this item (their role, the task's state, another's claim), or null when they may.
	 *  While set the canvas shows the item and takes no edit: no drawing, no field, tag, link or delete, no save. */
	readOnly = $state<string | null>(null);

	readonly canSave = $derived(
		this.readOnly === null && this.dirty && !this.saving && this._saveUrl !== null,
	);

	setReadOnly(reason: string | null): void {
		const was = this.readOnly;
		this.readOnly = reason;
		if (reason === null) {
			// The item became editable (claimed, or taken up for review): drawing comes back with it.
			if (was !== null) {
				this.mode = 'edit';
				this.ctx?.plugins.interaction.setEditMode(true);
			}
			return;
		}
		this.picker = null;
		this.mode = 'view';
		this.ctx?.plugins.interaction.setEditMode(false);
		this.setTool('select');
	}

	// ── engine lifecycle ──

	/** Called by a spatial viewer once its engine + data are ready. `saveUrl` (the
	 *  annotations endpoint) enables the local-first Save; omit it for read-only. */
	attach(ctx: PixiContext, table: Table, saveUrl?: string, version?: number): void {
		this.ctx = ctx;
		this.table = table;
		this.count = table.numRows;
		this._saveUrl = saveUrl ?? null;
		this._version = version ?? null;
		this._adoptLinks(table);

		const im = ctx.plugins.interaction;
		im.setEditMode(this.mode === 'edit');
		im.setTool(this.activeTool);
		im.setBrushOptions(this.brushOptions);
		// Adjustments survive a unit change / video frame swap: a contrast lift chosen for a faded
		// volume applies to the next page too, until reset. Guarded so a neutral state never touches
		// the plugin (unit-test harnesses attach engine doubles without an image plugin).
		if (this.imageAdjusted) this._applyImageAdjust();
		this.cvCapable = im.cvCapable; // still image loaded ⇒ the magnetic CV tool is available
		this.cvReady.clear();
		im.onCvToolReady = (tool) => this.cvReady.add(tool);
		this.magneticSnapped = false;
		im.onMagneticSnap = (snapped) => (this.magneticSnapped = snapped);
		im.onSelect = (index) => {
			// The CANVAS path, and the one that matters for relations: an annotator picks the target
			// by clicking the shape, not a sidebar row (selecting a row swaps the list out for the
			// detail pane, so there is no list left to click). It goes through the link interception
			// `select()` makes, so an armed relation takes the click as its endpoint.
			if (index !== null && this._linkPick(index)) return;
			this.selectedIndex = index;
			this._mirrorSelection(im.getSelectedSet());
		};
		im.onDirtyChange = (hasDirty) => {
			if (hasDirty) this._geoDirty = true;
		};
		im.onChange = (index, geo) => {
			// On a video, a box moved off its keyframe's moment is a new keyframe of its track at the playhead.
			if (this.trackMode && this._keyframeAt(index, geo)) return;
			// drag-end geometry of an existing shape → queue for Save (canvas already updated)
			// `before` is what a redo/undo must restore: the PENDING edit if one exists, else null
			// meaning "the row's own geometry from the table".
			this._pushUndo({
				kind: 'geometry',
				index,
				before: this._geoEdits.get(index) ?? null,
				after: geo,
			});
			this._geoEdits.set(index, geo);
			this._geoDirty = true;
		};
		im.onCommit = (shape) => {
			// An armed assist model takes the gesture as its prompt: a click is a segmenter's point, a drawn box the region
			// it looks in. Nothing is drawn by hand while it is armed.
			const armed = this.assistModel;
			if (armed !== null) {
				const click = shape.type !== 'rect' || shape.width * shape.height <= MANUAL_MIN_AREA;
				if (click && takes(armed, 'points'))
					this.addAssistPoint(shape.x + shape.width / 2, shape.y + shape.height / 2);
				else if (!click && takes(armed, 'box')) {
					this.finishAssistSession();
					void this.assist({
						region: { x: shape.x, y: shape.y, width: shape.width, height: shape.height },
					});
				} else
					this.notice = {
						tone: 'error',
						text: click
							? `${armed.title} takes a box: drag one over what it should look at.`
							: `${armed.title} takes clicks: click on what it should find.`,
					};
				return;
			}
			// The tools commit every gesture, clicks included; a sub-5×5 rect is an accidental click.
			if (shape.type !== 'rect' || shape.width * shape.height > MANUAL_MIN_AREA) {
				const index = this._appendInsert(this._buildInsert(shape));
				if (index >= 0) this.openPicker(index, 'drawn');
			}
		};

		// chain (never overwrite) the image viewport hook the image stage installed, so
		// scale tracks wheel-zoom + pan + the zoom buttons alike.
		const img = ctx.plugins.image;
		const prev = img.onViewportChange;
		img.onViewportChange = (bounds) => {
			prev?.(bounds);
			this.scale = img.scale;
			this.viewport += 1;
		};
		this._detachViewport = () => {
			img.onViewportChange = prev;
		};
		this.scale = img.scale;
		this._syncLayerConfig();
		this._syncTracks();
	}

	/** Undo `attach()` — every hook it installed, not just the viewport chain.
	 *
	 *  `attach` hands the engine SEVEN closures that capture `this`: six InteractionManager
	 *  callbacks and the chained `image.onViewportChange`. Clearing only the viewport left the
	 *  other six live, so an engine event after teardown still wrote this controller's `$state`
	 *  and re-evaluated its deriveds from a destroyed effect — which is precisely what Svelte
	 *  reports as `derived_inert`. Release them all, and release them BEFORE `ctx` is nulled
	 *  (they are reached through it). */
	detach(): void {
		this._detachViewport?.();
		this._detachViewport = null;
		const im = this.ctx?.plugins.interaction;
		if (im) {
			im.onCvToolReady = undefined;
			im.onMagneticSnap = undefined;
			im.onSelect = undefined;
			im.onDirtyChange = undefined;
			im.onChange = undefined;
			im.onCommit = undefined;
		}
		this.ctx = null;
		this.table = null;
	}

	// ── toolbar / mode ──
	setTool(tool: Tool): void {
		this.activeTool = tool;
		this.ctx?.plugins.interaction.setTool(tool);
	}
	toggleMode(): void {
		if (this.readOnly !== null) return;
		this.mode = this.mode === 'edit' ? 'view' : 'edit';
		this.ctx?.plugins.interaction.setEditMode(this.mode === 'edit');
		if (this.mode === 'view') this.setTool('select');
	}
	setBrushOptions(patch: Partial<BrushOptions>): void {
		this.brushOptions = { ...this.brushOptions, ...patch };
		this.ctx?.plugins.interaction.setBrushOptions(this.brushOptions);
	}

	// ── image adjustments (display-only; never saved) ──
	get imageAdjusted(): boolean {
		const a = this.imageAdjust;
		return a.brightness !== 1 || a.contrast !== 1 || a.saturation !== 1;
	}
	setImageAdjust(patch: Partial<ImageAdjust>): void {
		this.imageAdjust = { ...this.imageAdjust, ...patch };
		this._applyImageAdjust();
	}
	resetImageAdjust(): void {
		this.imageAdjust = { ...NEUTRAL_ADJUST };
		this._applyImageAdjust();
	}
	private _applyImageAdjust(): void {
		const { brightness, contrast, saturation } = this.imageAdjust;
		this.ctx?.plugins.image.setImageAdjustments(brightness, contrast, saturation);
	}

	// ── selection ──
	select(index: number | null): void {
		// While a relation is armed, a click on a shape is an ENDPOINT, not a selection. Letting it
		// also change the selection would move the sidebar out from under the annotator mid-link.
		if (index !== null && this._linkPick(index)) return;
		this._selectRow(index);
	}

	/** Selection WITHOUT the link interception. Undo/redo must reach this directly: routing an
	 *  internal reselect through `select()` would feed the undone row into a half-drawn link. */
	private _selectRow(index: number | null): void {
		// Choosing an annotation leaves any text selected in a line: a class key now relabels the annotation.
		if (index !== null) this.textSelection = null;
		this.multiSelect = false;
		this.selectedIndex = index;
		const im = this.ctx?.plugins.interaction;
		im?.select(index);
		this._mirrorSelection(im?.getSelectedSet() ?? new Set());
	}
	/** Toggle an annotation in the multi-selection (Shift/Ctrl-click) — the `picked`
	 *  target for bulk ops ("apply to selection", Label Studio). */
	toggleSelect(index: number): void {
		this.multiSelect = true;
		const im = this.ctx?.plugins.interaction;
		im?.select(index, true);
		this._mirrorSelection(im?.getSelectedSet() ?? new Set());
		this.selectedIndex = im?.getSelectedIndex() ?? index;
	}
	clearSelection(): void {
		this.select(null);
	}
	/** Set a status on the WHOLE multi-selection — a bulk verdict through the picked seam. */
	bulkStatus(status: string): void {
		const picked = [...this.selectedSet];
		if (picked.length === 0) return;
		this.apply({
			target: { level: 'picked', indices: picked },
			producer: 'human',
			op: 'verdict',
			execution: 'interactive',
			payload: { fields: { status } },
		});
	}

	// ── relations ────────────────────────────────────────────────────────────────────────────────
	//
	// The ontology declares relations and the annotator validates them; this is where a person DRAWS one, so a
	// task declaring a REQUIRED relation (KIE, DocVQA) can be submitted.
	//
	// Two clicks, not a drag: a drag between two small boxes on a zoomed canvas is a precision task,
	// and it collides with the marquee/pan gestures the same pointer already owns.

	/** The links drawn on this unit. Rides the draft beside `shapes`. */
	links = $state<AnnoLink[]>([]);
	/** The relation being drawn, or null. Set from the task's ontology by the shell. */
	linkMode = $state<string | null>(null);
	/** The first endpoint picked, waiting for its target. */
	pendingLinkFrom = $state<string | null>(null);
	/** Relation names this task declares. Empty ⇒ the unit has no relations to draw. */
	relationNames = $state<string[]>([]);
	/** Classes the TASK declares as drawable with `text` — the only legitimate labels for a span.
	 *
	 *  NOT `labelClasses`, which is derived from labels already present in the table: on a page whose
	 *  only rows are `line`, that offers `line` as the class for a span, and a span labelled with its
	 *  parent's class is meaningless. Observed live before this existed.
	 *
	 *  Empty on an unconstrained canvas, where the caller falls back to a plain name. */
	textSpanClasses = $state<string[]>([]);

	// ── item-level classification (the Label-Studio "Choices" question, over any modality) ──
	/** Classes the TASK declares as `tag`-drawable — item-level choices, not shapes. Set from the
	 *  ontology by the shell, exactly like `textSpanClasses`. Empty ⇒ no classification bar. */
	tagClasses = $state<string[]>([]);
	/** The item's current tags — one `tag` row per active choice, deletes respected. MULTILABEL by
	 *  design (the safe superset — CVAT's image tags and LS's `choice="multiple"` behave so);
	 *  single-choice enforcement belongs to the ontology's validators at submit, not to the bar. */
	readonly activeTags = $derived(
		new Set(this.rows.filter((r) => r.shape === 'tag' && r.label).map((r) => r.label)),
	);
	// ── transcription (the row's `text` facet — the primary content, not a tool or a field) ──
	/** Per declared class: whether its regions CARRY TRANSCRIBED TEXT. Set from the ontology by
	 *  the shell, exactly like `tagClasses`. Empty ⇒ no task constraint — the workspace's
	 *  historical behaviour (the text editor offered everywhere) is the unconstrained fallback. */
	classTranscribe = $state<Record<string, boolean>>({});
	/** Whether the inspector should OFFER transcription editing for a row of `label`.
	 *
	 *  Unconstrained (no ontology, or no classes declared) ⇒ yes, everywhere — the pre-declaration
	 *  behaviour. Constrained ⇒ yes only where declared; a row whose label is NOT a declared class
	 *  (an unlabeled draft, a document row) stays editable, because no declaration covers it and
	 *  refusing on a rule that does not exist would be a wrong answer. This is what makes
	 *  "transcription" answerable in the model at last: a detection box does not offer a text
	 *  field it has no business carrying, an OCR paragraph does. */
	offersTranscription(label: string): boolean {
		// A declared class answers with its declaration; everything else (no ontology, or a label
		// no class covers) is unconstrained and stays editable.
		return this.classTranscribe[label] ?? true;
	}

	// ── per-row ATTRIBUTES (the ontology's typed per-class fields: reading order, script, …) ──
	/** Attribute declarations per CLASS, from the task's ontology — set by the shell exactly like
	 *  `tagClasses`. Empty ⇒ no attribute editor. Values are edited as STRINGS: the submit
	 *  validator parses int/bool/enum from strings, so the string IS the wire value. */
	classAttributes = $state<Record<string, AttrSpec[]>>({});
	/** The declared attributes for a row's class — [] when its class declares none. */
	attributesFor(label: string): AttrSpec[] {
		return this.classAttributes[label] ?? [];
	}
	/** A row's `metadata` as the object it holds (overlay-aware), or null when it holds no object: unparseable, an
	 *  array or a scalar. An empty column is an empty object. */
	private _metadataOf(index: number): Record<string, unknown> | null {
		const raw = this.rows.find((r) => r.index === index)?.metadata || '{}';
		let parsed: unknown;
		try {
			parsed = JSON.parse(raw);
		} catch {
			return null;
		}
		return isPlainObject(parsed) ? parsed : null;
	}
	/** A row's current attribute values for the editor: each key's value as the string the editor shows. */
	rowAttributes(index: number): Record<string, string> {
		const metadata = this._metadataOf(index) ?? {};
		return Object.fromEntries(
			Object.entries(metadata).map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v)]),
		);
	}
	/** Set (or clear, with '') ONE attribute on a row, merged into its metadata: every other key keeps its value as it
	 *  is, nested ones included. A field edit like any other, so it rides the same overlay, undo stack and Save delta.
	 *  Metadata that holds no object is refused in words rather than replaced, since writing over it would lose it. */
	setAttribute(index: number, name: string, value: string): void {
		if (this.readOnly !== null) return;
		const metadata = this._metadataOf(index);
		if (metadata === null) {
			this.notice = {
				tone: 'error',
				text: `This annotation's metadata is not an object, so ${name} cannot be set without losing it.`,
			};
			return;
		}
		const next = { ...metadata };
		if (value === '') delete next[name];
		else next[name] = value;
		this.updateField(index, 'metadata', JSON.stringify(next));
	}

	/** Toggle an item-level choice: on ⇒ append a `tag` row (rides the same insert overlay, undo
	 *  stack and Save delta as a drawn shape); off ⇒ delete that row. The chip IS the row. */
	toggleTag(label: string): void {
		if (this.readOnly !== null) return;
		const existing = this.rows.find((r) => r.shape === 'tag' && r.label === label);
		if (existing) {
			this.deleteRow(existing.index);
			return;
		}
		this._appendInsert(
			makeInsertRow({
				shape_type: 'tag',
				label,
				status: 'accepted',
				source: 'human',
			}),
		);
	}

	/** Take the links the read rows carry (each row's `links`, the relations leaving it), unless this canvas holds link
	 *  edits no save has carried yet: those are newer than the rows. */
	private _adoptLinks(table: Table): void {
		if (this._linkRevision !== this._savedLinkRevision) return;
		this.links = linksOf(table);
		this._pushLinksToCanvas();
	}

	/** Arm link-drawing for `name`, or disarm when it is already armed (a toggle, like the tools).
	 *
	 *  Arming ADOPTS the annotation currently being inspected as the source. The rail lives in that
	 *  annotation's inspector, so "link answers" plainly means "link THIS one", and the sidebar
	 *  replaces the list with the detail on selection — asking for a source click after arming would
	 *  be both redundant and, from the sidebar, impossible. The next click is the TARGET.
	 *
	 *  A canvas armed with nothing selected still works: the first click becomes the source. */
	toggleLinkMode(name: string): void {
		const arming = this.linkMode !== name;
		this.linkMode = arming ? name : null;
		this.pendingLinkFrom = arming ? (this.selected?.id ?? null) : null;
	}

	/** Feed a picked row into the pending link. Returns true when the click was CONSUMED, so the
	 *  caller does not also treat it as an ordinary selection. */
	private _linkPick(index: number): boolean {
		if (!this.linkMode) return false;
		const id = this.rows.find((r) => r.index === index)?.id;
		if (!id) return false;
		if (this.pendingLinkFrom === null) {
			this.pendingLinkFrom = id;
			return true;
		}
		if (this.pendingLinkFrom === id) {
			// Clicking the source again CANCELS. A self-link is meaningless for every relation this
			// model can express, and silently ignoring the click would read as a broken canvas.
			this.pendingLinkFrom = null;
			return true;
		}
		this.addLink({ name: this.linkMode, from_shape: this.pendingLinkFrom, to_shape: id });
		this.pendingLinkFrom = null;
		return true;
	}

	/** Add a link, undoably. A duplicate is a no-op rather than a second identical edge. */
	addLink(link: AnnoLink): void {
		if (this.readOnly !== null) return;
		const exists = this.links.some(
			(l) =>
				l.name === link.name && l.from_shape === link.from_shape && l.to_shape === link.to_shape,
		);
		if (exists) return;
		this.links = [...this.links, link];
		this._linkRevision += 1;
		this._pushLinksToCanvas();
		this._pushUndo({ kind: 'link', link, created: true });
	}

	/** Remove a link, undoably. */
	removeLink(link: AnnoLink): void {
		if (this.readOnly !== null) return;
		const before = this.links.length;
		this.links = this.links.filter(
			(l) =>
				!(l.name === link.name && l.from_shape === link.from_shape && l.to_shape === link.to_shape),
		);
		if (this.links.length === before) return;
		this._linkRevision += 1;
		this._pushLinksToCanvas();
		this._pushUndo({ kind: 'link', link, created: false });
	}

	/** The links touching a shape id — what the panel renders beside a selected annotation. */
	linksFor(id: string): AnnoLink[] {
		return this.links.filter((l) => l.from_shape === id || l.to_shape === id);
	}

	/** Push the links to the CANVAS as row indices.
	 *
	 *  The model addresses endpoints by shape ID (a draft is replaced whole on every save, so an
	 *  index would silently re-point at a different shape); the engine knows rows by position and has
	 *  never known what an annotation is called. This is the one place those two facts meet, and it
	 *  is re-run whenever either side changes — a reload renumbers rows, which would otherwise leave
	 *  every edge pointing somewhere arbitrary.
	 *
	 *  A link whose endpoint is not in the current table is DROPPED rather than clamped: a link to a
	 *  row that is not here has no honest position, and drawing it at row 0 would be a lie the
	 *  annotator could not tell from a real edge.
	 */
	private _pushLinksToCanvas(): void {
		const arrow = this.ctx?.plugins.arrow;
		if (!arrow) return;
		const byId = new Map(this.rows.map((r) => [r.id, r.index]));
		const edges: { from: number; to: number }[] = [];
		for (const link of this.links) {
			const from = byId.get(link.from_shape);
			const to = byId.get(link.to_shape);
			if (from === undefined || to === undefined) continue;
			edges.push({ from, to });
		}
		arrow.setLinks(edges);
	}

	deleteSelected(): void {
		if (this.selectedIndex == null) return;
		this.deleteRow(this.selectedIndex);
	}

	/** Delete ANY row by index — the undoable delete the canvas and the span surface share. Split
	 *  from deleteSelected so a child row (a text-span tag's ✕) can be removed without first
	 *  stealing the selection from the parent being annotated. */
	deleteRow(i: number): void {
		if (this.readOnly !== null) return;
		const t = this.table;
		const id = t ? rawField(t, 'id', i) : null;
		if (id) {
			this._deletes = [...this._deletes, id];
			this._pushUndo({ kind: 'delete', index: i, id: String(id) });
		}
		// The CANVAS half: the delete overlay hides the row from the renderer and from hit-testing at once, so the
		// canvas and the sidebar give one answer before any save reloads the table.
		this.ctx?.plugins.arrow.setDeleted(i);
		this.ctx?.plugins.arrow.sync();
		this._geoDirty = true;
		if (this.selectedIndex === i) this.select(null);
	}

	/** Forward a key to the engine's active tool — polygon/brush Enter-commit,
	 *  Escape-cancel, Backspace vertex-undo (the shell routes these while drawing). */
	forwardToolKey(key: string): void {
		this.ctx?.plugins.interaction.handleKeyDown(key);
	}

	/** Queue a new row for Save AND render it optimistically — append AT THE END
	 *  (index = numRows, so the index-keyed overlay/selection stay valid), re-render the
	 *  WebGPU canvas, re-apply pending field patches (sync re-materializes caches). */
	private _appendInsert(row: InsertRow): number {
		if (this.readOnly !== null) return -1;
		// Recorded BEFORE the append, so `index` is the row index the appended row will occupy.
		const index = this.table?.numRows ?? this._inserts.length;
		this._pushUndo({ kind: 'insert', index, at: this._inserts.length, row });
		this._inserts = [...this._inserts, row];
		const t = this.table;
		if (t) {
			// Grow the row table so the review sidebar (and temporal lane) see the new row at
			// once; when a Pixi canvas is attached, feed it the grown table too (re-render +
			// re-apply pending field patches — sync re-materializes caches).
			const next = t.concat(buildBatchTable(t.schema, [row as unknown as Record<string, unknown>]));
			const arrow = this.ctx?.plugins.arrow;
			if (arrow) {
				// An insert APPENDS, so existing row indices are unchanged and the delete overlay stays
				// valid across this swap — unlike _reload(), which renumbers and must clear it.
				arrow.load(next);
				arrow.sync();
			}
			this.table = next;
			this.count = next.numRows;
			this._reapplyOverrides();
		} else {
			this.count = this._inserts.length;
		}
		this._geoDirty = true;
		this._syncTracks();
		return index;
	}

	/** Map a committed engine shape → the queued insert row (backend NewAnnotation). */
	private _buildInsert(shape: CommitShape): InsertRow {
		// On a video a drawn box is the first keyframe of a new track at the playhead; any other shape is pinned to the
		// one frame it was drawn on (`./temporal/tracks.ts`).
		const pinned = { t_start: this.timeCursor, t_end: this.timeCursor };
		const temporal = !this.trackMode
			? {}
			: shape.type === 'rect'
				? { group: newTrackId(), ...pinned }
				: pinned;
		return makeInsertRow({
			...temporal,
			// ONE seam into the SERVICE vocabulary (`bbox`, `keypoint`, `polyline`): the names the
			// draft's submit check accepts, whatever the engine's tools call their shapes.
			shape_type: canonicalShapeType(shape.type) ?? shape.type,
			x: shape.x,
			y: shape.y,
			width: shape.width,
			height: shape.height,
			rotation: shape.rotation ?? 0,
			polygon: shape.polygon ?? [],
			mask: shape.mask ?? '',
		});
	}
	/** Whether the selection is a box a polygon can be made of: a tag, a span, a stretch of time or a point has no
	 *  outline to convert, and is offered none. */
	get convertible(): boolean {
		return this.readOnly === null && this.selected?.shape === 'bbox';
	}
	convertToPolygon(): void {
		if (!this.convertible) return;
		if (this.ctx?.plugins.interaction.convertToPolygon()) this._geoDirty = true;
	}

	// ── inline field edits (canvas + overlay) + undo/redo ──
	updateField(index: number, field: EditableField, value: string): void {
		if (this.readOnly !== null) return;
		const t = this.table;
		if (!t) return;
		const before = effectiveField(t, this._overrides, field, index) ?? '';
		if (before === value) return;
		// A TEXT edit shifts the text under every span anchored to this row: they are re-anchored in the same action, so
		// an undo puts the text and the spans back together.
		this._group(() => {
			this._pushUndo({ kind: 'field', index, field, before, after: value });
			this._setField(index, field, value);
			if (field === 'text') this._remapChildSpans(index, before, value);
		});
	}

	/** Re-anchor `parentIndex`'s child spans for an `oldText → newText` edit: shifted/stretched
	 *  spans land on the span-edit overlay (display + Save channel in one); spans whose anchored
	 *  text was edited away are DELETED rather than guessed at. Insert rows additionally update
	 *  their own pending payload — their ids are unknown to the server's patch path. */
	private _remapChildSpans(parentIndex: number, oldText: string, newText: string): void {
		const parentId = this.rows.find((r) => r.index === parentIndex)?.id;
		if (!parentId) return;
		const spans = this.rows
			.filter((r) => r.parentId === parentId && r.charStart != null && r.charStart >= 0)
			.map((r) => ({ index: r.index, start: r.charStart ?? 0, end: r.charEnd ?? 0 }));
		if (spans.length === 0) return;
		for (const s of remapSpans(oldText, newText, spans)) {
			const row = this.rows.find((r) => r.index === s.index);
			if (!row) continue;
			if (s.start === null || s.end === null) {
				this.deleteRow(s.index);
				continue;
			}
			const original = spans.find((sp) => sp.index === s.index);
			if (original && original.start === s.start && original.end === s.end) continue;
			this._setSpan(s.index, { char_start: s.start, char_end: s.end });
		}
	}

	/** Move a span's edges, on the undo stack: the offset overlay (display and the save's `spans` channel), and the
	 *  pending insert's own offsets for a span drawn since the last save. */
	private _setSpan(index: number, range: { char_start: number; char_end: number }): void {
		this._pushUndo({
			kind: 'span',
			index,
			before: this._spanEdits.get(index) ?? null,
			after: range,
		});
		this._spanEdits.set(index, range);
		this._patchInsertRow(index, range);
		this._geoDirty = true;
	}

	/** A row's own offsets from the table, before any pending edit. */
	private _tableSpan(index: number): { char_start: number; char_end: number } {
		const at = (field: string): number => {
			const v = this.table?.getChild(field)?.get(index);
			return typeof v === 'number' ? v : -1;
		};
		return { char_start: at('char_start'), char_end: at('char_end') };
	}

	/** Keep a pending insert's own fields in step with an overlay: an insert's row is what its save sends. */
	private _patchInsertRow(index: number, fields: Partial<InsertRow>): void {
		const id = this.table ? rawField(this.table, 'id', index) : null;
		if (id !== null && this._inserts.some((r) => r.id === id))
			this._inserts = this._inserts.map((r) => (r.id === id ? { ...r, ...fields } : r));
	}

	// ── text spans ──────────────────────────────────────────────────────────────────────────────────────────────────

	/** Text the person has selected in a line, which a class key labels as a new span; null when none is. */
	textSelection = $state<{ line: number | { text: string }; start: number; end: number } | null>(
		null,
	);

	/** The row holding the item's own text, writing one when no row holds it yet (an item whose text is its dataset
	 *  row's, before anything was labelled in it). Its index. */
	ensureDocument(text: string): number {
		const held = this.rows.find(
			(r) => r.shape === 'text' && r.parentId === '' && r.source === 'dataset',
		);
		if (held) return held.index;
		return this._appendInsert(
			makeInsertRow({ shape_type: 'text', text, source: 'dataset', status: 'accepted' }),
		);
	}

	/**
	 * A span over `start`..`end` of a line's text, labelled `label` or, without one, with the class picker opened on it.
	 * `line` is the row holding the text, or the item's own text when no row holds it yet (written with the span, as one
	 * action). Refused rather than clamped when the range is not inside the text: a clamped span is a range nobody asked
	 * for. Its index, or -1.
	 *
	 * The span stores its offsets and, beside them, the text they cover, so the row reads sensibly in a list; the offsets
	 * are what count, since a word can repeat within a line.
	 */
	addTextSpan(line: number | { text: string }, start: number, end: number, label?: string): number {
		if (this.readOnly !== null || this.table === null) return -1;
		const text =
			typeof line === 'number' ? (this.rows.find((r) => r.index === line)?.text ?? '') : line.text;
		if (start < 0 || end <= start || end > pointLength(text)) return -1;
		const index = this._group(() => {
			const parent = typeof line === 'number' ? line : this.ensureDocument(line.text);
			const parentId = this.rows.find((r) => r.index === parent)?.id;
			if (parentId === undefined) return -1;
			return this._appendInsert(
				makeInsertRow({
					shape_type: 'text',
					parent_id: parentId,
					char_start: start,
					char_end: end,
					text: pointSlice(text, start, end),
					label: label ?? '',
				}),
			);
		});
		if (index >= 0) {
			if (label === undefined) this.openPicker(index, 'drawn');
			else {
				this.lastClass = label;
				this._selectRow(index);
				this.notice = { tone: 'success', text: `Labeled “${label}”` };
			}
		}
		return index;
	}

	/** Move an edge of the selected span by `delta` characters (its keys); true when the selection is a span. */
	nudgeSpan(edge: 'start' | 'end', delta: number): boolean {
		const row = this.selected;
		if (
			!row ||
			row.shape !== 'text' ||
			!row.parentId ||
			row.charStart === null ||
			row.charEnd === null
		)
			return false;
		const start = edge === 'start' ? row.charStart + delta : row.charStart;
		const end = edge === 'end' ? row.charEnd + delta : row.charEnd;
		if (end > start) this.resizeSpan(row.index, start, end);
		return true;
	}

	/** Move a span's edges to `start`..`end` of its line, on the undo stack; refused outside the line. */
	resizeSpan(index: number, start: number, end: number): void {
		if (this.readOnly !== null) return;
		const row = this.rows.find((r) => r.index === index);
		const parent = row ? this.rows.find((r) => r.id === row.parentId) : undefined;
		if (!row || !parent || start < 0 || end <= start || end > pointLength(parent.text)) return;
		if (row.charStart === start && row.charEnd === end) return;
		this._group(() => {
			this._setSpan(index, { char_start: start, char_end: end });
			this.updateField(index, 'text', pointSlice(parent.text, start, end));
		});
	}
	setStatus(index: number, status: string): void {
		// A proposal is a model's: a row no model made never becomes one (the annotator refuses the edit).
		if (
			status === 'prediction' &&
			!this.rows.find((r) => r.index === index)?.source.startsWith('model:')
		)
			return;
		// Manual mode = ONE instance of the LabelOp abstraction (human · verdict ·
		// interactive · one). Routing through apply() proves the annotator isn't
		// coupled to the review flow — a model/batch producer slots into the same seam.
		this.apply({
			target: { level: 'one', index },
			producer: 'human',
			op: 'verdict',
			execution: 'interactive',
			payload: { fields: { status } },
		});
	}

	/** Set a label on the selection — one annotation, or the whole picked multi-select
	 *  (Label Studio's "apply to selection"). Flows through the same LabelOp seam. */
	applyLabel(label: string): void {
		const picked = [...this.selectedSet];
		const target: Selection =
			picked.length > 1
				? { level: 'picked', indices: picked }
				: this.selectedIndex != null
					? { level: 'one', index: this.selectedIndex }
					: { level: 'picked', indices: [] };
		this.apply({
			target,
			producer: 'human',
			op: 'set',
			execution: 'interactive',
			payload: { fields: { label } },
		});
		// The rows relabel in place; the live region confirms how many.
		const n = picked.length > 1 ? picked.length : this.selectedIndex != null ? 1 : 0;
		if (n > 0)
			this.notice = {
				tone: 'success',
				text: `Labeled ${n} annotation${n === 1 ? '' : 's'} “${label}”`,
			};
	}

	// ── review-queue navigation (accept-and-advance — the throughput loop) ──
	/** Move the selection by ±1 within the review queue (clamped). */
	selectQueueRelative(delta: number): void {
		const q = this.reviewQueue;
		if (!q.length) return;
		const pos = this.queuePos.at - 1; // queuePos is 1-based; -1 ⇒ nothing selected
		const target = pos < 0 ? 0 : Math.min(Math.max(pos + delta, 0), q.length - 1);
		const row = q[target];
		if (row) this.select(row.index);
	}
	next(): void {
		this.selectQueueRelative(1);
	}
	prev(): void {
		this.selectQueueRelative(-1);
	}
	/** Set the selection's status, then advance to the next item still needing review
	 *  (the next prediction in queue order), else the next row — the review loop. */
	acceptAndAdvance(status: string): void {
		const i = this.selectedIndex;
		if (i == null) return;
		const q = this.reviewQueue; // capture pre-change order (the row re-sorts after)
		const pos = q.findIndex((r) => r.index === i);
		const was = q[pos]?.status;
		this.setStatus(i, status);
		// What the verdict did, said in place of whatever the live region said before (an assist's answer, a save).
		if (
			this.readOnly === null &&
			was !== status &&
			(status === 'accepted' || status === 'rejected')
		)
			this.notice = {
				tone: 'success',
				text: status === 'rejected' ? 'Rejected: it is not published' : 'Kept',
			};
		const rest = pos < 0 ? q : q.slice(pos + 1);
		const nextRow = rest.find((r) => r.status === 'prediction') ?? rest[0];
		this.select(nextRow ? nextRow.index : null);
	}

	// ── the write-plane seam: dispatch a LabelOp (all 3 modes flow through here) ──
	apply(op: LabelOp): LabelOutcome {
		const spec = PRODUCERS[op.producer];
		if (!spec) return { status: 'unsupported', reason: `unknown producer '${op.producer}'` };

		// A batch op runs a deriver over a corpus selection, on the annotator's jobs door, which is a
		// service-to-service door and never published to browsers (#9 D4 merges batch jobs into flows).
		if (op.execution === 'batch') {
			return { status: 'unsupported', reason: 'batch jobs run from flows, not from the canvas' };
		}

		// Interactive + human = the manual review path (real, local-first → Save).
		if (spec.kind === 'human' && (op.op === 'set' || op.op === 'verdict')) {
			const fields = op.payload.fields ?? {};
			const deltas: LabelDelta[] = [];
			for (const index of this._resolveInteractive(op.target)) {
				for (const [field, value] of Object.entries(fields)) {
					this.updateField(index, field as EditableField, value);
				}
				deltas.push({ index, fields });
			}
			return { status: 'applied', deltas };
		}

		// An interactive model producer (assist) has no transport on this canvas.
		return {
			status: 'unsupported',
			reason: `interactive ${spec.kind} '${spec.name}' not wired yet`,
		};
	}

	/** Resolve an interactive (annotation-level) Selection to engine row indices.
	 *  Chunk-level selections are corpus-scale (batch), so client-side they fall back
	 *  to the current canvas selection. */
	private _resolveInteractive(sel: Selection): number[] {
		switch (sel.level) {
			case 'one':
				return [sel.index];
			case 'picked':
				return sel.indices;
			default:
				return [...this.selectedSet];
		}
	}

	/** Counts every edit, undo and redo: what the autosave's debounce restarts on, so it waits from the LAST edit. */
	edits = $state(0);

	/** Ops pushed while a group is being made (`_group`), which go on the stack as one. */
	private _collecting: UndoOp[] | null = null;

	/** Push an op and drop the redo branch — a new action invalidates any undone future. */
	private _pushUndo(op: UndoOp): void {
		if (this._collecting !== null) {
			this._collecting.push(op);
			return;
		}
		this._undo = [...this._undo, op];
		this._redo = [];
		this.edits += 1;
	}

	/** Run `make`, its edits one action on the undo stack. Nested, the inner edits join the outer action. */
	private _group<T>(make: () => T): T {
		if (this._collecting !== null) return make();
		const ops: UndoOp[] = [];
		this._collecting = ops;
		try {
			return make();
		} finally {
			this._collecting = null;
			if (ops.length === 1 && ops[0]) this._pushUndo(ops[0]);
			else if (ops.length > 1) this._pushUndo({ kind: 'group', ops });
		}
	}

	/** Revert the last action — field edit, draw, delete or geometry drag alike. */
	undo(): void {
		const op = this._undo.at(-1);
		if (!op) return;
		this._undo = this._undo.slice(0, -1);
		this._redo = [...this._redo, op];
		this.edits += 1;
		this._apply(op, 'undo');
	}

	/** Re-apply the last undone action. */
	redo(): void {
		const op = this._redo.at(-1);
		if (!op) return;
		this._redo = this._redo.slice(0, -1);
		this._undo = [...this._undo, op];
		this.edits += 1;
		this._apply(op, 'redo');
	}

	/** The ONE reversal path, so undo and redo can never disagree about what an op means. */
	private _apply(op: UndoOp, direction: 'undo' | 'redo'): void {
		const arrow = this.ctx?.plugins.arrow;
		switch (op.kind) {
			case 'group': {
				const ops = direction === 'undo' ? op.ops.toReversed() : op.ops;
				for (const each of ops) this._apply(each, direction);
				return;
			}
			case 'span': {
				const range = direction === 'undo' ? op.before : op.after;
				if (range === null) this._spanEdits.delete(op.index);
				else this._spanEdits.set(op.index, range);
				this._patchInsertRow(op.index, range ?? this._tableSpan(op.index));
				break;
			}
			case 'field':
				this._setField(op.index, op.field, direction === 'undo' ? op.before : op.after);
				break;
			case 'link': {
				// `created XOR undo` — undoing a creation removes, undoing a deletion restores, and
				// redo is the mirror. One expression rather than four branches that can disagree.
				const present = op.created !== (direction === 'undo');
				const gone = this.links.filter(
					(l) =>
						!(
							l.name === op.link.name &&
							l.from_shape === op.link.from_shape &&
							l.to_shape === op.link.to_shape
						),
				);
				this.links = present ? [...gone, op.link] : gone;
				this._linkRevision += 1;
				this._pushLinksToCanvas();
				break;
			}
			case 'insert': {
				// A drawn row cannot be removed from an immutable Arrow table, so undo HIDES it (the
				// same overlay a delete uses) and drops it from the save payload — the row never
				// reaches the server either way, which is what "undone" has to mean. `_suppressed`
				// is the SIDEBAR half of that hiding: `rows` cannot read the canvas overlay, and
				// without it an undone draw kept a ghost row (and a ghost tag chip) in the queue.
				if (direction === 'undo') {
					this._inserts = this._inserts.filter((_, i) => i !== op.at);
					this._suppressed.add(op.row.id);
					arrow?.setDeleted(op.index);
				} else {
					this._inserts = [...this._inserts.slice(0, op.at), op.row, ...this._inserts.slice(op.at)];
					this._suppressed.delete(op.row.id);
					arrow?.unsetDeleted(op.index);
				}
				arrow?.sync();
				this._geoDirty = true;
				break;
			}
			case 'delete': {
				if (direction === 'undo') {
					this._deletes = this._deletes.filter((id) => id !== op.id);
					arrow?.unsetDeleted(op.index);
				} else {
					this._deletes = [...this._deletes, op.id];
					arrow?.setDeleted(op.index);
				}
				arrow?.sync();
				this._geoDirty = true;
				break;
			}
			case 'temporal': {
				const time = direction === 'undo' ? op.before : op.after;
				if (time === null) this._temporalEdits.delete(op.index);
				else this._temporalEdits.set(op.index, time);
				this._patchInsert(op.index, time ?? this._tableTime(op.index));
				this._geoDirty = true;
				this._syncTracks();
				break;
			}
			case 'geometry': {
				const geo = direction === 'undo' ? op.before : op.after;
				if (geo === null) {
					// No pending edit before this drag: drop the override entirely so the row falls back
					// to its own geometry from the table, rather than being pinned to a fabricated one.
					this._geoEdits.delete(op.index);
				} else {
					this._geoEdits.set(op.index, geo);
					arrow?.setOverride(op.index, {
						x: geo.x,
						y: geo.y,
						w: geo.w,
						h: geo.h,
						polygon: geo.polygon ?? [],
					});
				}
				arrow?.sync();
				this._geoDirty = true;
				break;
			}
		}
		// A link op touches no ROW, so there is nothing to reselect for it.
		if (op.kind !== 'link') this._selectRow(op.index);
	}

	/** Apply a field value to BOTH the WebGPU canvas (arrow.setFieldOverride →
	 *  re-render) and the sidebar overlay. The single write path for edit/undo/redo. */
	private _setField(index: number, field: EditableField, value: string): void {
		this._overrides.set(`${index}:${field}`, value);
		// A row drawn since the last save travels as an insert, whose own fields are what the save sends: an edit's
		// field overlay names an id the server's patch has no row for yet.
		const id = this.table ? rawField(this.table, 'id', index) : null;
		if (id !== null && this._inserts.some((r) => r.id === id))
			this._inserts = this._inserts.map((r) => (r.id === id ? { ...r, [field]: value } : r));
		this.ctx?.plugins.arrow.setFieldOverride(index, field, value);
		this.ctx?.plugins.arrow.sync();
	}

	/** Re-apply pending field edits to the canvas — a full re-materialize (after an
	 *  optimistic append) rebuilds column caches from this.table and wipes the in-place
	 *  setFieldOverride patches, so replay them once. */
	private _reapplyOverrides(): void {
		const arrow = this.ctx?.plugins.arrow;
		if (!arrow || this._overrides.size === 0) return;
		for (const [key, value] of this._overrides) {
			const sep = key.indexOf(':');
			arrow.setFieldOverride(Number(key.slice(0, sep)), key.slice(sep + 1), value);
		}
		arrow.sync();
	}

	// ── persistence: local-first Save → Lance merge_insert (NOT sync-per-edit) ──

	/** Flush the accumulated field-edit overlay to Lance as ONE atomic version, then
	 *  reload so the display reflects the persisted (merge-reordered) rows. */
	// ── autosave ─────────────────────────────────────────────────────────────────────────────────
	//
	// Deferred until now because it needed a real answer to ONE question: what happens when the save
	// conflicts. A manual save reloads and drops the pending edits — defensible, because the user
	// pressed Save and is told to their face. Doing that on a TIMER would discard work nobody asked
	// to discard, in the background, while they were still typing. So autosave HALTS on a conflict
	// and leaves the edits alone; resolving it is a decision, and decisions belong to the annotator.

	/** Milliseconds of quiet after the last edit before an autosave fires (`/config.json`'s
	 *  `canvas.autosaveIdleSeconds`, set by the page). */
	autosaveIdleMs = 4000;

	/** Off until a caller starts it — the ad-hoc canvas and the tests must not get a background
	 *  writer they never asked for. */
	autosaveOn = $state(false);
	/** Set when autosave stops itself. Non-null means it will NOT retry until the user saves. */
	autosaveHalted = $state<string | null>(null);
	/** Epoch ms of the last successful save, for the status line. */
	lastSavedAt = $state<number | null>(null);
	private _autosaveTimer: ReturnType<typeof setTimeout> | null = null;

	/** What the status line says. One string, so the canvas can never show two answers at once. */
	readonly saveStatus = $derived.by<string>(() => {
		if (this.saving) return 'saving…';
		if (this.autosaveHalted) return this.autosaveHalted;
		if (this.dirty) return this.autosaveOn ? 'unsaved — autosaving' : 'unsaved';
		if (this.lastSavedAt !== null) return 'saved';
		return '';
	});

	/** Begin autosaving. Idempotent; the caller owns the stop. */
	startAutosave(): void {
		this.autosaveOn = true;
	}

	stopAutosave(): void {
		this.autosaveOn = false;
		this._clearAutosaveTimer();
	}

	private _clearAutosaveTimer(): void {
		if (this._autosaveTimer !== null) {
			clearTimeout(this._autosaveTimer);
			this._autosaveTimer = null;
		}
	}

	/** Called by the shell on every edit (`edits`), so the timer restarts from the last one. DEBOUNCED on
	 *  purpose: saving on every keystroke would put a write behind each character of a label, and the
	 *  transport is a whole-payload write guarded by a version — a burst of those is a conflict generator. */
	scheduleAutosave(): void {
		this._clearAutosaveTimer();
		if (!this.autosaveOn || this.autosaveHalted !== null) return;
		if (!this.canSave) return;
		this._autosaveTimer = setTimeout(() => {
			this._autosaveTimer = null;
			// A save reloads the rows; one landing while a shape or a region is held would drop the gesture, so it waits
			// for the release (measured on a video: a keyframe dragged across an autosave's reload was lost).
			if (this.gesturing()) {
				this.scheduleAutosave();
				return;
			}
			void this.save({ auto: true });
		}, this.autosaveIdleMs);
	}

	/** Whether the temporal canvas holds a region of time under the pointer: it answers while it is open. */
	timeGesture: (() => boolean) | null = null;

	/** Whether a person holds a shape or a region under the pointer now. */
	gesturing(): boolean {
		return this.timeGesture?.() === true || this.ctx?.plugins.interaction.gesturing === true;
	}

	/** Called after a save lands. */
	onSaved: (() => void) | null = null;

	async save(opts: { auto?: boolean } = {}): Promise<void> {
		const t = this.table;
		const url = this._saveUrl;
		if (!t || !url || this.saving) return;

		const payload = buildSavePayload({
			overrides: this._overrides,
			geoEdits: this._geoEdits,
			temporalEdits: this._temporalEdits,
			spanEdits: this._spanEdits,
			inserts: this._inserts,
			deletes: this._deletes,
			version: this._version,
			resolveId: (index) => rawField(t, 'id', index),
			links: this._linkRevision !== this._savedLinkRevision ? this.links : null,
		});
		if (payloadIsEmpty(payload)) return;
		// The links this save carries, by the revision of them it read: a link edited while it is in flight is newer.
		const linksSent = this._linkRevision;
		// What this save carries, by id: an edit made while it is in flight is not in it, and survives it.
		const sent = this.pendingEdits();

		this.saving = true;
		this.saveError = null;
		try {
			const status = await saveAnnotations(url, payload);
			if (status === 'signed-out') {
				// The shell's one prompt offers to sign in again, and the canvas keeps these edits through it.
				this.saveError = 'Your session ended, so nothing was saved. Your edits are still here.';
				this.notice = { tone: 'error', text: this.saveError };
				return;
			}
			if (status === 'conflict') {
				if (opts.auto) {
					// HALT rather than reload: a background timer must never discard someone's work while
					// they type. Stop, say so, and leave the edits exactly where they are.
					this.autosaveHalted = 'conflict — autosave paused, save manually to resolve';
					this.saveError =
						'Annotations changed on the server. Your edits are still here — press Save to review the conflict.';
					this.notice = { tone: 'error', text: this.saveError };
					return;
				}
				// The table advanced under us (another reviewer, a deriver): reload to the server's
				// state; the pending edits are dropped, to be re-applied on fresh data.
				this.saveError = 'Annotations changed on the server — reloaded. Re-apply your edits.';
				this.notice = { tone: 'error', text: this.saveError };
				this._savedLinkRevision = this._linkRevision;
				await this._reload();
				this._resetOverlays();
				return;
			}
			if (!opts.auto) {
				// An autosave is silent on success: a message every four idle seconds is noise.
				this.notice = { tone: 'success', text: 'Annotations saved' };
			} else if (this.notice?.tone === 'error') {
				// What a refused or failed save said is answered by this one landing: it no longer stands.
				this.notice = null;
			}
			// The links this save carried are the server's now; ones edited since stay this canvas's to send.
			this._savedLinkRevision = linksSent;
			// What was edited since this save was sent, read again once the rows are fetched and just before they replace
			// the table: an edit made while the reload was in flight is kept too.
			let later = this.pendingEdits(sent);
			await this._reload(() => {
				later = this.pendingEdits(sent);
			});
			this._resetOverlays();
			this.applyPending(later);
			this.lastSavedAt = Date.now();
			this.onSaved?.();
			// A successful save is what clears a halt: a manual one because the conflict has been looked at, an
			// autosave's because the failure it stopped for has passed.
			this.autosaveHalted = null;
		} catch (e) {
			this.saveError = e instanceof Error ? e.message : String(e);
			// A transport failure halts autosave too: retrying a broken connection every four seconds
			// buries the real error under a hundred identical messages.
			if (opts.auto) this.autosaveHalted = `autosave paused — ${this.saveError}`;
			this.notice = { tone: 'error', text: `Save failed: ${this.saveError}` };
		} finally {
			this.saving = false;
		}
	}

	/** The unsaved edits by annotation id, the shape that survives a reload; with `except`, only those not in it (an
	 *  edit to the same field, geometry or span that changed since counts as unsaved). */
	pendingEdits(except?: PendingEdits): PendingEdits {
		const t = this.table;
		const idOf = (index: number): string | null => (t ? rawField(t, 'id', index) : null);
		const sentInserts = new Set(except?.inserts.map((r) => String(r.id)));
		const sentDeletes = new Set(except?.deletes);
		const sentFields = new Map(except?.fields.map((f) => [`${f.id}:${f.field}`, f.value]));
		const sentGeometry = new Map(except?.geometry.map((g) => [g.id, JSON.stringify(g.geo)]));
		const sentSpans = new Map(except?.spans.map((s) => [s.id, `${s.char_start}:${s.char_end}`]));
		const sentTimes = new Map(except?.times.map((s) => [s.id, `${s.t_start}:${s.t_end}`]));
		const fields: PendingEdits['fields'] = [];
		for (const [key, value] of this._overrides) {
			const sep = key.indexOf(':');
			const id = idOf(Number(key.slice(0, sep)));
			const field = key.slice(sep + 1);
			if (id !== null && sentFields.get(`${id}:${field}`) !== value)
				fields.push({ id, field, value });
		}
		const geometry: PendingEdits['geometry'] = [];
		for (const [index, geo] of this._geoEdits) {
			const id = idOf(index);
			const at = { x: geo.x, y: geo.y, w: geo.w, h: geo.h, polygon: geo.polygon ?? null };
			if (id !== null && sentGeometry.get(id) !== JSON.stringify(at))
				geometry.push({ id, geo: at });
		}
		const spans: PendingEdits['spans'] = [];
		for (const [index, span] of this._spanEdits) {
			const id = idOf(index);
			if (id !== null && sentSpans.get(id) !== `${span.char_start}:${span.char_end}`)
				spans.push({ id, ...span });
		}
		const times: PendingEdits['times'] = [];
		for (const [index, time] of this._temporalEdits) {
			const id = idOf(index);
			if (id !== null && sentTimes.get(id) !== `${time.t_start}:${time.t_end}`)
				times.push({ id, ...time });
		}
		return {
			inserts: this._inserts.filter((r) => !sentInserts.has(r.id)).map((r) => ({ ...r })),
			deletes: this._deletes.filter((id) => !sentDeletes.has(id)),
			fields,
			geometry,
			spans,
			times,
		};
	}

	/** Apply unsaved edits by id to the rows as they are now: a row an edit names that is gone takes none. */
	applyPending(edits: PendingEdits): void {
		for (const row of edits.inserts)
			this._appendInsert(makeInsertRow(row as Partial<InsertRow> & { shape_type: string }));
		const t = this.table;
		if (!t) return;
		const indexOf = new Map<string, number>();
		for (let i = 0; i < t.numRows; i += 1) {
			const id = rawField(t, 'id', i);
			if (id !== null) indexOf.set(id, i);
		}
		for (const { id, field, value } of edits.fields) {
			const index = indexOf.get(id);
			if (index !== undefined) this.updateField(index, field as EditableField, value);
		}
		for (const { id, geo } of edits.geometry) {
			const index = indexOf.get(id);
			if (index === undefined) continue;
			this._geoEdits.set(index, geo);
			this.ctx?.plugins.arrow.setOverride(index, { ...geo, polygon: geo.polygon ?? [] });
			this._geoDirty = true;
		}
		for (const { id, char_start, char_end } of edits.spans) {
			const index = indexOf.get(id);
			if (index !== undefined) this._spanEdits.set(index, { char_start, char_end });
		}
		for (const { id, t_start, t_end } of edits.times) {
			const index = indexOf.get(id);
			if (index !== undefined) this._temporalEdits.set(index, { t_start, t_end });
		}
		for (const id of edits.deletes) {
			const index = indexOf.get(id);
			if (index !== undefined) this.deleteRow(index);
		}
		this.ctx?.plugins.arrow.sync();
		this.edits += 1;
	}

	/** Take the open task's rules from its captured ontology. */
	configure(ontology: TaskOntology | undefined): void {
		const classes = ontology?.classes ?? [];
		this.allowedShapeTypes = ontologyTools(ontology);
		this.relationNames = (ontology?.relations ?? []).map((r) => r.name);
		this.textSpanClasses = classes.filter((c) => c.tools.includes('text')).map((c) => c.name);
		this.tagClasses = classes.filter((c) => c.tools.includes('tag')).map((c) => c.name);
		this.classAttributes = Object.fromEntries(
			classes
				.filter((c) => c.attributes.length > 0)
				.map((c) => [
					c.name,
					c.attributes.map((a) => ({
						name: a.name,
						type: a.type === 'int' || a.type === 'enum' || a.type === 'bool' ? a.type : 'free',
						choices: a.choices,
						required: a.required,
					})),
				]),
		);
		this.classTranscribe = Object.fromEntries(classes.map((c) => [c.name, c.transcribe]));
		this.classDefs = classes.map((c) => ({
			name: c.name,
			colour: c.colour ?? null,
			tools: c.tools,
			required: c.required,
		}));
		this.allowEmpty = ontology?.allow_empty ?? false;
		// The shapes are coloured by their class (the layers group by `label`), each class its own colour.
		for (const [name, hex] of this.classColour) this.layers.setColor(name, hexToNum(hex));
	}

	/** Drop every pending local edit (after a flush or a conflict reload). */
	private _resetOverlays(): void {
		this._undo = [];
		this._redo = [];
		this._inserts = [];
		this._deletes = [];
		this._suppressed.clear();
		this._geoEdits.clear();
		this._spanEdits.clear();
		this._temporalEdits.clear();
		this._geoDirty = false;
	}

	/** Re-fetch the persisted annotations into the canvas + controller, clearing the
	 *  now-flushed overlay. merge_insert reorders rows, so selection is dropped. */
	private async _reload(beforeSwap?: () => void): Promise<void> {
		const url = this._saveUrl;
		if (!url) return;
		// A refused reload returns silently: the write already committed, so save() still clears the
		// flushed overlays. An unreachable one throws to save()'s catch, which keeps the pending edits
		// and the version for a retry.
		const loaded = await loadAnnotations(url);
		if (!loaded.ok) {
			if (loaded.status === 0) throw new Error(loaded.detail);
			return;
		}
		const { table, version } = loaded.data;
		beforeSwap?.();
		this._version = version;
		this._overrides.clear();
		const ctx = this.ctx;
		if (ctx) {
			ctx.plugins.arrow.clearOverrides(); // drop stale geometry overrides — table is authoritative
			// The delete overlay is positional, so a reload renumbering rows would leave it hiding an
			// unrelated shape. The reloaded table has the deletions applied already.
			ctx.plugins.arrow.clearDeleted();
			ctx.plugins.arrow.load(table);
			ctx.plugins.arrow.sync();
		}
		this.table = table;
		this.count = table.numRows;
		// AFTER `this.table` is swapped: the id→index map is built from `rows`, which reads the new
		// table. Re-pushing here is what stops a reload — which renumbers every row — from leaving
		// each edge pointing at whatever now happens to sit at its old index.
		this._adoptLinks(table);
		this._pushLinksToCanvas();
		this._syncTracks();
		this.select(null);
	}

	// ── time: a recording's regions and a video's tracks ─────────────────────────────────────────────────────────────

	/** Called by a temporal viewer (a recording has no picture to draw on) once its annotations are read: the rows, and
	 *  the door a save writes to with the version they were read at. */
	attachTable(table: Table, saveUrl: string, version: number): void {
		this.table = table;
		this.count = table.numRows;
		this._saveUrl = saveUrl;
		this._version = version;
		this._adoptLinks(table);
	}

	/** Release what `attachTable` took. */
	detachTable(): void {
		if (this.ctx !== null) return;
		this.table = null;
	}

	/** Whether this canvas takes an edit of time now: an editable item, in edit mode, its rows read. */
	readonly canEditTime = $derived(
		this.readOnly === null && this.mode === 'edit' && this.table !== null,
	);

	/** A row's own times from the table, before any pending edit. */
	private _tableTime(index: number): { t_start: number; t_end: number } {
		const t = this.table;
		const at = (field: string): number => {
			const v = t?.getChild(field)?.get(index);
			return typeof v === 'number' ? v : 0;
		};
		return { t_start: at('t_start'), t_end: at('t_end') };
	}

	/** Keep a pending insert's own times in step with its time overlay: an insert's row is what its save sends. */
	private _patchInsert(index: number, time: { t_start: number; t_end: number }): void {
		const id = this.table ? rawField(this.table, 'id', index) : null;
		if (id !== null && this._inserts.some((r) => r.id === id))
			this._inserts = this._inserts.map((r) => (r.id === id ? { ...r, ...time } : r));
	}

	/** Add a region of the recording from `start` to `end` seconds, and open the class picker on it. Its index, or -1. */
	addSegment(start: number, end: number, init: Partial<InsertRow> = {}): number {
		if (!this.canEditTime || !(end > start)) return -1;
		const index = this._appendInsert(
			makeInsertRow({ shape_type: 'segment', t_start: start, t_end: end, ...init }),
		);
		if (index >= 0 && init.label === undefined) this.openPicker(index, 'drawn');
		return index;
	}

	/** Move or resize a row's times: an edit on the undo stack like any other, saved on the `temporal` channel. */
	updateSegmentTime(index: number, t_start: number, t_end: number): void {
		if (!this.canEditTime || !(t_end >= t_start)) return;
		const before = this._temporalEdits.get(index) ?? null;
		const current = before ?? this._tableTime(index);
		if (current.t_start === t_start && current.t_end === t_end) return;
		const after = { t_start, t_end };
		this._pushUndo({ kind: 'temporal', index, before, after });
		this._temporalEdits.set(index, after);
		this._patchInsert(index, after);
		this._geoDirty = true;
		this._syncTracks();
	}

	/** Where the playhead is, in seconds: a shape drawn on a video is pinned to it, and its tracks are drawn at it. */
	timeCursor = $state(0);
	/** On a video the rows sharing a `group` are one object's keyframes (`./temporal/tracks.ts`): the canvas draws each
	 *  track's box at the playhead, interpolated between its keyframes. Off for a still, where every row sits at 0. */
	trackMode = $state(false);

	setTrackMode(on: boolean): void {
		this.trackMode = on;
		this._syncTracks();
	}

	setTimeCursor(seconds: number): void {
		if (seconds === this.timeCursor) return;
		this.timeCursor = seconds;
		this._syncTracks();
	}

	/** The rows as tracks: every group with a time, its keyframes in time order. */
	readonly tracks = $derived.by<Track[]>(() => {
		if (!this.trackMode) return [];
		const t = this.table;
		const rows: TrackRow[] = [];
		for (const r of this.rows) {
			if (!r.group || r.tStart === null) continue;
			const geo = this._geoEdits.get(r.index);
			const num = (field: string): number | null => {
				const v = t?.getChild(field)?.get(r.index);
				return typeof v === 'number' ? v : null;
			};
			rows.push({
				id: r.id,
				index: r.index,
				group: r.group,
				tStart: r.tStart,
				x: geo ? geo.x : num('x'),
				y: geo ? geo.y : num('y'),
				width: geo ? geo.w : num('width'),
				height: geo ? geo.h : num('height'),
				label: r.label,
				shape: r.shape,
			});
		}
		return tracksFrom(rows);
	});

	/** The keyframe of the row's track at the playhead, within half a frame, or null. */
	private _keyframeNear(group: string, at: number): { index: number } | null {
		const track = this.tracks.find((tr) => tr.group === group);
		const kf = track?.keyframes.find((k) => Math.abs(k.t - at) <= this.frameTolerance);
		return kf ? { index: kf.index } : null;
	}

	/** Half a frame at the video's rate: two moments closer than this are one frame. Set by the video viewer. */
	frameTolerance = 0.02;

	/** The time canvas's keys, while an item with time is open: the page hands them every key the focus did not take, so
	 *  the frame a box was just drawn on steps and plays as the strip does. True when the key was one of them. */
	timeKeys = $state.raw<((e: KeyboardEvent) => boolean) | null>(null);

	/** A box of a track moved at the playhead. On its keyframe's own moment it is that keyframe's edit (false: the
	 *  caller queues it as a geometry edit); elsewhere it becomes a new keyframe at the playhead (true). */
	private _keyframeAt(index: number, geo: GeometryUpdate): boolean {
		const row = this.rows.find((r) => r.index === index);
		if (!row || !row.group) return false;
		const at = this.timeCursor;
		const near = this._keyframeNear(row.group, at);
		const arrow = this.ctx?.plugins.arrow;
		if (near !== null && near.index === index) return false;
		// The drag moved the track's representative row on the canvas; the moved box belongs to the new keyframe, so the
		// representative keeps its own geometry.
		arrow?.setOverride(index, { ...this._geometryOf(index), polygon: [] });
		if (near !== null) {
			this._pushUndo({
				kind: 'geometry',
				index: near.index,
				before: this._geoEdits.get(near.index) ?? null,
				after: geo,
			});
			this._geoEdits.set(near.index, geo);
			this._geoDirty = true;
			this._syncTracks();
			return true;
		}
		this._appendInsert(
			makeInsertRow({
				shape_type: row.shape || 'bbox',
				x: geo.x,
				y: geo.y,
				width: geo.w,
				height: geo.h,
				polygon: geo.polygon ?? [],
				t_start: at,
				t_end: at,
				label: row.label,
				group: row.group,
			}),
		);
		this._syncTracks();
		return true;
	}

	/** A row's geometry: its pending edit, else the table's. */
	private _geometryOf(index: number): { x: number; y: number; w: number; h: number } {
		const geo = this._geoEdits.get(index);
		if (geo) return { x: geo.x, y: geo.y, w: geo.w, h: geo.h };
		const t = this.table;
		const at = (field: string): number => {
			const v = t?.getChild(field)?.get(index);
			return typeof v === 'number' ? v : 0;
		};
		return { x: at('x'), y: at('y'), w: at('width'), h: at('height') };
	}

	/** Draw every track at the playhead: each live track's box on one of its rows, every other keyframe hidden, and a
	 *  track outside its lifespan not drawn at all (`boxAt`). */
	private _syncTracks(): void {
		const arrow = this.ctx?.plugins.arrow;
		if (!arrow) return;
		if (!this.trackMode) {
			arrow.setTrackDisplay(new Map(), new Set());
			arrow.sync();
			return;
		}
		const overrides = new Map<number, { x: number; y: number; w: number; h: number }>();
		const hidden = new Set<number>();
		for (const track of this.tracks) {
			const box = boxAt(track, this.timeCursor, this.frameTolerance);
			// The row that stands for the track: its keyframe at the playhead when there is one, so a drag there edits it.
			const near = track.keyframes.find(
				(k) => Math.abs(k.t - this.timeCursor) <= this.frameTolerance,
			);
			const shown = near ?? track.keyframes[0];
			for (const kf of track.keyframes) if (kf !== shown || box === null) hidden.add(kf.index);
			if (shown && box)
				overrides.set(shown.index, { x: box.x, y: box.y, w: box.width, h: box.height });
		}
		// A shape pinned to one frame (a polygon, a point) is drawn on that frame alone.
		const tracked = new Set(this.tracks.flatMap((tr) => tr.keyframes.map((k) => k.index)));
		for (const r of this.rows) {
			if (tracked.has(r.index) || r.tStart === null || r.shape === 'segment' || r.shape === 'tag')
				continue;
			if (Math.abs(r.tStart - this.timeCursor) > this.frameTolerance) hidden.add(r.index);
		}
		arrow.setTrackDisplay(overrides, hidden);
		arrow.sync();
	}

	/** Copy the selected track's box at the playhead to the frame `step` seconds on, as a keyframe there. */
	copyToNextFrame(step: number): void {
		const index = this.selectedIndex;
		if (index === null || !this.trackMode || this.readOnly !== null) return;
		const row = this.rows.find((r) => r.index === index);
		const track = row ? this.tracks.find((tr) => tr.group === row.group) : undefined;
		const box = track ? boxAt(track, this.timeCursor, this.frameTolerance) : null;
		if (!row || !track || !box) return;
		const at = this.timeCursor + step;
		const near = this._keyframeNear(track.group, at);
		const geo = { x: box.x, y: box.y, w: box.width, h: box.height, polygon: null };
		if (near !== null) {
			this._pushUndo({
				kind: 'geometry',
				index: near.index,
				before: this._geoEdits.get(near.index) ?? null,
				after: geo,
			});
			this._geoEdits.set(near.index, geo);
			this._geoDirty = true;
		} else {
			this._appendInsert(
				makeInsertRow({
					shape_type: row.shape || 'bbox',
					x: box.x,
					y: box.y,
					width: box.width,
					height: box.height,
					t_start: at,
					t_end: at,
					label: row.label,
					group: track.group,
				}),
			);
		}
		this._syncTracks();
	}

	/** A row's box on the screen, in CSS pixels of the canvas, at the view as it is now (a track's box at the playhead);
	 *  null without a canvas or a box. Reads `viewport`, so a caller deriving from it follows zoom and pan. */
	screenBoxOf(index: number): { x: number; y: number; width: number; height: number } | null {
		void this.viewport;
		const ctx = this.ctx;
		if (!ctx) return null;
		// A region of the time line and a text span have no box on the picture: their pickers open where they are drawn.
		const shape = this.rows.find((r) => r.index === index)?.shape;
		if (shape === 'segment' || shape === 'text') return null;
		const track = this.tracks.find((tr) => tr.keyframes.some((k) => k.index === index));
		const box = track ? boxAt(track, this.timeCursor, this.frameTolerance) : null;
		const g = box ? { x: box.x, y: box.y, w: box.width, h: box.height } : this._geometryOf(index);
		const a = ctx.plugins.interaction.worldToScreen(g.x, g.y);
		const b = ctx.plugins.interaction.worldToScreen(g.x + g.w, g.y + g.h);
		return {
			x: Math.min(a.x, b.x),
			y: Math.min(a.y, b.y),
			width: Math.abs(b.x - a.x),
			height: Math.abs(b.y - a.y),
		};
	}

	/** Mark the selected track as leaving the picture at the playhead (an outside keyframe: a box of no area, the
	 *  track absent from here to its next keyframe), or, on one already outside, bring it back as the box before it. */
	toggleOutside(): void {
		const index = this.selectedIndex;
		if (index === null || !this.trackMode || this.readOnly !== null) return;
		const row = this.rows.find((r) => r.index === index);
		const track = row ? this.tracks.find((tr) => tr.group === row.group) : undefined;
		if (!row || !track) return;
		const at = this.timeCursor;
		const near = this._keyframeNear(track.group, at);
		const before = [...track.keyframes].reverse().find((k) => k.t < at && !k.outside);
		if (near !== null) {
			const kf = track.keyframes.find((k) => k.index === near.index);
			const back = kf?.outside && before ? before : null;
			const geo = back
				? { x: back.x, y: back.y, w: back.width, h: back.height, polygon: null }
				: { x: kf?.x ?? 0, y: kf?.y ?? 0, w: 0, h: 0, polygon: null };
			this._pushUndo({
				kind: 'geometry',
				index: near.index,
				before: this._geoEdits.get(near.index) ?? null,
				after: geo,
			});
			this._geoEdits.set(near.index, geo);
			this._geoDirty = true;
		} else {
			const box = boxAt(track, at, this.frameTolerance);
			this._appendInsert(
				makeInsertRow({
					shape_type: row.shape || 'bbox',
					x: box?.x ?? 0,
					y: box?.y ?? 0,
					width: 0,
					height: 0,
					t_start: at,
					t_end: at,
					label: row.label,
					group: track.group,
				}),
			);
		}
		this._syncTracks();
	}

	/** Move a keyframe to another moment (its lane's drag), on the undo stack. */
	retimeKeyframe(index: number, at: number): void {
		this.updateSegmentTime(index, at, at);
	}

	// ── assist: a model's proposals on the item ─────────────────────────────────────────────────────────────────────────

	/** The unit the canvas labels, which the assist doors name: set by the shell. */
	unit: AssistUnit | null = null;
	/** The model armed on the canvas: its next click (a model that takes points) or box (one that takes a box) is its
	 *  prompt rather than a drawn shape. Null when none. */
	assistModel = $state.raw<AssistModel | null>(null);
	/** What a model that takes words is asked to find. */
	assistPrompt = $state('');
	/** A click session: every click so far, each request carrying them all (the endpoint keeps no session), so one more
	 *  click refines the same object. */
	assistPoints = $state<AssistPoint[]>([]);
	/** Whether the next click says "this" (true) or "not this" (false). */
	assistPointPositive = $state(true);
	/** Proposals scored below this never reach the item (0 keeps every one); how many it held back is said. */
	assistMinConfidence = $state(0);
	/** The model being asked, while its answer is on its way; null when nothing is. */
	assistAsking = $state<string | null>(null);
	readonly assistBusy = $derived(this.assistAsking !== null);
	/** Why the last ask got no proposals: the annotator's own words for an endpoint that was slow, failed or refused it,
	 *  shown until the next ask. Never an empty answer. */
	assistError = $state<string | null>(null);
	/** The rows the session's last answer added, which the next answer replaces. */
	private _assistSessionRows: { id: string; index: number }[] = [];

	/** Arm a model (its clicks and boxes become its prompts) or, with null, put it away. Changing it ends a session,
	 *  keeping its last answer. */
	setAssistModel(model: AssistModel | null): void {
		if (model?.name !== this.assistModel?.name) this.finishAssistSession();
		this.assistModel = model;
		if (model !== null) this.setTool('rect');
	}

	/** One more click of the session, and the model asked again with every click so far. */
	addAssistPoint(x: number, y: number): void {
		if (this.assistModel === null) return;
		this.assistPoints = [...this.assistPoints, { x, y, positive: this.assistPointPositive }];
		void this.assist({ points: this.assistPoints });
	}

	/** End the session, keeping its last answer: the next click starts a new object. */
	finishAssistSession(): void {
		this.assistPoints = [];
		this.assistPointPositive = true;
		this._assistSessionRows = [];
	}

	/**
	 * Ask the armed model (or `model`) for proposals on the unit. They arrive as `prediction` rows from
	 * `model:<name>`, each naming the model's version and its score, on the item's undo stack and saved like any shape,
	 * for the person to keep (A) or reject (R), or all at once (Alt+A, Alt+R). On a video they are pinned to the
	 * playhead, a box as a new track's first keyframe. A refinement of a session replaces the session's previous answer,
	 * once the new one is in hand. An ask the annotator refuses (no endpoint, a slow or failing one, a refused ask) is
	 * said in its words, as an error.
	 */
	async assist(
		opts: {
			model?: AssistModel;
			region?: { x: number; y: number; width: number; height: number };
			points?: AssistPoint[];
		} = {},
	): Promise<void> {
		const model = opts.model ?? this.assistModel;
		const unit = this.unit;
		if (model === null || unit === null || this.readOnly !== null || this.assistBusy) return;
		this.assistAsking = model.title || model.name;
		this.assistError = null;
		try {
			const answer = await requestAssist(unit, {
				model: model.name,
				prompt: takes(model, 'text') ? this.assistPrompt.trim() : '',
				region: opts.region ?? null,
				points: opts.points ?? [],
			});
			if (!answer.ok) {
				this.assistError = answer.detail;
				this.notice = { tone: 'error', text: `Assist failed: ${answer.detail}` };
				return;
			}
			const { proposals, dropped } = answer.data;
			const kept = proposals.filter(
				(p) => this.assistMinConfidence <= 0 || p.score >= this.assistMinConfidence,
			);
			const refining = (opts.points?.length ?? 0) > 0;
			const added = this._group(() => {
				if (refining) this._retractSessionRows();
				return kept.map((p) => {
					const fields = proposalFields(answer.data, p);
					const temporal = !this.trackMode
						? {}
						: {
								t_start: this.timeCursor,
								t_end: this.timeCursor,
								...(fields.shape_type === 'bbox' ? { group: newTrackId() } : {}),
							};
					const row = makeInsertRow({ ...temporal, ...fields });
					return { id: row.id, index: this._appendInsert(row) };
				});
			});
			if (refining) this._assistSessionRows = added;
			const below = proposals.length - kept.length;
			const said = [
				`${kept.length} proposal${kept.length === 1 ? '' : 's'} from ${answer.data.model} ${answer.data.version}${kept.length > 0 ? ': A keeps, R rejects, Alt+A keeps all, Alt+R removes all' : ''}`,
				below > 0 ? `${below} below the ${this.assistMinConfidence.toFixed(2)} threshold` : '',
				dropped.length > 0 ? `${dropped.length} outside this task's classes (${dropped[0]})` : '',
			].filter(Boolean);
			this.notice = { tone: kept.length > 0 ? 'success' : 'error', text: said.join(' · ') };
		} finally {
			this.assistAsking = null;
		}
	}

	// ── proposals: a model's shapes nobody kept or removed yet ───────────────────────────────────────────────────────

	/** The item's proposals nobody kept or removed: what keeps it from being submitted (`submitBlockers`), as the
	 *  annotator counts them (every `prediction` row). */
	readonly proposals = $derived(this.rows.filter((r) => r.status === 'prediction'));

	/** Keep every proposal of the item (each becomes the person's, still naming the model that made it): one action, one
	 *  undo. */
	keepAllProposals(): void {
		const rows = this.proposals;
		if (this.readOnly !== null || rows.length === 0) return;
		this._group(() => {
			for (const r of rows) this.updateField(r.index, 'status', 'accepted');
		});
		this.notice = {
			tone: 'success',
			text: `Kept ${rows.length} proposal${rows.length === 1 ? '' : 's'}`,
		};
	}

	/** Remove every proposal of the item: one action, one undo. */
	removeAllProposals(): void {
		const rows = this.proposals;
		if (this.readOnly !== null || rows.length === 0) return;
		this._group(() => {
			for (const r of rows) this.deleteRow(r.index);
		});
		this.notice = {
			tone: 'success',
			text: `Removed ${rows.length} proposal${rows.length === 1 ? '' : 's'}`,
		};
	}

	/** Take back the rows the session's last answer added, as an undone draw is: never saved, so nothing reaches the
	 *  wire. */
	private _retractSessionRows(): void {
		const arrow = this.ctx?.plugins.arrow;
		const ids = new Set(this._assistSessionRows.map((r) => r.id));
		if (ids.size === 0) return;
		this._inserts = this._inserts.filter((r) => !ids.has(r.id));
		for (const row of this._assistSessionRows) {
			this._suppressed.add(row.id);
			arrow?.setDeleted(row.index);
		}
		arrow?.sync();
		this._assistSessionRows = [];
	}

	// ── compare: another version of the item beside the reader's own ────────────────────────────────────────────────

	/** The version shown beside the item's own rows, or null. */
	compare = $state.raw<CompareVersion | null>(null);

	/** A row of a table as the comparison reads it. */
	private _compareShape(table: Table, row: AnnoRow, texts: Map<string, string>): CompareShape {
		const num = (field: string): number => {
			const v = table.getChild(field)?.get(row.index);
			return typeof v === 'number' ? v : 0;
		};
		const own = table === this.table ? this._geoEdits.get(row.index) : undefined;
		const box =
			row.shape === 'segment' || row.shape === 'tag' || row.shape === 'text'
				? null
				: own
					? { x: own.x, y: own.y, w: own.w, h: own.h }
					: { x: num('x'), y: num('y'), w: num('width'), h: num('height') };
		const parentText = texts.get(row.parentId);
		return {
			index: row.index,
			id: row.id,
			shape: row.shape,
			label: row.label,
			box,
			time: row.tStart !== null && row.tEnd !== null ? { start: row.tStart, end: row.tEnd } : null,
			span:
				row.shape === 'text' &&
				parentText !== undefined &&
				row.charStart !== null &&
				row.charEnd !== null
					? { text: parentText, start: row.charStart, end: row.charEnd }
					: null,
		};
	}

	/** The rows the other version holds, as the canvas shows them. */
	readonly compareRows = $derived.by<AnnoRow[]>(() => {
		const version = this.compare;
		if (version === null) return [];
		if (version.table === null) return this.rows.filter((r) => isProposal(r));
		return projectRows(version.table, new Map(), []);
	});

	/** The item's own rows paired with the other version's, each pair saying how it stands. */
	readonly compareDiff = $derived.by<DiffEntry[]>(() => {
		const version = this.compare;
		const table = this.table;
		if (version === null || table === null) return [];
		const marks = (r: AnnoRow) => !(r.shape === 'text' && r.parentId === '');
		const textsOf = (rows: readonly AnnoRow[]) => new Map(rows.map((r) => [r.id, r.text]));
		const mineRows = (
			version.table === null ? this.rows.filter((r) => !isProposal(r)) : this.rows
		).filter(marks);
		const theirTable = version.table ?? table;
		const theirRows = this.compareRows.filter(marks);
		const mine = mineRows.map((r) => this._compareShape(table, r, textsOf(this.rows)));
		const theirs = theirRows.map((r) =>
			this._compareShape(
				theirTable,
				r,
				textsOf(version.table === null ? this.rows : this.compareRows),
			),
		);
		return diffVersions(mine, theirs);
	});

	/**
	 * Take the other version's shape into the item, in place of the item's own where the two are one shape differing.
	 * A proposal on the item's own rows is kept (accepted); another replica's shape is copied in as the reader's own
	 * accepted row, its source kept, a span re-anchored into the item's text holding the same words. One action on the
	 * undo stack. True when something was taken.
	 */
	takeFromCompare(entry: DiffEntry): boolean {
		const version = this.compare;
		const table = this.table;
		const theirs = entry.theirs;
		if (version === null || table === null || theirs === null || this.readOnly !== null)
			return false;
		return this._group(() => {
			if (version.table === null) {
				this.updateField(theirs.index, 'status', 'accepted');
			} else if (!this._copyIn(version.table, theirs)) return false;
			if (entry.kind === 'changed' && entry.mine !== null) this.deleteRow(entry.mine.index);
			this.notice = {
				tone: 'success',
				text: `Took ${theirs.label || theirs.shape} from ${version.name}`,
			};
			return true;
		});
	}

	/** Copy a row of another version's table into the item as an accepted row of the reader's. */
	private _copyIn(from: Table, shape: CompareShape): boolean {
		const row = from.get(shape.index) as Record<string, unknown> | null;
		if (!row) return false;
		const str = (field: string): string =>
			typeof row[field] === 'string' ? (row[field] as string) : '';
		const num = (field: string): number =>
			typeof row[field] === 'number' ? (row[field] as number) : 0;
		const polygon =
			row['polygon'] == null ? [] : Array.from(row['polygon'] as ArrayLike<number>, Number);
		// A model's row taken from another replica still says which model, at which version and score, made it; one
		// that cannot say both is taken in as the reader's own, since the annotator refuses a model's row without them.
		const version = str('model_version');
		const score = typeof row['confidence'] === 'number' ? row['confidence'] : null;
		const named = str('source') || 'human';
		const madeBy = named.startsWith('model:') && version !== '' && score !== null;
		const source = named.startsWith('model:') && !madeBy ? 'human' : named;
		let parent = '';
		if (shape.span !== null) {
			const words = shape.span.text;
			const line = this.rows.find(
				(r) => r.parentId === '' && r.shape !== 'tag' && r.text === words,
			);
			if (!line) {
				this.notice = {
					tone: 'error',
					text: 'Not taken: this item holds no text with the same words for the span to range into.',
				};
				return false;
			}
			parent = line.id;
		}
		const index = this._appendInsert(
			makeInsertRow({
				shape_type: shape.shape,
				x: num('x'),
				y: num('y'),
				width: num('width'),
				height: num('height'),
				rotation: num('rotation'),
				polygon,
				t_start: num('t_start'),
				t_end: num('t_end'),
				text: str('text'),
				label: shape.label,
				group: str('group'),
				mask: str('mask'),
				parent_id: parent,
				char_start: shape.span?.start ?? -1,
				char_end: shape.span?.end ?? -1,
				status: 'accepted',
				source,
				...(madeBy ? { model_version: version, confidence: score } : {}),
				metadata: str('metadata') || '{}',
			}),
		);
		return index >= 0;
	}

	// ── zoom ──
	zoomIn(): void {
		this.ctx?.plugins.image.zoomIn();
	}
	zoomOut(): void {
		this.ctx?.plugins.image.zoomOut();
	}
	resetView(): void {
		this.ctx?.plugins.image.resetView();
	}
	actualSize(): void {
		this.ctx?.plugins.image.actualSize();
	}

	// ── layers ──
	setGroupBy(column: string): void {
		this.layers.setGroupBy(column);
		this.groupByColumn = column;
	}
	toggleGroupVisible(group: string): void {
		this.layers.toggleVisibility(group);
		this.ctx?.plugins.arrow.setGroupVisible(group, !this.layers.isHidden(group));
	}
	setGroupColor(group: string, hex: string): void {
		this.layers.setColor(group, hexToNum(hex));
	}
	isHidden(group: string): boolean {
		return this.hiddenGroups.has(group);
	}
	groupColorHex(group: string): string {
		const n = this.groupColors.get(group);
		return n == null ? '#3b82f6' : numToHex(n);
	}

	// ── internals ──
	private _mirrorSelection(set: ReadonlySet<number>): void {
		this.selectedSet.clear();
		for (const i of set) this.selectedSet.add(i);
	}
	private _pullLayers(): void {
		this.groupByColumn = this.layers.groupByColumn;
		this.hiddenGroups.clear();
		for (const g of this.layers.hiddenGroups) this.hiddenGroups.add(g);
		this.groupColors.clear();
		for (const [k, v] of this.layers.groupColors) this.groupColors.set(k, v);
		this._syncLayerConfig();
	}
	private _syncLayerConfig(): void {
		const arrow = this.ctx?.plugins.arrow;
		if (!arrow) return;
		arrow.setLayerConfig({
			hiddenGroups: this.layers.hiddenGroups,
			groupByColumn: this.layers.groupByColumn,
			groupColors: this.layers.groupColors,
		});
		arrow.sync();
	}
}
