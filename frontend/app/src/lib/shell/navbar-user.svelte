<script lang="ts">
	import { Avatar, AvatarFallback } from '@riksarkivet/ui/avatar';
	import * as DropdownMenu from '@riksarkivet/ui/dropdown-menu';
	import MenuLink from './menu-link.svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { AccentDialog, AppearanceMenu } from '@riksarkivet/ui/appearance';
	import { LogIn, LogOut } from '@lucide/svelte';
	import type { NavUser } from './nav-config.ts';

	// The shell's identity and appearance control, at the right of the top row: an avatar trigger and its
	// dropdown (identity, the person's mode, theme and accent, the auth control). Context-free (no sidebar
	// dependency) so it renders on every page. The
	// identity flows in as a plain prop from the app's root layout load (single-sourced via
	// the app's sessionToUser) — the shared shell never imports app data.
	//
	// `authEnabled` drives the auth affordance: off → the local identity,
	// no auth UI; on + signed in → identity + Sign out; on + signed out (user null) → a "Sign in" prompt.
	// Login/logout are the app's own `/auth/*` pages: the session lives in the page's memory, so they are
	// soft navigations — a document load would drop it.
	let {
		user = null,
		authEnabled = false,
		pathname = '',
		link = (path: string) => path,
	}: {
		user?: NavUser | null;
		authEnabled?: boolean;
		pathname?: string;
		/** Resolves an app path to its href (`$app/paths`' `resolve`, which the shell's caller owns). */
		link?: (path: string) => string;
	} = $props();

	const signedIn = $derived(!!user);
	// The identity shown on the trigger. NEVER invented: in a governance UI the point is knowing who
	// you are, so a signed-out reader is shown as signed out in both auth modes; only the cue differs
	// (a live sign-in link, or a statement that this stack signs nobody in).
	const identity = $derived(
		user ??
			(authEnabled
				? { name: 'Sign in', email: 'not signed in', initials: '?' }
				: { name: 'Not signed in', email: 'auth disabled on this stack', initials: '—' }),
	);
	// Return to the app the user signed in from (validated as a local path in the login route).
	const loginHref = $derived(link(`/auth/login?redirect=${encodeURIComponent(pathname || '/')}`));
	// The accent's dialog outlives the menu that opens it.
	let accentOpen = $state(false);
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger>
		{#snippet child({ props })}
			<Button {...props} variant="ghost" size="icon" aria-label="Account">
				<Avatar class="size-7">
					<AvatarFallback class="bg-secondary text-secondary-foreground text-xs tracking-normal"
						>{identity.initials ?? '—'}</AvatarFallback
					>
				</Avatar>
			</Button>
		{/snippet}
	</DropdownMenu.Trigger>
	<DropdownMenu.Content class="min-w-64" side="bottom" align="end" sideOffset={6}>
		<DropdownMenu.Label class="text-foreground p-0 tracking-normal normal-case">
			<div class="flex items-center gap-2.5 px-2 py-2 text-left text-sm">
				<Avatar class="size-8">
					<AvatarFallback class="bg-secondary text-secondary-foreground">
						{identity.initials ?? '—'}
					</AvatarFallback>
				</Avatar>
				<div class="grid flex-1 text-left text-sm leading-tight">
					<span class="truncate">{identity.name}</span>
					<span class="text-muted-foreground truncate text-xs">{identity.email ?? 'local'}</span>
				</div>
			</div>
		</DropdownMenu.Label>
		<DropdownMenu.Separator />
		<AppearanceMenu oncustom={() => (accentOpen = true)} />
		{#if authEnabled}
			<DropdownMenu.Separator />
			{#if signedIn}
				<!-- The anchor IS the menu item. An anchor nested inside an item would be the menu's first
				     tabbable element, so the menu would open with focus on it and two Enters would sign out. -->
				<MenuLink href={link('/auth/logout')}>
					<LogOut class="size-4" />
					Sign out
				</MenuLink>
			{:else}
				<!-- Returns here after the round-trip. -->
				<MenuLink href={loginHref}>
					<LogIn class="size-4" />
					Sign in
				</MenuLink>
			{/if}
		{/if}
	</DropdownMenu.Content>
</DropdownMenu.Root>

<AccentDialog bind:open={accentOpen} />
