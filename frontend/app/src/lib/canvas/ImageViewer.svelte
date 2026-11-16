<script lang="ts">
	// One image unit on the labeling canvas: the shared still viewer (`#lib/media/image`) shows the picture, fits,
	// zooms and pans it; this adds the annotation layer over it (the Arrow shapes and the interaction tools), then
	// the annotations, then the controller attached to both. Remounted per unit by the shell's `{#key}`.
	import { onDestroy } from 'svelte';
	import { loadAnnotations } from '#lib/api/annotations.api.ts';
	import ImageStage, { type ImageStageContext } from '#lib/media/image/ImageStage.svelte';
	import { ArrowDataPlugin, InteractionManager, type PixiContext } from './engine/index.ts';
	import type { AnnotatorController } from './controller.svelte.ts';

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

	// The annotations read is awaited after the stage handed over, and can outlive this component: a stale
	// continuation must not attach the controller after its owner is gone. Checked after the await.
	let alive = true;
	onDestroy(() => {
		alive = false;
		controller.detach();
	});

	let interaction: InteractionManager | undefined;
	let arrow: ArrowDataPlugin | undefined;

	/** Over the picture: the shapes, then the tools; a drag pans only while the pan tool is chosen. */
	function onready({ app, image }: ImageStageContext): () => void {
		const shapes = new ArrowDataPlugin(app);
		const im = new InteractionManager(app, shapes);
		arrow = shapes;
		interaction = im;
		shapes.setViewport(image.getViewportBounds());
		const ctx: PixiContext = { app, plugins: { image, arrow: shapes, interaction: im } };
		// The still, for the magnetic tool's corner maps.
		im.setImageSource(image.imageElement);
		void loadLayer(ctx);
		return () => {
			im.destroy();
			shapes.destroy();
			interaction = undefined;
			arrow = undefined;
		};
	}

	async function loadLayer(ctx: PixiContext): Promise<void> {
		try {
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

<ImageStage
	url={imageUrl}
	controls={false}
	canPan={() => interaction?.currentTool === 'pan'}
	{onready}
	onerror={(failure) => onerror?.(failure.message)}
	onviewport={(bounds) => {
		arrow?.setViewport(bounds);
		interaction?.updateHandles();
	}}
/>
