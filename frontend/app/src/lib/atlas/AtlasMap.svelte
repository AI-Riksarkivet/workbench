<script lang="ts">
	/**
	 * AtlasMap — the dataset's embedding map, drawn by the dock's Atlas panel.
	 *
	 * Renders a custom WebGPU point scatter (<GpuScatter>) over Float32Array x/y
	 * for the CURRENT projection space. The renderer draws instanced quads shaded
	 * into round, premultiplied sprites — modern WebGPU only, no legacy fallback.
	 *
	 * The shared cross-filter store fuses into a per-point RGBA buffer (the
	 * shader reads true per-point alpha, so "dim" is just a low alpha — no
	 * reserved colour slot, no 256-category cap). Bidirectional:
	 *
	 *   • search/filter → `crossFilter.filteredIds` dims non-matching points;
	 *   • lasso/cluster on the map → `crossFilter.selectedIds` fills the table
	 *     (via the page) and can seed a search.
	 *
	 * Everything is read from the viewer's atlas doors as the person (`#lib/api/atlas.api.ts`): the
	 * points by their ETag, which spaces are built, and the rows of a selection or a clicked point by
	 * their stable row id.
	 */
	import GpuScatter from './gpu-scatter.svelte';
	import type { AtlasPoints, AtlasSpace, Hit } from '@workbench/explorer-api';
	import { atlasPoints, atlasRows, atlasStatus } from '#lib/api/atlas.api.ts';
	import { failureText } from '#lib/failure.ts';
	import { themeBackground } from '#lib/gpu/css-color.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import { crossFilter, buildKeyIndex, hitKey, type ColorBy } from './cross-filter.svelte.ts';
	import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';
	import { activeView } from '@workbench/explorer-api/descriptor';
	import { buildGrid, nearestIndex, type SpatialGrid } from './atlas-grid.ts';
	import {
		ATLAS_PALETTE,
		NOISE_ALPHA,
		DIM_ALPHA,
		hexToRgb,
		hueRgb,
		buildHuePalette,
		type Rgb,
	} from './atlas-colors.ts';
	import { indicesInPolygon, type Pt } from './atlas-geometry.ts';
	import AtlasTooltip from './AtlasTooltip.svelte';
	import AtlasLegend from './AtlasLegend.svelte';
	import {
		visibleIds,
		buildClusterLegend,
		clusterStats as buildClusterStats,
		buildCategoryLegend,
		channelLabel,
		categoryTitle as deriveCategoryTitle,
		type CategoryChannel,
		type ClusterRanking,
	} from './atlas-legend.ts';
	// The canvas is this component's own WebGPU renderer; only the overlay CONTROLS
	// use the shared design system, so the map chrome matches the rest of the estate.
	import { Button } from '@riksarkivet/ui';
	import { Select } from '@riksarkivet/ui/select';
	import type { SelectOption } from '@riksarkivet/ui/select';
	import { Loader2, Lasso, X, Hand, Settings2 } from '@lucide/svelte';
	import { appearance } from '@riksarkivet/ui/appearance';

	let {
		active = $bindable(null),
		onSeedSearch,
		onSelectionHits,
	}: {
		active?: Hit | null;
		/** Promote the current map selection to a search (by stable `_rowid`). */
		onSeedSearch?: (rowids: number[]) => void;
		/** Surface the lasso/box selection's hits to the page (drives HitTable). */
		onSelectionHits?: (hits: Hit[], total: number) => void;
	} = $props();

	// The dataset descriptor — every space/channel/identity read routes through
	// it, so the map names no corpus column. Loaded by the root layout before any
	// route renders, so it's resolved here.
	const view = activeView();

	// The WebGPU scatter reads TRUE per-point alpha, so the old 256-slot /
	// categoryColors palette (and its grey-padding monochrome bug) is gone: we
	// build a per-point RGBA buffer directly. We still bound the number of
	// DISTINCT cluster/language hues only to keep the legend sane.
	const MAX_DISTINCT = 512;

	let error = $state<string | null>(null);
	let loading = $state(true);
	let pts = $state.raw<AtlasPoints | null>(null);
	let x = $state.raw<Float32Array | null>(null); // decoded f32 coords (CPU math)
	let y = $state.raw<Float32Array | null>(null);
	let xBits = $state.raw<Uint16Array | null>(null); // raw f16 bits (GPU vertex data)
	let yBits = $state.raw<Uint16Array | null>(null);
	// Data extent computed from the decoded f32 x/y (same source the grid uses) so
	// GpuScatter's camera fit matches the rendered f16 positions.
	// The toolbar's and the status bar's heights, so the fit keeps the points out from under them: each sits 12 px
	// (`top-3`, `bottom-3`) from its edge, and the points keep 8 px clear of it.
	let toolbarH = $state(0);
	let statusH = $state(0);
	const OVERLAY_EDGE = 12;
	const OVERLAY_CLEAR = 8;
	// The sides keep the same margin an overlay's edge does, so no point is fitted against the panel's border.
	const fitInsets = $derived({
		top: toolbarH > 0 ? toolbarH + OVERLAY_EDGE + OVERLAY_CLEAR : 0,
		right: OVERLAY_EDGE + OVERLAY_CLEAR,
		bottom: statusH > 0 ? statusH + OVERLAY_EDGE + OVERLAY_CLEAR : 0,
		left: OVERLAY_EDGE + OVERLAY_CLEAR,
	});
	let fitBounds = $state.raw<{ minX: number; minY: number; maxX: number; maxY: number }>({
		minX: 0,
		minY: 0,
		maxX: 1,
		maxY: 1,
	});
	let grid = $state.raw<SpatialGrid | null>(null); // uniform bucket index for hover

	let pointSize = $state(0); // 0 = auto
	let filterAlpha = $state(DIM_ALPHA); // 0..160 — opacity of EXCLUDED points (search-filtered OR lasso-not-selected)
	// Derived straight from the store — the selection is also cleared OUTSIDE this
	// component (routes/+page, the workflow Atlas modal), so a manual copy desyncs.
	const selectionCount = $derived(crossFilter.selectedIds.size);
	let tableLoading = $state(false);
	let mode = $state<'lasso' | 'pan'>('lasso');

	// hover popover
	let hoverIndex = $state<number | null>(null);
	let hoverX = $state(0);
	let hoverY = $state(0);
	let mapEl = $state<HTMLDivElement | null>(null);

	// scatter size
	let mapW = $state(0);
	let mapH = $state(0);

	// theme follows the app's theme button (toggles `.dark` on <html>)
	const isDark = $derived(appearance.dark);

	// Spatial-grid hover picking (`buildGrid`/`nearestIndex`) lives in ./atlas-grid;
	// lasso geometry in ./atlas-geometry; colour math in ./atlas-colors — all pure.

	/** Data extent of the decoded f32 coords (drives GpuScatter's camera fit). */
	function boundsOf(
		xs: Float32Array,
		ys: Float32Array,
	): { minX: number; minY: number; maxX: number; maxY: number } {
		let minX = Infinity;
		let minY = Infinity;
		let maxX = -Infinity;
		let maxY = -Infinity;
		const n = Math.min(xs.length, ys.length);
		for (let i = 0; i < n; i++) {
			const xi = xs[i]!;
			const yi = ys[i]!;
			if (xi < minX) minX = xi;
			if (xi > maxX) maxX = xi;
			if (yi < minY) minY = yi;
			if (yi > maxY) maxY = yi;
		}
		if (!Number.isFinite(minX)) return { minX: 0, minY: 0, maxX: 1, maxY: 1 };
		return { minX, minY, maxX, maxY };
	}

	// ── load points for a space + (re)build the shared key→index map ──────────
	/** Load `space`'s points; `again` reads them afresh (a retry), never reusing a failed answer. */
	async function load(space: AtlasSpace, again = false): Promise<void> {
		loading = true;
		error = null;
		try {
			const read = atlasPoints({ dataset: view.id, space });
			if (again) await read.refresh().catch(() => {});
			const p = await read;
			pts = p;
			// x/y arrive already decoded to owned Float32Arrays (api.ts) — consume the
			// views directly, no `Float32Array.from(...)` copy. The raw f16 bits feed
			// the GPU vertex buffer.
			const xs = p.x;
			const ys = p.y;
			x = xs;
			y = ys;
			xBits = p.xBits;
			yBits = p.yBits;
			grid = buildGrid(xs, ys);
			// Compute the data extent from the SAME decoded f32 so GpuScatter's fit
			// lines up with the f16 GPU positions (both derive from these bits).
			fitBounds = boundsOf(xs, ys);
			crossFilter.resetForSpace(buildKeyIndex(p), `${view.id}\u0000${space}`);
			// A selection the same points kept is listed again; a new universe starts with none.
			if (crossFilter.hasSelection) void loadTableForIndices([...crossFilter.selectedIds]);
			else onSelectionHits?.([], 0);
		} catch (e) {
			pts = null;
			x = null;
			y = null;
			xBits = null;
			yBits = null;
			grid = null;
			error = failureText(e);
		} finally {
			loading = false;
		}
	}

	// Keep the shown space valid for THIS dataset: if the persisted/default space
	// isn't one the descriptor declares, fall back to the first declared space.
	$effect(() => {
		const spaces = view.atlasSpaces;
		if (spaces.length > 0 && !spaces.some((s) => s.name === crossFilter.space)) {
			crossFilter.setSpace(spaces[0]!.name);
		}
	});

	// Whether this dataset declares any atlas spaces at all. When it doesn't
	// (e.g. a corpus with no projections), the whole view degrades to an empty
	// state and NEVER fetches /api/atlas/* — no spurious 404s.
	const hasAtlas = $derived(view.atlasSpaces.length > 0);

	// The space to read: the chosen one when this dataset declares it, else its first. The store's
	// choice may be another dataset's (or the default) until the effect above corrects it, and the viewer
	// refuses a space the dataset does not declare.
	const space = $derived(
		view.atlasSpaces.some((s) => s.name === crossFilter.space)
			? crossFilter.space
			: (view.atlasSpaces[0]?.name ?? crossFilter.space),
	);

	// Which declared spaces are built (their x column holds rows), read before any points: it gates each
	// space's tab, and a space with nothing projected is the map's empty state, with no points asked for.
	const status = $derived(hasAtlas ? atlasStatus({ dataset: view.id, space }) : null);
	const builtSpaces = $derived<Record<string, boolean>>(status?.current?.spaces ?? {});
	/** Whether the space shown has nothing projected yet; undefined while the status is read. */
	const unbuilt = $derived(
		status?.current ? !(status.current.spaces?.[space] ?? status.current.projected) : undefined,
	);

	// Can this browser draw the map at all? Probed once per page, and BEFORE any projection is fetched.
	// `undefined` = not probed yet, `null` = supported, a string = the reason it is not. The three states
	// are distinct on purpose: treating "not yet known" as "supported" would fetch on the first tick and
	// defeat the gate, and treating it as "unsupported" would flash an error on every load.
	let gpuReason = $state<string | null | undefined>(undefined);
	void gpuUnsupportedReason().then((reason) => (gpuReason = reason));

	// Load whenever the chosen space changes (initial mount + Text/Visual toggle).
	$effect(() => {
		const shown = space;
		if (!hasAtlas) return;
		// The projection is 6.6 MB and the renderer is WebGPU-only with no fallback, so on a browser that
		// cannot run it every one of those bytes buys a red error message. Measured before this gate
		// existed: 31.8 MiB across four toggles, all of it discarded. Wait for the probe rather than
		// racing it — `undefined` means we do not yet know, and guessing costs a download.
		if (gpuReason !== null) return;
		if (unbuilt !== false) return;
		void load(shown);
	});

	async function switchSpace(space: AtlasSpace): Promise<void> {
		if (space === crossFilter.space) return;
		if (spaceTabs.find((t) => t.value === space)?.disabled) return; // gated until built
		crossFilter.setSpace(space);
	}

	// Colour math (hslToRgb / hexToRgb / hueRgb / hueCss / buildHuePalette) is in
	// ./atlas-colors; the derived swatch/point colours below pass the live theme.
	const NOISE_RGB = $derived(hexToRgb((isDark ? ATLAS_PALETTE.dark : ATLAS_PALETTE.light).noise));
	const OTHER_RGB = $derived(hexToRgb((isDark ? ATLAS_PALETTE.dark : ATLAS_PALETTE.light).other));
	const ACCENT_RGB = $derived(hueRgb(1, 4, isDark));
	// The canvas clears to the page's own `--background`, read again when the theme changes.
	// Read again whenever the theme, mode or accent changes, not only when light and dark swap.
	const BG_COLOR = $derived((void appearance.resolved, themeBackground()));

	/**
	 * THEME-INDEPENDENT size ranking. `slotOf` maps clusterId → its dense rank
	 * (0..distinct-1, or -1 for the small-cluster tail beyond MAX_DISTINCT).
	 * `distinct` is how many distinct cluster hues we emit.
	 */
	const clusterRanking = $derived.by((): ClusterRanking | null => {
		const cl = pts?.cluster;
		if (!cl) return null;
		const counts = new Map<number, number>();
		for (const c of cl) if (c >= 0) counts.set(c, (counts.get(c) ?? 0) + 1);
		const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1]);
		const distinct = Math.min(ranked.length, MAX_DISTINCT);
		const slotOf = new Map<number, number>();
		ranked.forEach(([id], rank) => slotOf.set(id, rank < MAX_DISTINCT ? rank : -1));
		return { slotOf, distinct };
	});

	/** The factorized (codes, labels) pair backing a categorical colour mode — a
	 *  declared atlas channel read by name from `points.channels`. `cluster`/`none`
	 *  are NOT channels (cluster carries its own noise/#id/hide semantics). */
	function channelFor(p: AtlasPoints | null, mode: ColorBy): CategoryChannel | null {
		if (!p || mode === 'cluster' || mode === 'none') return null;
		return p.channels[mode] ?? null;
	}

	// Membership state enum bytes (must match the WGSL fragment + the old alphaFor
	// priority): 0=NORMAL (full), 1=NOISE (noiseAlpha), 2=DIMMED (filterAlpha),
	// 3=HIDDEN (discard). DIMMED outranks NOISE — a filtered-out/unselected noise
	// point dims to filterAlpha, exactly as the old alphaFor did.
	const ST_NORMAL = 0;
	const ST_NOISE = 1;
	const ST_DIMMED = 2;
	const ST_HIDDEN = 3;

	/**
	 * Per-point base RGB hue (length 3*count) — the category colour ONLY, no
	 * alpha. The shader applies the state-derived alpha. Rebuilt only when the
	 * HUE changes: colorBy / theme (isDark) / space (pts) / clusterRanking — NOT
	 * on the filterAlpha slider, NOT on selection. (hidden points are handled by
	 * the state buffer, so `hidden` is intentionally not a dep here.)
	 */
	const baseRgb = $derived.by((): Uint8Array | null => {
		const p = pts;
		if (!p) return null;
		const n = p.count;
		const colorBy = crossFilter.colorBy;
		const buf = new Uint8Array(n * 3);

		const write = (i: number, col: Rgb): void => {
			const o = i * 3;
			buf[o] = col.r;
			buf[o + 1] = col.g;
			buf[o + 2] = col.b;
		};

		if (colorBy === 'cluster' && p.cluster) {
			const r = clusterRanking;
			if (!r) return null;
			const { slotOf, distinct } = r;
			const palette = buildHuePalette(distinct, isDark); // distinct hues once, not per point
			const cl = p.cluster;
			const noise = NOISE_RGB;
			const other = OTHER_RGB;
			for (let i = 0; i < n; i++) {
				const c = cl[i]!;
				if (c < 0) {
					write(i, noise);
				} else {
					const rank = slotOf.get(c) ?? -1;
					write(i, rank < 0 ? other : palette[rank]!);
				}
			}
			return buf;
		}

		const channel = channelFor(p, colorBy);
		if (channel) {
			const { codes, labels } = channel;
			const distinct = Math.min(MAX_DISTINCT, labels.length);
			const palette = buildHuePalette(distinct, isDark); // distinct hues once, not per point
			const other = OTHER_RGB;
			const noise = NOISE_RGB;
			for (let i = 0; i < n; i++) {
				const code = codes[i] ?? 0;
				const empty = (labels[code] ?? '') === ''; // unclustered/noise → muted grey
				write(i, empty ? noise : code < distinct ? palette[code]! : other);
			}
			return buf;
		}

		// colorBy === 'none' (or the channel's data is absent): one accent hue.
		const accent = ACCENT_RGB;
		for (let i = 0; i < n; i++) write(i, accent);
		return buf;
	});

	/**
	 * Per-point membership state (length count, 1 byte/point). Rebuilt only on
	 * filteredIds / selectedIds / hidden / colorBy / space change — NOT on the
	 * filterAlpha slider (a uniform) and NOT on theme. This is the buffer a
	 * selection/filter change re-uploads (8× smaller than the old RGBA).
	 */
	const pointState = $derived.by((): Uint8Array | null => {
		const p = pts;
		if (!p) return null;
		const n = p.count;
		const colorBy = crossFilter.colorBy;
		const buf = new Uint8Array(n);

		// Read both cross-filter Sets once (tracks them as deps + avoids 145k method
		// calls). A point EXCLUDED by either the search filter or the lasso/cluster
		// selection becomes DIMMED (the shader applies the slider's filterAlpha).
		const fIds = crossFilter.filteredIds;
		const sel = crossFilter.selectedIds;
		const selActive = sel.size > 0;
		const hidden = crossFilter.hidden;

		// Cluster mode: noise (c<0) shares one hide key (-1); clusters use their id.
		if (colorBy === 'cluster' && p.cluster) {
			const cl = p.cluster;
			for (let i = 0; i < n; i++) {
				const c = cl[i]!;
				if (hidden.has(c < 0 ? -1 : c)) {
					buf[i] = ST_HIDDEN;
					continue;
				}
				if ((fIds !== null && !fIds.has(i)) || (selActive && !sel.has(i))) buf[i] = ST_DIMMED;
				else if (c < 0) buf[i] = ST_NOISE;
				else buf[i] = ST_NORMAL;
			}
			return buf;
		}

		const channel = channelFor(p, colorBy);
		if (channel) {
			const { codes, labels } = channel;
			for (let i = 0; i < n; i++) {
				const code = codes[i] ?? 0;
				if (hidden.has(code)) {
					buf[i] = ST_HIDDEN;
					continue;
				}
				if ((fIds !== null && !fIds.has(i)) || (selActive && !sel.has(i))) buf[i] = ST_DIMMED;
				else if ((labels[code] ?? '') === '') buf[i] = ST_NOISE; // empty label → muted
				else buf[i] = ST_NORMAL;
			}
			return buf;
		}

		// colorBy === 'none': no noise/hidden semantics — only the dim membership.
		for (let i = 0; i < n; i++) {
			buf[i] =
				(fIds !== null && !fIds.has(i)) || (selActive && !sel.has(i)) ? ST_DIMMED : ST_NORMAL;
		}
		return buf;
	});

	const autoPointSize = $derived(pointSize > 0 ? pointSize : 5);

	// Data-space coords of the active hit's point (the one in the player / table)
	// so <GpuScatter> can ring it — resolves via the shared key→index map.
	const activeMarkerXY = $derived.by((): [number, number] | null => {
		const a = active;
		const xs = x;
		const ys = y;
		if (!a || !xs || !ys) return null;
		const i = crossFilter.keyToIndex.get(hitKey(a));
		if (i === undefined) return null;
		const mx = xs[i];
		const my = ys[i];
		return mx !== undefined && my !== undefined ? [mx, my] : null;
	});

	// ── filter-aware legend counts ────────────────────────────────────────────
	// The legend math is PURE (./atlas-legend, unit-testable): these thin deriveds
	// resolve the reactive store reads (filtered/selected sets, colorBy, theme),
	// the channel + ranking, and hand them to the builders. The presentational
	// <AtlasLegend/> renders the rows — it computes nothing.
	const visible = $derived(
		visibleIds(crossFilter.filteredIds, crossFilter.hasSelection ? crossFilter.selectedIds : null),
	);
	const legendFiltered = $derived(visible !== null);
	const clusterRows = $derived(
		pts?.cluster && clusterRanking
			? buildClusterLegend(pts.cluster, clusterRanking, visible, isDark)
			: [],
	);
	const clusterStatsValue = $derived(buildClusterStats(pts?.cluster, visible));
	const categoryChannel = $derived(channelFor(pts, crossFilter.colorBy));
	const categoryRows = $derived(
		buildCategoryLegend(categoryChannel, visible, isDark, MAX_DISTINCT),
	);
	const categoryTotal = $derived(categoryChannel?.labels.length ?? 0);
	const categoryTitleValue = $derived(deriveCategoryTitle(crossFilter.colorBy));

	// Colour modes: `cluster` (always), then each declared atlas channel present
	// in the points payload (by name, in descriptor order), then `none`. A channel
	// only appears once its column is built into /points, so the menu tracks data.
	const colorOptions = $derived.by((): (SelectOption & { value: ColorBy })[] => {
		const opts: (SelectOption & { value: ColorBy })[] = [{ value: 'cluster', label: 'Cluster' }];
		const p = pts;
		if (p) {
			for (const name of view.atlasChannels(crossFilter.space)) {
				if (p.channels[name]) opts.push({ value: name, label: channelLabel(name) });
			}
		}
		opts.push({ value: 'none', label: 'None' });
		return opts;
	});
	const colorValues = $derived(new Set(colorOptions.map((o) => o.value as string)));

	// Toolbar: the size/opacity sliders live in a ⚙ popover to keep the bar clean.
	let showSettings = $state(false);
	/** Whether the legend shows on a narrow map, where it is a toggle (always shown when wide). */
	let legendOpen = $state(false);

	// Space tabs come from the descriptor's declared atlas spaces (DRY the
	// segmented toggle). The first declared space stays selectable; the rest are
	// gated until /status reports their projection built.
	const spaceTabs: { value: AtlasSpace; label: string; disabled: boolean; title: string }[] =
		$derived(
			view.atlasSpaces.map((s, i) => {
				const built = builtSpaces[s.name] ?? false;
				const disabled = i > 0 && !built;
				const label = channelLabel(s.name);
				return {
					value: s.name,
					label,
					disabled,
					title: disabled
						? `${label}: no projection has been written for this space yet`
						: `${label} embedding map`,
				};
			}),
		);

	// Pure geometry/grid helpers live in ./atlas-geometry + ./atlas-grid; these
	// thin wrappers bind them to the component's currently-loaded points/grid.
	function indicesInLasso(poly: Pt[]): number[] {
		return x && y ? indicesInPolygon(x, y, poly) : [];
	}
	function pickIndex(qx: number, qy: number): number | null {
		return grid && x && y ? nearestIndex(grid, x, y, qx, qy) : null;
	}

	// ── load the table for a set of indices (1000-cap; full set stays in store) ─
	// Resolved by stable `_rowid` (one per point from /points) — the backend takes
	// exactly these rows, so even a max-size selection lists in a few hundred ms.
	async function loadTableForIndices(idx: number[]): Promise<void> {
		if (idx.length === 0) {
			onSelectionHits?.([], 0);
			return;
		}
		const rowids = pts?.rowid;
		if (!rowids) {
			// No per-point rowid (e.g. a stale cached /points) — we can't fetch the
			// rows. Report an EMPTY selection (total 0, not idx.length) so the page
			// falls back to its search hits / empty-state rather than showing an empty
			// table under a misleading "N chunks" header. A cache-busted /points
			// (api.ts) restores rowids; this is just the belt-and-braces guard.
			console.warn('atlas: /points payload has no rowid — cannot list selection (stale cache?)');
			crossFilter.clearSelection(); // also undims the map so state stays consistent
			onSelectionHits?.([], 0);
			return;
		}
		const ids = idx
			.slice(0, 1000)
			.map((i) => rowids[i])
			.filter((r): r is number => r !== undefined);
		tableLoading = true;
		try {
			const hits = await atlasRows(ids);
			onSelectionHits?.(hits, idx.length);
		} catch (e) {
			console.error('atlas: failed to load selection', e);
			// Report total 0 (matching the no-rowid guard above) — never a phantom
			// "N chunks" header over an empty table.
			onSelectionHits?.([], 0);
		} finally {
			tableLoading = false;
		}
	}

	// ── GpuScatter callbacks ──────────────────────────────────────────────────
	// hover → nearest index → tooltip (anchored at cursor)
	function onScatterHover(dataX: number, dataY: number): void {
		const i = pickIndex(dataX, dataY);
		hoverIndex = i;
		if (i != null) {
			hoverX = lastPointerX;
			hoverY = lastPointerY;
		}
	}
	function onScatterHoverEnd(): void {
		hoverIndex = null;
	}

	// click a point → load full hit into the shared player
	async function onScatterPick(dataX: number, dataY: number): Promise<void> {
		const i = pickIndex(dataX, dataY);
		if (i == null) return;
		const rowid = pts?.rowid?.[i];
		if (rowid === undefined) return;
		try {
			const [hit] = await atlasRows([rowid]);
			if (hit) active = hit;
		} catch {
			/* keep previous */
		}
	}

	// lasso → FULL index selection (drives dimming) + capped table fetch
	async function onScatterLasso(polyDataXY: number[]): Promise<void> {
		const poly: Pt[] = [];
		for (let i = 0; i + 1 < polyDataXY.length; i += 2) {
			poly.push({ x: polyDataXY[i]!, y: polyDataXY[i + 1]! });
		}
		if (poly.length < 3) return;
		const idx = indicesInLasso(poly);
		crossFilter.setSelectedIndices(idx); // untruncated — drives the dim recolour
		await loadTableForIndices(idx);
	}

	// legend click → select an entire cluster / language (a first-class lasso)
	async function pickCluster(id: number): Promise<void> {
		const cl = pts?.cluster;
		if (!cl) return;
		crossFilter.selectCluster(id, cl);
		await loadTableForIndices([...crossFilter.selectedIds]);
	}

	/** Select every point in a category (language / topic / doc_topic) → table +
	 *  seedable search. The atlas analog of the Tree page's "show results". */
	async function pickCategory(code: number): Promise<void> {
		const ch = channelFor(pts, crossFilter.colorBy);
		if (!ch) return;
		const idx: number[] = [];
		for (let i = 0; i < ch.codes.length; i++) if (ch.codes[i] === code) idx.push(i);
		crossFilter.setSelectedIndices(idx);
		await loadTableForIndices(idx);
	}

	function clearSelection(): void {
		crossFilter.clearSelection();
		onSelectionHits?.([], 0);
	}

	function seedSearch(): void {
		const rowids = pts?.rowid;
		if (!rowids) return;
		const ids = [...crossFilter.selectedIds]
			.map((i) => rowids[i])
			.filter((r): r is number => r !== undefined);
		if (ids.length) onSeedSearch?.(ids);
	}

	// ── hover popover wiring ──────────────────────────────────────────────────
	// Plain (non-reactive) cursor coords: read synchronously when a hover lands,
	// never rendered directly — so a pointermove no longer triggers reactivity.
	let lastPointerX = 0;
	let lastPointerY = 0;
	let rafId = 0;
	function flushHover(): void {
		rafId = 0;
		if (hoverIndex != null) {
			hoverX = lastPointerX;
			hoverY = lastPointerY;
		}
	}
	function trackPointer(e: PointerEvent): void {
		const rect = mapEl?.getBoundingClientRect();
		if (!rect) return;
		lastPointerX = e.clientX - rect.left;
		lastPointerY = e.clientY - rect.top;
		if (hoverIndex != null && rafId === 0) {
			rafId = requestAnimationFrame(flushHover);
		}
	}
	$effect(() => () => {
		if (rafId !== 0) cancelAnimationFrame(rafId);
	});

	const legendMode = $derived(crossFilter.colorBy);
</script>

{#if !hasAtlas}
	<div class="text-muted-foreground grid h-full place-items-center p-6 text-center text-sm">
		This dataset has no embedding map.
	</div>
{:else if error}
	<div class="grid h-full place-items-center">
		<LoadFailure
			what="The embedding map could not be loaded."
			detail={error}
			onretry={() => void load(space, true)}
		/>
	</div>
{:else}
	<div
		bind:this={mapEl}
		bind:clientWidth={mapW}
		bind:clientHeight={mapH}
		onpointermove={trackPointer}
		role="presentation"
		class="bg-background @container relative h-full min-h-0"
	>
		{#if status?.error && !status.current}
			<div class="grid h-full place-items-center">
				<LoadFailure
					what="The embedding map could not be loaded."
					detail={failureText(status.error)}
					onretry={() => void status.refresh()}
				/>
			</div>
		{:else if unbuilt}
			<div class="grid h-full place-items-center p-6 text-center">
				<div class="max-w-sm">
					<p class="text-foreground text-sm font-medium">No embedding map yet</p>
					<p class="text-muted-foreground mt-1 text-xs">
						The {channelLabel(crossFilter.space)} projection holds no points yet.
					</p>
				</div>
			</div>
		{:else if gpuReason}
			<!-- The reason belongs HERE, not only in <GpuScatter>: now that an unsupported browser never
			     fetches, `pts` stays null and the scatter never mounts, so its own message would never be
			     reached and the panel would spin for ever. Same wording, one source (`gpu-support.ts`). -->
			<div
				class="text-muted-foreground flex h-full flex-col items-center justify-center gap-1 px-6 text-center text-sm"
			>
				<span class="text-destructive">{gpuReason}</span>
				<span
					>The embedding map draws with WebGPU. Search, the tree and the graph's table work without
					it.</span
				>
			</div>
		{:else if loading || !x || !y || !xBits || !yBits || !pts || !baseRgb || !pointState}
			<div class="text-muted-foreground flex h-full items-center justify-center gap-2 text-sm">
				<Loader2 class="size-4 animate-spin" /> Loading embedding map…
			</div>
		{:else}
			{#if mapW > 0 && mapH > 0}
				<GpuScatter
					{xBits}
					{yBits}
					{fitBounds}
					{baseRgb}
					state={pointState}
					{filterAlpha}
					noiseAlpha={NOISE_ALPHA}
					pointSize={autoPointSize}
					width={mapW}
					height={mapH}
					background={BG_COLOR}
					onHover={onScatterHover}
					onHoverEnd={onScatterHoverEnd}
					onPick={onScatterPick}
					onLasso={onScatterLasso}
					{mode}
					markerXY={activeMarkerXY}
					insets={fitInsets}
				/>
			{/if}

			<AtlasTooltip {pts} index={hoverIndex} x={hoverX} y={hoverY} />

			<!-- toolbar -->
			<div
				bind:offsetHeight={toolbarH}
				class="border-border bg-card/85 absolute top-3 left-3 flex max-w-[calc(100%-1.5rem)] flex-wrap items-center gap-1.5 rounded-lg border p-1 text-xs shadow-sm backdrop-blur"
			>
				<span class="text-muted-foreground px-1.5">{(pts.count ?? 0).toLocaleString()} pts</span>

				<!-- projection space (segmented) -->
				<div class="border-border flex items-center gap-0.5 rounded-lg border p-0.5">
					{#each spaceTabs as t (t.value)}
						<Button
							variant={crossFilter.space === t.value ? 'secondary' : 'ghost'}
							size="xs"
							class={crossFilter.space === t.value ? '' : 'text-muted-foreground'}
							aria-pressed={crossFilter.space === t.value}
							disabled={t.disabled}
							title={t.title}
							onclick={() => switchSpace(t.value)}
						>
							{t.label}
						</Button>
					{/each}
				</div>

				<!-- tool: lasso / pan (segmented, icon-only) -->
				<div class="border-border flex items-center gap-0.5 rounded-lg border p-0.5">
					<Button
						variant={mode === 'lasso' ? 'secondary' : 'ghost'}
						size="icon-xs"
						title="Lasso — drag to select a region"
						aria-label="Lasso select"
						aria-pressed={mode === 'lasso'}
						onclick={() => (mode = 'lasso')}
					>
						<Lasso />
					</Button>
					<Button
						variant={mode === 'pan' ? 'secondary' : 'ghost'}
						size="icon-xs"
						title="Pan — drag to move (or shift / middle-drag in any mode; scroll to zoom)"
						aria-label="Pan"
						aria-pressed={mode === 'pan'}
						onclick={() => (mode = 'pan')}
					>
						<Hand />
					</Button>
				</div>

				<!-- colour by: a function binding straight to the singleton — the design-system
             Select binds a plain string, so the setter narrows it to the store's
             ColorBy union at the boundary (no shadow $state to clobber the
             persisted value on remount). -->
				<Select
					bind:value={
						() => crossFilter.colorBy,
						(v) => {
							if (colorValues.has(v)) crossFilter.colorBy = v;
						}
					}
					options={colorOptions}
					ariaLabel="Colour points by"
				/>

				<!-- The legend, behind a toggle where the map is too narrow to hold it beside the toolbar. -->
				<Button
					variant={legendOpen ? 'secondary' : 'ghost'}
					size="xs"
					class="@2xl:hidden"
					aria-pressed={legendOpen}
					onclick={() => (legendOpen = !legendOpen)}
				>
					Legend
				</Button>

				<!-- display settings (point size, filtered opacity) -->
				<div class="relative">
					<Button
						variant={showSettings ? 'secondary' : 'ghost'}
						size="icon-sm"
						title="Display settings"
						aria-label="Display settings"
						aria-pressed={showSettings}
						onclick={() => (showSettings = !showSettings)}
					>
						<Settings2 />
					</Button>
					{#if showSettings}
						<button
							type="button"
							aria-label="Close settings"
							class="fixed inset-0 z-10 cursor-default"
							onclick={() => (showSettings = false)}
						></button>
						<div
							class="border-border bg-popover text-popover-foreground absolute top-full left-0 z-20 mt-1 flex w-56 flex-col gap-3 rounded-lg border p-3 text-xs shadow-md"
						>
							<label class="block">
								<span class="text-muted-foreground mb-1 flex items-center justify-between">
									Point size <span class="font-mono">{pointSize === 0 ? 'auto' : pointSize}</span>
								</span>
								<input
									type="range"
									min="0"
									max="20"
									step="0.5"
									bind:value={pointSize}
									class="accent-primary w-full"
								/>
							</label>
							<label class="block">
								<span class="text-muted-foreground mb-1 flex items-center justify-between">
									Dimmed opacity <span class="font-mono">{filterAlpha}</span>
								</span>
								<input
									type="range"
									min="0"
									max="160"
									step="1"
									bind:value={filterAlpha}
									class="accent-primary w-full"
								/>
								<span class="text-muted-foreground mt-0.5 block text-[0.7rem]">
									How visible excluded points are — search-filtered or not in the lasso/cluster
									selection (left = hidden).
								</span>
							</label>
						</div>
					{/if}
				</div>
			</div>

			<!-- legend / distribution (clickable → select); below the toolbar on a narrow map -->
			<div class={[legendOpen ? 'contents' : 'hidden', '@2xl:contents']}>
				<AtlasLegend
					{legendMode}
					{clusterRows}
					{categoryRows}
					clusterStats={clusterStatsValue}
					categoryTitle={categoryTitleValue}
					{categoryTotal}
					{legendFiltered}
					hidden={crossFilter.hidden}
					{isDark}
					onPickCluster={pickCluster}
					onPickCategory={pickCategory}
					onToggleHidden={(code) => crossFilter.toggleHidden(code)}
					onShowAll={() => crossFilter.showAll()}
				/>
			</div>

			<!-- selection status + actions -->
			<div
				bind:offsetHeight={statusH}
				class="border-border bg-card/85 absolute bottom-3 left-3 flex max-w-[calc(100%-1.5rem)] items-center gap-2 rounded-lg border p-1 pl-2 text-xs shadow-sm backdrop-blur"
			>
				{#if tableLoading}
					<Loader2 class="text-muted-foreground size-3.5 animate-spin" />
				{/if}
				{#if selectionCount > 0}
					<span class="text-foreground">{selectionCount.toLocaleString()} selected</span>
					{#if selectionCount > 1000}
						<span class="text-muted-foreground">· table shows 1000</span>
					{/if}
					<Button
						variant="outline"
						size="xs"
						onclick={seedSearch}
						title="Open this map selection as the full results list (paging, rerank, table/grid views)"
					>
						Show as results
					</Button>
					<Button
						variant="ghost"
						size="icon-xs"
						onclick={clearSelection}
						title="Clear selection"
						aria-label="Clear selection"
					>
						<X />
					</Button>
				{:else}
					<Lasso class="text-muted-foreground size-3.5" />
					<span class="text-muted-foreground"
						>drag to lasso · click a legend to select · click a point to play · scroll to zoom</span
					>
				{/if}
			</div>
		{/if}
	</div>
{/if}
