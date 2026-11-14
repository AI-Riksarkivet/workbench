<script lang="ts">
	import type { LayoutServerData } from './$types';
	import '../app.css';
	import { browser } from '$app/environment';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { Toaster } from 'svelte-sonner';
	import { AppShell } from '@workbench/ui/shell';
	import { STUDIO_ZONE_NAV } from '$lib/nav';
	import type { Snippet } from 'svelte';
	import NodeLibrary from '$lib/flows/NodeLibrary.svelte';
	let { children, data }: { children: Snippet; data: LayoutServerData } = $props();

	// The flow builder's node library rides the shell's `sidebarContent` seam — rail content UNDER
	// the route row, the same slot the lakehouse workbench's saved-views list uses. The canvas is the
	// zone ROOT now, so that is the one route carrying it; the check stays explicit rather than
	// unconditional so a future studio page does not silently inherit Serve discovery. NodeLibrary is
	// deliberately store-free (see palette.ts), so importing it here does not drag the graph
	// singleton — and its localStorage read — into any other route.
	const onCanvasPage = $derived(page.url.pathname.replace(/\/$/, '').endsWith('/studio'));

	// Animate soft client-side navs via the View Transitions API. onNavigate
	// registers a callback and is SSR-safe; the document check guards browsers
	// without support. Cross-document MFE navs are handled by the `@view-transition`
	// rule in the shared @workbench/ui tokens stylesheet.
	onNavigate((navigation) => {
		if (!document.startViewTransition) return;
		return new Promise((resolve) => {
			document.startViewTransition(async () => {
				resolve();
				await navigation.complete;
			});
		});
	});
</script>

<!-- ModeWatcher sets `class="dark"` on <html> so the .dark theme tokens apply —
     without it the shared sidebar renders unstyled. Identical wiring to every
     other microfrontend (compute/storage/monolith). -->
<ModeWatcher defaultMode="dark" />
{#if browser}
	<Toaster />
{/if}

<!-- The shared AppShell (one grouped sidebar) from @workbench/ui — identical to every
     other microfrontend, zero drift. `base` (=/studio) frames the breadcrumb. -->
<AppShell
	pathname={page.url.pathname}
	user={data.user}
	authEnabled={data.authEnabled}
	me={data.me}
	meLoading={false}
	project={data.activeProject ? { name: data.activeProject } : undefined}
	zoneNav={STUDIO_ZONE_NAV}
	sidebarContent={onCanvasPage ? nodeLibrary : undefined}
>
	{@render children()}
</AppShell>

{#snippet nodeLibrary()}
	<NodeLibrary />
{/snippet}
