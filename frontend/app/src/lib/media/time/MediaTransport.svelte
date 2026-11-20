<script lang="ts">
	// The transport of a recording or a video: play and pause, one scrubber, the clock, the playback speed and the
	// volume. Controlled: every value is the media element's own, bound by the holder (`bind:currentTime`,
	// `bind:paused`, ...) and passed in here bound again, so a press here is a change on the element and nothing is kept
	// twice. What the recording holds over time (its waveform, segments, regions) is the time strip's above it
	// (`TimeStrip.svelte`).
	import type { Snippet } from 'svelte';
	import { Pause, Play, Volume1, Volume2, VolumeX } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { Slider } from '@riksarkivet/ui/slider';
	import { cn } from '@riksarkivet/ui/utils';
	import { formatClock } from './timeline.ts';

	let {
		currentTime = $bindable(0),
		duration,
		paused = $bindable(true),
		volume = $bindable(1),
		muted = $bindable(false),
		playbackRate = $bindable(1),
		noun = 'recording',
		actions,
		class: className,
	}: {
		currentTime?: number;
		/** The recording's length in seconds; 0 (or NaN) while it is not yet known. */
		duration: number;
		paused?: boolean;
		volume?: number;
		muted?: boolean;
		playbackRate?: number;
		/** What the recording is called, for the controls' names ("Seek in the recording"). */
		noun?: string;
		/** Controls at the end of the bar (fullscreen). */
		actions?: Snippet;
		class?: string;
	} = $props();

	const known = $derived(Number.isFinite(duration) && duration > 0);
	const length = $derived(known ? duration : 0);

	const SPEEDS = [1, 1.25, 1.5, 2, 0.5, 0.75];
	const nextSpeed = $derived(SPEEDS[(SPEEDS.indexOf(playbackRate) + 1) % SPEEDS.length] ?? 1);
	const speedLabel = (rate: number): string => `${rate}×`;

	const silent = $derived(muted || volume === 0);

	function seek(t: number): void {
		if (!known) return;
		currentTime = Math.min(length, Math.max(0, t));
	}

	/** The scrubber's arrows step five seconds, as players do, rather than the slider's fine step. */
	const STEP = 5;
	function onScrubKeys(e: KeyboardEvent): void {
		const delta =
			e.key === 'ArrowRight' || e.key === 'ArrowUp'
				? STEP
				: e.key === 'ArrowLeft' || e.key === 'ArrowDown'
					? -STEP
					: e.key === 'PageUp'
						? length / 10
						: e.key === 'PageDown'
							? -length / 10
							: null;
		if (delta === null) return;
		e.preventDefault();
		e.stopPropagation();
		seek(currentTime + delta);
	}
</script>

<div
	class={cn('@container flex flex-col gap-1.5 px-3 py-2', className)}
	role="group"
	aria-label="Playback"
	data-testid="media-transport"
>
	<!-- The keys are taken in the capture phase, before the slider's own, so an arrow is five seconds. -->
	<div class="px-0.5" onkeydowncapture={onScrubKeys} role="presentation">
		<Slider
			value={known ? Math.min(currentTime, length) : 0}
			min={0}
			max={known ? length : 1}
			step={0.01}
			disabled={!known}
			trackClass="h-2"
			aria-label={`Seek in the ${noun}`}
			aria-valuetext={`${formatClock(currentTime)} of ${formatClock(length)}`}
			onValueChange={seek}
		/>
	</div>
	<div class="flex items-center gap-1">
		<Button
			variant="ghost"
			size="icon-sm"
			aria-label={paused ? 'Play' : 'Pause'}
			title={paused ? 'Play (Space)' : 'Pause (Space)'}
			disabled={!known}
			onclick={() => (paused = !paused)}
		>
			{#if paused}
				<Play class="size-4 fill-current" />
			{:else}
				<Pause class="size-4 fill-current" />
			{/if}
		</Button>
		<span
			class="text-muted-foreground px-1 text-xs whitespace-nowrap tabular-nums"
			data-testid="clock"
		>
			<span class="text-foreground">{formatClock(currentTime)}</span>
			<span aria-hidden="true">/</span><span class="sr-only">of</span>
			{formatClock(length)}
		</span>
		<span class="flex-1"></span>
		<Button
			variant="ghost"
			size="xs"
			class="min-w-10 tabular-nums"
			aria-label={`Playback speed ${speedLabel(playbackRate)}; change to ${speedLabel(nextSpeed)}`}
			title="Playback speed"
			onclick={() => (playbackRate = nextSpeed)}
		>
			{speedLabel(playbackRate)}
		</Button>
		<Button
			variant="ghost"
			size="icon-sm"
			aria-label={silent ? 'Unmute' : 'Mute'}
			aria-pressed={silent}
			title={silent ? 'Unmute (M)' : 'Mute (M)'}
			onclick={() => {
				if (silent && volume === 0) volume = 1;
				muted = !silent;
			}}
		>
			{#if silent}
				<VolumeX class="size-4" />
			{:else if volume < 0.5}
				<Volume1 class="size-4" />
			{:else}
				<Volume2 class="size-4" />
			{/if}
		</Button>
		<!-- The volume slider where the bar has room for it; mute is always there. -->
		<div class="hidden w-20 @xs:block">
			<Slider
				value={silent ? 0 : volume}
				min={0}
				max={1}
				step={0.05}
				aria-label="Volume"
				aria-valuetext={`${Math.round((silent ? 0 : volume) * 100)}%`}
				onValueChange={(v) => {
					volume = v;
					muted = v === 0;
				}}
			/>
		</div>
		{@render actions?.()}
	</div>
</div>
