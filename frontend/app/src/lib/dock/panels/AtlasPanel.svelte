<script lang="ts">
	/**
	 * The dataset's atlas: the first declared projection space's points, drawn on a 2-D canvas, each
	 * coloured by its cluster. The points are the viewer's Arrow stream, revalidated by its ETag on each
	 * visit (`atlas.api.ts`).
	 */
	import { fetchDescriptor } from '#lib/api/datasets.api.ts';
	import { atlasPoints, type AtlasPoints } from '#lib/api/atlas.api.ts';
	import { explore } from '#lib/explore/explore.svelte.ts';

	const dataset = $derived(explore.dataset);
	const descriptor = $derived(dataset ? fetchDescriptor(dataset) : null);
	const space = $derived(descriptor?.current?.declared.atlas[0]?.name ?? null);
	const points = $derived(dataset && space ? atlasPoints({ dataset, space }) : null);

	/** A stable hue per cluster label. */
	function hue(label: string | null): number {
		if (label === null) return 0;
		let h = 0;
		for (const ch of label) h = (h * 31 + ch.charCodeAt(0)) % 360;
		return h;
	}

	/** Draw `atlas` into the canvas at its laid-out size, again whenever the canvas is resized. */
	function draw(atlas: AtlasPoints) {
		return (canvas: HTMLCanvasElement) => {
			const paint = (): void => {
				const ratio = window.devicePixelRatio || 1;
				const { width, height } = canvas.getBoundingClientRect();
				canvas.width = Math.max(1, Math.round(width * ratio));
				canvas.height = Math.max(1, Math.round(height * ratio));
				const context = canvas.getContext('2d');
				if (!context) return;
				context.clearRect(0, 0, canvas.width, canvas.height);
				let minX = Infinity;
				let maxX = -Infinity;
				let minY = Infinity;
				let maxY = -Infinity;
				for (let i = 0; i < atlas.x.length; i++) {
					const x = atlas.x[i] ?? Number.NaN;
					const y = atlas.y[i] ?? Number.NaN;
					if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
					minX = Math.min(minX, x);
					maxX = Math.max(maxX, x);
					minY = Math.min(minY, y);
					maxY = Math.max(maxY, y);
				}
				const pad = 8 * ratio;
				const sx = (canvas.width - 2 * pad) / (maxX - minX || 1);
				const sy = (canvas.height - 2 * pad) / (maxY - minY || 1);
				const size = 3 * ratio;
				for (let i = 0; i < atlas.x.length; i++) {
					const x = atlas.x[i] ?? Number.NaN;
					const y = atlas.y[i] ?? Number.NaN;
					if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
					const label = atlas.cluster[i] ?? null;
					context.fillStyle = label === null ? 'oklch(0.6 0 0)' : `oklch(0.68 0.15 ${hue(label)})`;
					context.fillRect(
						pad + (x - minX) * sx,
						canvas.height - pad - (y - minY) * sy,
						size,
						size,
					);
				}
			};
			paint();
			const observer = new ResizeObserver(paint);
			observer.observe(canvas);
			return () => observer.disconnect();
		};
	}

	const message = (err: unknown): string =>
		typeof err === 'object' && err !== null && 'body' in err
			? String((err as { body: { message?: unknown } }).body.message)
			: err instanceof Error
				? err.message
				: String(err);
</script>

<section class="flex h-full min-h-0 flex-col p-2" aria-labelledby="atlas-heading">
	<h2 id="atlas-heading" class="text-muted-foreground mb-2 text-xs">
		{space ? `Atlas · ${space}` : 'Atlas'}
	</h2>
	{#if descriptor?.current && !space}
		<p class="text-muted-foreground text-sm">This dataset declares no atlas space.</p>
	{:else if points?.error && !points.current}
		<p class="text-destructive text-sm" role="alert">
			The atlas could not be read: {message(points.error)}
		</p>
	{:else if !points?.current}
		<p class="text-muted-foreground text-sm" role="status">Reading the atlas…</p>
	{:else}
		<p class="text-muted-foreground mb-1 text-xs" role="status">
			{points.current.x.length} points
		</p>
		<div
			class="flex min-h-0 flex-1"
			role="img"
			aria-label={`Atlas of ${points.current.x.length} points, coloured by cluster`}
		>
			<canvas class="min-h-0 w-full flex-1" {@attach draw(points.current)}></canvas>
		</div>
	{/if}
</section>
