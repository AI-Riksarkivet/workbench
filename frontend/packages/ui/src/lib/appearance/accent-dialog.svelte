<script lang="ts">
	import { Button, buttonVariants } from '../components/button/index.js';
	import { BlockMark } from '../components/block-mark/index.js';
	import { Dialog } from '../components/dialog/index.js';
	import { Input } from '../components/input/index.js';
	import type { AccentInMode } from './appearance.js';
	import { appearance } from './appearance.svelte.js';
	import { oklchToHex } from './color.js';
	import { isAccent } from './core.js';

	// An accent of the person's own: one colour, from which the accent's ramp, its tinted shadow and the block marks
	// are derived. It applies as it is picked. Where the colour as chosen would not read (as text on the page, or with
	// text on it), its lightness is moved until it does and the text on it is switched to whichever side reads; the
	// dialog says which mode was changed and how, so nothing is corrected silently.
	let { open = $bindable(false) }: { open?: boolean } = $props();

	const FALLBACK = '#3a5bd9';
	let draft = $state(FALLBACK);
	// What the colour becomes on the current theme, measured against its surfaces as the store will apply it.
	const seeds = $derived(appearance.preview(draft));
	const valid = $derived(seeds !== null);

	function pick(hex: string): void {
		draft = hex;
		const accent = hex.trim().toLowerCase();
		if (isAccent(accent)) appearance.set({ accent });
	}

	function describe(mode: AccentInMode, name: 'Light' | 'Dark'): string {
		const ratio = `${mode.contrastOfText.toFixed(1)}:1`;
		const text = mode.on === 'light' ? 'light text' : 'dark text';
		return mode.lightnessClamped
			? `${name}: ${name === 'Light' ? 'darkened' : 'lightened'} to ${oklchToHex(mode.color)} so it reads, with ${text} at ${ratio}`
			: `${name}: as chosen, with ${text} at ${ratio}`;
	}
</script>

<Dialog.Root
	bind:open
	onOpenChange={(next) => {
		if (next) draft = appearance.accent ?? FALLBACK;
	}}
>
	<Dialog.Content class="max-w-md">
		<div class="grid gap-1.5">
			<Dialog.Title>Accent colour</Dialog.Title>
			<Dialog.Description>
				One colour sets the accent, its shadow and the block marks, in light and in dark. Where it
				would not read, its lightness is adjusted.
			</Dialog.Description>
		</div>
		<div class="flex items-end gap-3">
			<label class="grid gap-1.5">
				<span class="text-muted-foreground text-[0.6875rem] uppercase">Colour</span>
				<input
					type="color"
					class="border-field-line bg-input h-8 w-14 cursor-pointer rounded-none border-0 border-b p-0.5"
					value={valid ? draft : FALLBACK}
					oninput={(event) => pick(event.currentTarget.value)}
				/>
			</label>
			<label class="grid flex-1 gap-1.5">
				<span class="text-muted-foreground text-[0.6875rem] uppercase">Hex</span>
				<Input
					value={draft}
					aria-invalid={!valid}
					spellcheck={false}
					class="font-mono"
					oninput={(event) => pick(event.currentTarget.value.trim())}
				/>
			</label>
		</div>
		<!-- A preview, not controls: what the accent looks like on the things it colours. -->
		<div class="bg-muted flex flex-wrap items-center gap-4 p-4" aria-hidden="true">
			<BlockMark size="lg" />
			<span class={buttonVariants({ size: 'sm' })}>Primary</span>
			<span class={buttonVariants({ size: 'sm', variant: 'secondary' })}>Secondary</span>
			<span class="text-primary text-sm">A link</span>
		</div>
		<ul
			class="text-muted-foreground grid gap-1 text-xs"
			aria-live="polite"
			data-testid="accent-report"
		>
			{#if seeds}
				<li>{describe(seeds.light, 'Light')}</li>
				<li>{describe(seeds.dark, 'Dark')}</li>
			{:else}
				<li>Not a colour: write it as #rrggbb.</li>
			{/if}
		</ul>
		<div class="flex flex-wrap justify-end gap-2">
			<Button
				variant="ghost"
				disabled={appearance.accent === null}
				onclick={() => appearance.set({ accent: null })}
			>
				Use the theme's accent
			</Button>
			<Button onclick={() => (open = false)}>Done</Button>
		</div>
	</Dialog.Content>
</Dialog.Root>
