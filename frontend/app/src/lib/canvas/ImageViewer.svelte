<script lang="ts">
	// One image unit on the Pixi engine: its picture (a signed link, which serves without a token), then its
	// annotations, then the controller attached to both. Remounted per unit by the shell's `{#key}`.
	import { onDestroy } from 'svelte';
	import { loadAnnotations } from '#lib/api/annotations.api.ts';
	import type { PixiContext } from './engine/index.ts';
	import type { AnnotatorController } from './controller.svelte.ts';
	import PixiCanvas from './PixiCanvas.svelte';

	let {
		imageUrl,
		annotationsUrl,
		controller,
		onload,
		onerror,
	}: {
		imageUrl: string;
		annotationsUrl: string;
		controller: AnnotatorController;
		onload?: (count: number) => void;
		/** A failure's words, and the door's status when the annotations read was refused. */
		onerror?: (message: string, status?: number) => void;
	} = $props();

	// `onready` runs un-awaited and either await can outlive this component: a stale continuation must not attach the
	// controller after its owner is gone. Checked after each await.
	let alive = true;
	onDestroy(() => {
		alive = false;
		controller.detach();
	});

	async function onready(ctx: PixiContext): Promise<void> {
		try {
			await ctx.plugins.image.load(imageUrl);
			if (!alive) return;
			// The still, for the magnetic tool's corner maps.
			ctx.plugins.interaction.setImageSource(ctx.plugins.image.imageElement);
			const loaded = await loadAnnotations(annotationsUrl);
			if (!alive) return;
			if (!loaded.ok) {
				// Left unattached: saving over annotations that could not be read would clobber them.
				onerror?.(loaded.detail, loaded.status);
				return;
			}
			const { table, version } = loaded.data;
			ctx.plugins.arrow.load(table);
			ctx.plugins.arrow.sync();
			controller.attach(ctx, table, annotationsUrl, version);
			onload?.(table.numRows);
		} catch (e) {
			onerror?.(e instanceof Error ? e.message : String(e));
		}
	}
</script>

<PixiCanvas {onready} {onerror} />
