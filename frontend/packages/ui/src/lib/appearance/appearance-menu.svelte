<script lang="ts">
	import { Monitor, Moon, Palette, Sun } from '@lucide/svelte';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import { THEMES, type Mode, type ThemeId } from './core.js';
	import { appearance } from './appearance.svelte.js';
	import ThemeSwatch from './theme-swatch.svelte';

	// The appearance choices, as items of the menu that holds them (the account menu): light, dark or the system's, a
	// shipped theme, or an accent of the person's own (`oncustom` opens its dialog, which lives outside the menu so it
	// stays open once the menu closes). Picking keeps the menu open, so the page shows each choice as it is made.
	let { oncustom }: { oncustom: () => void } = $props();

	const MODES: { value: Mode; label: string; icon: typeof Sun }[] = [
		{ value: 'light', label: 'Light', icon: Sun },
		{ value: 'dark', label: 'Dark', icon: Moon },
		{ value: 'system', label: 'System', icon: Monitor },
	];
	const isMode = (value: string): value is Mode => MODES.some((m) => m.value === value);
	const isTheme = (value: string): value is ThemeId => THEMES.some((t) => t.id === value);
</script>

<DropdownMenu.Label>Mode</DropdownMenu.Label>
<DropdownMenu.RadioGroup
	aria-label="Mode"
	value={appearance.mode}
	onValueChange={(value) => {
		if (isMode(value)) appearance.set({ mode: value });
	}}
>
	{#each MODES as mode (mode.value)}
		<DropdownMenu.RadioItem value={mode.value} closeOnSelect={false}>
			<mode.icon aria-hidden="true" />
			{mode.label}
		</DropdownMenu.RadioItem>
	{/each}
</DropdownMenu.RadioGroup>
<DropdownMenu.Separator />
<DropdownMenu.Label>Theme</DropdownMenu.Label>
<DropdownMenu.RadioGroup
	aria-label="Theme"
	value={appearance.theme}
	onValueChange={(value) => {
		// A theme comes with its own accent: picking one sets a custom accent aside.
		if (isTheme(value)) appearance.set({ theme: value, accent: null });
	}}
>
	{#each THEMES as theme (theme.id)}
		<DropdownMenu.RadioItem value={theme.id} closeOnSelect={false} title={theme.description}>
			<span class="flex-1">{theme.label}</span>
			<ThemeSwatch theme={theme.id} />
		</DropdownMenu.RadioItem>
	{/each}
</DropdownMenu.RadioGroup>
<DropdownMenu.Item class="pl-7" onclick={oncustom}>
	<Palette aria-hidden="true" />
	<span class="flex-1">Accent colour…</span>
	{#if appearance.accent}
		<span class="text-muted-foreground font-mono text-xs">{appearance.accent}</span>
	{/if}
</DropdownMenu.Item>
