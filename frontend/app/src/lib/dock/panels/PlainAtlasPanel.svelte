<script module lang="ts">
	/** The map's selection and hidden categories, kept across the dock remounting the panel, and started
	 *  afresh for another set-up. */
	const kept = $state<{ key: string; selected: number[]; hidden: number[] }>({
		key: '',
		selected: [],
		hidden: [],
	});
</script>

<script lang="ts">
	/**
	 * The Atlas of a table nobody stamped, from the columns a person picked (`#lib/plain/setup.ts`): each row
	 * placed by two numeric columns, or by a vector column the viewer projects to two dimensions, coloured
	 * and labelled by the columns chosen. Until it is set up, the panel is its set-up form
	 * (`#lib/plain/SetupForm.svelte`), filled in where the schema suggests exactly one answer.
	 *
	 * It draws as the stamped atlas does, with the same WebGPU renderer (`#lib/atlas/gpu-scatter.svelte`):
	 * lasso and pan, a legend whose category selects its points, a hovered point's label, and a clicked
	 * point opened in full in the Row panel. A table past the viewer's cap is drawn from an evenly spaced
	 * sample, and the panel says so. A set-up naming a column the table no longer holds is reported, with
	 * the way to set it up again, and nothing is read for it.
	 */
	import { Hand, Lasso, Loader2, Settings2, X } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import { appearance } from '@workbench/ui/appearance';
	import { interpolateViridis } from 'd3-scale-chromatic';
	import { derivedPoints, tableProfile } from '#lib/api/derived.api.ts';
	import AtlasLegend from '#lib/atlas/AtlasLegend.svelte';
	import GpuScatter from '#lib/atlas/gpu-scatter.svelte';
	import {
		buildHuePalette,
		DIM_ALPHA,
		hexToRgb,
		ATLAS_PALETTE,
		hueRgb,
		NOISE_ALPHA,
	} from '#lib/atlas/atlas-colors.ts';
	import { indicesInPolygon, type Pt } from '#lib/atlas/atlas-geometry.ts';
	import { buildGrid, nearestIndex } from '#lib/atlas/atlas-grid.ts';
	import { buildCategoryLegend } from '#lib/atlas/atlas-legend.ts';
	import { dockControl } from '#lib/dock/dock-control.svelte.ts';
	import { failureText } from '#lib/failure.ts';
	import { themeBackground } from '#lib/gpu/css-color.ts';
	import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';
	import NoTable from '#lib/plain/NoTable.svelte';
	import SetupForm from '#lib/plain/SetupForm.svelte';
	import { extentNote } from '#lib/plain/extent.ts';
	import { plain } from '#lib/plain/plain.svelte.ts';
	import { missingColumns, type AtlasSetup } from '#lib/plain/setup.ts';
	import { qualifiedTable } from '#lib/plain/tables.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import RowPanel from './RowPanel.svelte';

	const MAX_DISTINCT = 512;
	const ST_NORMAL = 0;
	const ST_NOISE = 1;
	const ST_DIMMED = 2;
	const ST_HIDDEN = 3;

	const facts = $derived(plain.facts);
	const table = $derived(plain.table);
	const info = $derived(facts && table ? (facts.tables[table] ?? null) : null);
	const setup = $derived(plain.setups.atlas);
	const missing = $derived(
		setup && info
			? missingColumns(
					setup,
					info.columns.map((c) => c.name),
				)
			: [],
	);
	const args = $derived(
		facts && table && info ? { dataset: facts.id, table, version: info.version } : null,
	);
	const editing = $derived(plain.editing.atlas || setup === undefined);
	const profile = $derived(args && editing ? tableProfile(args) : null);

	let gpuReason = $state<string | null | undefined>(undefined);
	void gpuUnsupportedReason().then((reason) => (gpuReason = reason));

	// Read once the browser can draw it: the points of a table that cannot be drawn are never fetched.
	const read = $derived(
		args && setup && !editing && missing.length === 0 && gpuReason === null
			? derivedPoints({ ...args, setup })
			: null,
	);
	const pts = $derived(read?.current ?? null);
	const key = $derived(
		args && setup ? `${args.dataset}\u0000${args.table}\u0000${JSON.stringify(setup)}` : '',
	);
	const selected = $derived(kept.key === key ? new Set(kept.selected) : new Set<number>());
	const hidden = $derived(kept.key === key ? new Set(kept.hidden) : new Set<number>());

	// The page's mode and its background, read again whenever the theme, mode or accent changes.
	const isDark = $derived(appearance.dark);
	const background = $derived((void appearance.resolved, themeBackground()));

	const grid = $derived(pts ? buildGrid(pts.x, pts.y) : null);
	const fitBounds = $derived.by(() => {
		if (!pts || pts.count === 0) return { minX: -1, minY: -1, maxX: 1, maxY: 1 };
		let minX = Infinity;
		let minY = Infinity;
		let maxX = -Infinity;
		let maxY = -Infinity;
		for (let i = 0; i < pts.count; i++) {
			minX = Math.min(minX, pts.x[i]!);
			maxX = Math.max(maxX, pts.x[i]!);
			minY = Math.min(minY, pts.y[i]!);
			maxY = Math.max(maxY, pts.y[i]!);
		}
		return { minX, minY, maxX, maxY };
	});
	const category = $derived(pts?.color?.kind === 'category' ? pts.color : null);
	const numberRange = $derived.by(() => {
		if (pts?.color?.kind !== 'number') return null;
		let lo = Infinity;
		let hi = -Infinity;
		for (const v of pts.color.values) {
			if (!Number.isFinite(v)) continue;
			lo = Math.min(lo, v);
			hi = Math.max(hi, v);
		}
		return Number.isFinite(lo) ? { lo, hi } : null;
	});

	const baseRgb = $derived.by((): Uint8Array | null => {
		if (!pts) return null;
		const buf = new Uint8Array(pts.count * 3);
		const put = (i: number, r: number, g: number, b: number): void => {
			buf[i * 3] = r;
			buf[i * 3 + 1] = g;
			buf[i * 3 + 2] = b;
		};
		const noise = hexToRgb((isDark ? ATLAS_PALETTE.dark : ATLAS_PALETTE.light).noise);
		if (category) {
			const distinct = Math.min(MAX_DISTINCT, category.labels.length);
			const palette = buildHuePalette(distinct, isDark);
			for (let i = 0; i < pts.count; i++) {
				const code = category.codes[i] ?? 0;
				const c = (category.labels[code] ?? '') === '' ? noise : (palette[code] ?? noise);
				put(i, c.r, c.g, c.b);
			}
		} else if (pts.color?.kind === 'number' && numberRange) {
			const span = numberRange.hi - numberRange.lo || 1;
			for (let i = 0; i < pts.count; i++) {
				const v = pts.color.values[i]!;
				if (!Number.isFinite(v)) {
					put(i, noise.r, noise.g, noise.b);
					continue;
				}
				const c = hexToRgb(interpolateViridis((v - numberRange.lo) / span));
				put(i, c.r, c.g, c.b);
			}
		} else {
			const accent = hueRgb(1, 4, isDark);
			for (let i = 0; i < pts.count; i++) put(i, accent.r, accent.g, accent.b);
		}
		return buf;
	});

	const pointState = $derived.by((): Uint8Array | null => {
		if (!pts) return null;
		const buf = new Uint8Array(pts.count);
		const any = selected.size > 0;
		for (let i = 0; i < pts.count; i++) {
			const code = category?.codes[i];
			if (code !== undefined && hidden.has(code)) buf[i] = ST_HIDDEN;
			else if (any && !selected.has(i)) buf[i] = ST_DIMMED;
			else if (category && (category.labels[code ?? 0] ?? '') === '') buf[i] = ST_NOISE;
			else buf[i] = ST_NORMAL;
		}
		return buf;
	});

	const legendRows = $derived(
		category
			? buildCategoryLegend(category, selected.size > 0 ? selected : null, isDark, MAX_DISTINCT)
			: [],
	);

	const maximized = $derived(dockControl.maximized === 'atlas');
	/** The panel's own width: the rows stand beside it only where both fit. */
	let panelWidth = $state(0);
	const beside = $derived(maximized && panelWidth >= 768);
	let mode = $state<'lasso' | 'pan'>('lasso');
	/** Whether the legend shows on a narrow map, where it is a toggle (always shown when wide). */
	let legendOpen = $state(false);
	let mapW = $state(0);
	let mapH = $state(0);
	let toolbarH = $state(0);
	let statusH = $state(0);
	let hover = $state<{ index: number; x: number; y: number } | null>(null);
	let pointer = { x: 0, y: 0 };
	const opened = $derived(plain.opened);
	const marker = $derived.by((): [number, number] | null => {
		if (!pts || !opened || opened.table !== table) return null;
		const i = pts.rowid.indexOf(opened.rowid);
		return i >= 0 ? [pts.x[i]!, pts.y[i]!] : null;
	});

	/** Select `indices` of this set-up's points; a selection kept for another set-up is let go. */
	function select(indices: number[]): void {
		if (kept.key !== key) kept.hidden = [];
		kept.key = key;
		kept.selected = indices;
	}

	function onHover(dataX: number, dataY: number): void {
		if (!grid || !pts) return;
		const i = nearestIndex(grid, pts.x, pts.y, dataX, dataY);
		hover = i === null ? null : { index: i, x: pointer.x, y: pointer.y };
	}

	function onPick(dataX: number, dataY: number): void {
		if (!grid || !pts || !facts || !table) return;
		const i = nearestIndex(grid, pts.x, pts.y, dataX, dataY);
		if (i === null) return;
		plain.opened = {
			table,
			rowid: pts.rowid[i]!,
			version: info?.version ?? 1,
			qualified: qualifiedTable(facts.id, table),
		};
		if (dockControl.maximized !== 'atlas') dockControl.reveal('player');
	}

	function onLasso(poly: number[]): void {
		if (!pts) return;
		const points: Pt[] = [];
		for (let i = 0; i + 1 < poly.length; i += 2) points.push({ x: poly[i]!, y: poly[i + 1]! });
		if (points.length >= 3) select(indicesInPolygon(pts.x, pts.y, points));
	}

	function pickCategory(code: number): void {
		if (!category) return;
		const out: number[] = [];
		for (let i = 0; i < category.codes.length; i++) if (category.codes[i] === code) out.push(i);
		select(out);
	}

	function toggleHidden(code: number): void {
		if (kept.key !== key) kept.selected = [];
		const now = [...hidden];
		kept.key = key;
		kept.hidden = hidden.has(code) ? now.filter((c) => c !== code) : [...now, code];
	}

	const n = (v: number): string => v.toLocaleString();
	const label = (i: number): string | null => {
		const l = pts?.label;
		return l ? (l.labels[l.codes[i] ?? 0] ?? null) : null;
	};
	const colourOf = (i: number): string | null => {
		if (!pts?.color) return null;
		return pts.color.kind === 'number'
			? String(pts.color.values[i])
			: (pts.color.labels[pts.color.codes[i] ?? 0] ?? null);
	};
	const selectedLabels = $derived(
		[...selected].slice(0, 8).map((i) => label(i) ?? `row ${pts?.rowid[i]}`),
	);
</script>

<section
	class="flex h-full min-h-0 flex-col"
	aria-labelledby="plain-atlas-heading"
	bind:clientWidth={panelWidth}
>
	<h2 id="plain-atlas-heading" class="sr-only">Atlas</h2>
	{#if !args}
		<NoTable />
	{:else if editing}
		<div class="min-h-0 flex-1 overflow-auto">
			{#if profile?.error && !profile.current}
				<div class="grid h-full place-items-center">
					<LoadFailure
						what="The table's columns could not be read."
						detail={failureText(profile.error)}
						onretry={() => void profile.refresh()}
					/>
				</div>
			{:else if !profile?.current}
				<p class="text-muted-foreground p-6 text-center text-sm" role="status">
					Reading a sample of the table to suggest columns…
				</p>
			{:else}
				<SetupForm
					panel="atlas"
					dataset={args.dataset}
					profile={profile.current}
					current={setup}
					onconfirm={(next) => plain.setUp('atlas', next as AtlasSetup)}
					oncancel={setup ? () => plain.edit('atlas', false) : undefined}
				/>
			{/if}
		</div>
	{:else if missing.length > 0}
		<div class="m-auto flex max-w-sm flex-col items-center gap-2 p-6 text-center" role="alert">
			<p class="text-sm font-medium">This atlas cannot be drawn</p>
			<p class="text-muted-foreground text-xs">
				Its set-up names {missing.length === 1 ? 'a column' : 'columns'} the table no longer holds:
				<span class="text-foreground font-mono">{missing.join(', ')}</span>.
			</p>
			<Button size="sm" onclick={() => plain.edit('atlas')}>Set up again</Button>
		</div>
	{:else if gpuReason}
		<div class="m-auto flex flex-col items-center gap-1 px-6 text-center text-sm" role="status">
			<span class="text-destructive">{gpuReason}</span>
			<span class="text-muted-foreground"
				>The atlas draws with WebGPU. The rows work without it.</span
			>
		</div>
	{:else if read?.error && !pts}
		<div class="grid h-full place-items-center">
			<LoadFailure
				what="The atlas could not be drawn."
				detail={failureText(read.error)}
				onretry={() => void read.refresh()}
			/>
		</div>
	{:else if !pts || !baseRgb || !pointState}
		<p class="text-muted-foreground m-auto flex items-center gap-2 text-sm" role="status">
			<Loader2 class="size-4 animate-spin" /> Placing the points…
		</p>
	{:else}
		<div class="flex min-h-0 flex-1">
			<div
				bind:clientWidth={mapW}
				bind:clientHeight={mapH}
				onpointermove={(e) => {
					const rect = e.currentTarget.getBoundingClientRect();
					pointer = { x: e.clientX - rect.left, y: e.clientY - rect.top };
				}}
				role="presentation"
				class="bg-background @container relative min-h-0 min-w-0 flex-1"
			>
				{#if mapW > 0 && mapH > 0}
					<GpuScatter
						xBits={pts.xBits}
						yBits={pts.yBits}
						{fitBounds}
						{baseRgb}
						state={pointState}
						filterAlpha={DIM_ALPHA}
						noiseAlpha={NOISE_ALPHA}
						pointSize={pts.count > 5000 ? 4 : 7}
						width={mapW}
						height={mapH}
						{background}
						{onHover}
						onHoverEnd={() => (hover = null)}
						{onPick}
						{onLasso}
						{mode}
						markerXY={marker}
						insets={{
							top: toolbarH + 20,
							// Beside the legend where it stands at the map's side (a wide map, `@2xl`); a narrow map
							// holds it under the toolbar.
							right: (category || numberRange) && mapW >= 672 ? 240 : 20,
							bottom: statusH + 20,
							left: 20,
						}}
					/>
				{/if}

				{#if hover}
					<div
						class="border-border bg-popover text-popover-foreground pointer-events-none absolute z-30 max-w-xs rounded-lg border px-2 py-1 text-xs shadow-md"
						style:left="{hover.x + 12}px"
						style:top="{hover.y + 12}px"
					>
						<span class="font-medium">{label(hover.index) ?? `row ${pts.rowid[hover.index]}`}</span>
						{#if colourOf(hover.index) !== null}
							<span class="text-muted-foreground"> · {setup?.color} {colourOf(hover.index)}</span>
						{/if}
					</div>
				{/if}

				<div
					bind:offsetHeight={toolbarH}
					class={[
						'border-border bg-card/85 absolute top-3 left-3 flex max-w-[calc(100%-1.5rem)] flex-wrap items-center gap-1.5 rounded-lg border p-1 text-xs shadow-sm backdrop-blur',
						(category || numberRange) && '@2xl:max-w-[calc(100%-16rem)]',
					]}
				>
					<span class="text-muted-foreground px-1.5" role="status">
						{n(pts.count)} of {n(pts.extent.total)} rows{extentNote(pts.extent)
							? ` (${extentNote(pts.extent)})`
							: ''}{pts.dropped > 0 ? ` · ${n(pts.dropped)} left out: no finite position` : ''}
						{#if pts.method === 'pca' && pts.variance !== null}
							<span
								class="hidden @2xl:inline"
								title="The vectors' first two principal components, which keep this share of their variance"
								>· {setup?.place === 'vector' ? setup.vector : ''} projected (PCA, {Math.round(
									pts.variance * 100,
								)}% of the variance)</span
							>
						{/if}
					</span>
					<div class="border-border flex items-center gap-0.5 rounded-lg border p-0.5">
						<Button
							variant={mode === 'lasso' ? 'secondary' : 'ghost'}
							size="icon-xs"
							aria-label="Lasso select"
							title="Lasso: drag to select a region"
							aria-pressed={mode === 'lasso'}
							onclick={() => (mode = 'lasso')}><Lasso /></Button
						>
						<Button
							variant={mode === 'pan' ? 'secondary' : 'ghost'}
							size="icon-xs"
							aria-label="Pan"
							title="Pan: drag to move; scroll to zoom"
							aria-pressed={mode === 'pan'}
							onclick={() => (mode = 'pan')}><Hand /></Button
						>
					</div>
					{#if category || numberRange}
						<Button
							variant={legendOpen ? 'secondary' : 'ghost'}
							size="xs"
							class="@2xl:hidden"
							aria-pressed={legendOpen}
							onclick={() => (legendOpen = !legendOpen)}>Legend</Button
						>
					{/if}
					<Button variant="ghost" size="xs" onclick={() => plain.edit('atlas')}>
						<Settings2 /> Change set-up
					</Button>
				</div>

				<div class={[legendOpen ? 'contents' : 'hidden', '@2xl:contents']}>
					{#if category}
						<AtlasLegend
							legendMode={setup?.color ?? 'none'}
							clusterRows={[]}
							categoryRows={legendRows}
							clusterStats={{ total: 0, noise: 0 }}
							categoryTitle={setup?.color ?? ''}
							categoryTotal={category.labels.length}
							legendFiltered={selected.size > 0}
							{hidden}
							{isDark}
							onPickCluster={() => {}}
							onPickCategory={pickCategory}
							onToggleHidden={toggleHidden}
							onShowAll={() => (kept.hidden = [])}
						/>
					{:else if numberRange}
						<div
							class="border-border bg-card/85 absolute top-24 right-3 flex flex-col gap-1 rounded-lg border p-2 text-xs shadow-sm backdrop-blur @2xl:top-3"
							aria-label="Colour scale"
						>
							<span class="text-muted-foreground font-medium">{setup?.color}</span>
							<span
								class="block h-2 w-40 rounded"
								style:background="linear-gradient(to right, {interpolateViridis(0)}, {interpolateViridis(
									0.5,
								)}, {interpolateViridis(1)})"
							></span>
							<span class="text-muted-foreground flex justify-between tabular-nums"
								><span>{numberRange.lo.toLocaleString()}</span><span
									>{numberRange.hi.toLocaleString()}</span
								></span
							>
						</div>
					{/if}
				</div>

				<div
					bind:offsetHeight={statusH}
					class="border-border bg-card/85 absolute bottom-3 left-3 flex max-w-[calc(100%-1.5rem)] flex-wrap items-center gap-2 rounded-lg border p-1 pl-2 text-xs shadow-sm backdrop-blur"
				>
					{#if selected.size > 0}
						<span class="text-foreground" role="status">{n(selected.size)} selected</span>
						<span class="text-muted-foreground min-w-0 truncate"
							>{selectedLabels.join(', ')}{selected.size > selectedLabels.length ? ', …' : ''}</span
						>
						<Button
							variant="ghost"
							size="icon-xs"
							aria-label="Clear selection"
							title="Clear selection"
							onclick={() => select([])}><X /></Button
						>
					{:else}
						<Lasso class="text-muted-foreground size-3.5" />
						<span class="text-muted-foreground"
							>drag to lasso · click a legend to select · click a point to open its row · scroll to
							zoom</span
						>
					{/if}
				</div>
			</div>
			{#if beside}
				<!-- Maximized, the atlas stands in for its page: the point opened, in full, beside it. -->
				<div class="border-border w-[26rem] max-w-[40%] shrink-0 border-l"><RowPanel /></div>
			{/if}
		</div>
	{/if}
</section>
