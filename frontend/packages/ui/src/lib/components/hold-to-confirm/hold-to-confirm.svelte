<script lang="ts">
	// Adapted from Baby UI's `hold-to-confirm` (https://github.com/kanakkholwal/baby-ui, Apache-2.0; see THIRD_PARTY.md
	// in this package): a destructive action confirmed one of two ways, so a stray click never fires it: a press held for
	// `duration` (pointer, Space or Enter), or two presses, the first of which arms it for `armWindow` (what a screen
	// reader in browse mode, voice control or a switch can send). Its name says both; arming and the outcome are
	// announced.
	import type { Snippet } from 'svelte';
	import { untrack } from 'svelte';
	import type { HTMLButtonAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';
	import { morph, trackActiveWidth } from './morph.js';
	import { createHold } from './hold.js';
	import { type HoldToConfirmSize, type HoldToConfirmVariant, holdToConfirm } from './variants.js';

	let {
		label,
		armedLabel = 'Press again',
		confirmedLabel = 'Done',
		armWindow = 4000,
		onConfirm,
		duration = 1200,
		icon,
		variant = 'default',
		size = 'default',
		confirmed = $bindable(),
		onHoldChange,
		disabled = false,
		class: classProp,
		...rest
	}: {
		/** The action, e.g. "Clear": the name reads "Clear — hold, or press twice". */
		label: string;
		/** Shown while one press has armed it, e.g. "Press again". */
		armedLabel?: string;
		/** How long one press stays armed, in milliseconds. */
		armWindow?: number;
		/** Shown once the hold completes, e.g. "Deleted". */
		confirmedLabel?: string;
		/** Called once when the hold completes. */
		onConfirm: () => void;
		/** Hold length in milliseconds. */
		duration?: number;
		icon?: Snippet;
		variant?: HoldToConfirmVariant;
		size?: HoldToConfirmSize;
		/** The done state; bindable. Set it back to false to reset. */
		confirmed?: boolean;
		/** Reports when a hold starts and ends, for hints such as "Keep holding". */
		onHoldChange?: (holding: boolean) => void;
		disabled?: boolean;
		class?: string;
	} & Omit<HTMLButtonAttributes, 'children' | 'onclick'> = $props();

	const hintId = $props.id();
	let ownDone = $state(false);
	let holding = $state(false);
	let armed = $state(false);
	const done = $derived(confirmed ?? ownDone);
	// Server and first paint only; after that the engine owns the fill's clip.
	const initialDone = untrack(() => done);
	let button = $state<HTMLButtonElement>();
	let engine: ReturnType<typeof createHold> | null = null;

	const options = $derived({
		duration,
		armWindow,
		done,
		disabled,
		onComplete: () => {
			if (confirmed === undefined) ownDone = true;
			else confirmed = true;
			onConfirm();
		},
		onHoldChange: (value: boolean) => {
			holding = value;
			onHoldChange?.(value);
		},
		onArmChange: (value: boolean) => {
			armed = value;
		},
	});
	const name = $derived(
		done
			? confirmedLabel
			: armed
				? `${label} — press again to confirm`
				: `${label} — hold, or press twice`,
	);

	$effect(() => {
		const el = button;
		if (!el) return;
		const created = untrack(() => createHold(el, options));
		engine = created;
		return () => {
			created.destroy();
			engine = null;
		};
	});
	$effect(() => {
		engine?.update(options);
	});

	const s = $derived(holdToConfirm({ variant, size }));
	const m = morph();
	const seconds = $derived((duration / 1000).toLocaleString('en-US', { maximumFractionDigits: 1 }));

	function widthOf(frame: HTMLElement) {
		return trackActiveWidth(frame);
	}
</script>

{#snippet face()}
	<span class={s.face()}>
		<span class={s.icon()}>
			<span data-on={!done} class={m.icon()}>
				{#if icon}
					{@render icon()}
				{:else}
					<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
						<path
							d="M2.5 4h11M6.5 4V2.8h3V4M4 4l.6 9a1 1 0 0 0 1 .9h4.8a1 1 0 0 0 1-.9L12 4"
							stroke="currentColor"
							stroke-width="1.4"
							stroke-linecap="round"
							stroke-linejoin="round"
						/>
					</svg>
				{/if}
			</span>
			<span data-on={done} class={m.icon()}>
				<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
					<path
						d="M4 12.5l5 5L20 6.5"
						pathLength="1"
						class={s.check()}
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
					/>
				</svg>
			</span>
		</span>
		<span {@attach widthOf} class={m.frame()}>
			<span data-on={!done && !armed} class={m.face()}>{label}</span>
			<span data-on={!done && armed} class={m.face()}>{armedLabel}</span>
			<span data-on={done} class={m.face()}>{confirmedLabel}</span>
		</span>
	</span>
{/snippet}

<button
	{...rest}
	bind:this={button}
	type="button"
	data-slot="hold-to-confirm"
	data-state={done ? 'done' : holding ? 'holding' : armed ? 'armed' : 'idle'}
	{disabled}
	aria-disabled={done || undefined}
	aria-label={name}
	aria-describedby={done ? undefined : hintId}
	class={cn(s.root(), classProp)}
>
	{@render face()}
	<span
		data-hold-fill=""
		class={s.fill()}
		style:clip-path={initialDone ? 'inset(0 0% 0 0)' : 'inset(0 100% 0 0)'}
		aria-hidden="true"
	>
		{@render face()}
	</span>
</button>
<span id={hintId} class="sr-only">
	Hold for {seconds} seconds, or press twice, to confirm. With a keyboard, hold Space or Enter.
</span>
<span class="sr-only" role="status" aria-live="polite"
	>{done ? confirmedLabel : armed ? `${armedLabel} to ${label.toLowerCase()}` : ''}</span
>
