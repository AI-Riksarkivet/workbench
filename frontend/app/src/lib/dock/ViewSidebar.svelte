<script lang="ts">
	/**
	 * The dataset's saved views: select one, save what is on screen as a new one, or mark one as the view the
	 * dataset opens with (the star). A view is the arrangement of panels, a plain table's set-ups of its
	 * Atlas, Graph and Tree, and the rows it listed (`./store.ts`'s `ViewSnapshot`). (A saved SEARCH is the
	 * Results panel's own, `saved-views`.)
	 *
	 * The modified marker beside the active view is the visible half of the decision `DockViews` makes:
	 * loading a view does NOT redirect autosave at it. Autosave keeps writing the implicit draft, and
	 * the view stays frozen until you press Save. Overwriting on every drag would mean you could never
	 * open a saved arrangement to LOOK at it without destroying it.
	 */
	import { Check, ChevronRight, Plus, Save, Star, Trash2 } from '@lucide/svelte';
	import type { DockViews } from './views.svelte.ts';

	interface Props {
		views: DockViews;
		onselect: (id: string) => void;
	}
	let { views, onselect }: Props = $props();

	let creating = $state(false);
	/** Open on a wide screen; on a narrow one the panels get the height, and the list opens on demand. */
	let open = $state(!window.matchMedia('(width < 768px)').matches);
	let draftName = $state('');

	async function create(): Promise<void> {
		if (draftName.trim() === '') return;
		if (await views.saveAs(draftName)) {
			draftName = '';
			creating = false;
		}
	}
</script>

<aside class="views">
	<header>
		<button
			type="button"
			class="heading"
			aria-expanded={open}
			aria-controls="dock-views"
			onclick={() => (open = !open)}
		>
			<ChevronRight size={12} class={open ? 'rotate-90' : ''} />
			Views
		</button>
		<button
			type="button"
			class="icon"
			title="Save what is on screen as a new view"
			aria-label="New view"
			onclick={() => {
				creating = !creating;
				open = true;
			}}
		>
			<Plus size={14} />
		</button>
	</header>

	<div id="dock-views" class="body" hidden={!open}>
		{#if creating}
			<form
				class="new"
				onsubmit={(e) => {
					e.preventDefault();
					void create();
				}}
			>
				<!-- The field appears on an explicit click, so focusing it is what the click asked for. -->
				<!-- svelte-ignore a11y_autofocus -->
				<input
					bind:value={draftName}
					type="text"
					placeholder="Name this view…"
					aria-label="View name"
					autofocus
				/>
			</form>
		{/if}

		{#if views.lastError !== null}
			<p class="problem">{views.lastError}</p>
		{/if}

		{#if views.phase === 'unreadable'}
			<!-- Never rendered as "no saved views": that reads as empty and invites the user to recreate
		     work that is still there. -->
			<p class="problem">
				Your saved views could not be read ({views.detail}). They have not been changed.
			</p>
			<button
				type="button"
				class="reset"
				disabled={views.busy}
				title="Discard the saved views that could not be read, so views can be saved again"
				onclick={() => void views.reset()}
			>
				Reset views
			</button>
		{:else if views.views.length === 0}
			<p class="empty">No saved views of this dataset yet. Arrange the dock, then press +.</p>
		{:else}
			<ul>
				{#each views.views as v (v.id)}
					{@const isActive = v.id === views.activeId}
					<li>
						<button type="button" class={['row', { on: isActive }]} onclick={() => onselect(v.id)}>
							{#if isActive}<Check size={13} />{/if}
							<span class="name">{v.name}</span>
							<!-- The whole point of the state model, in one dot. -->
							{#if isActive && views.dirty}<span class="dot" title="Modified since it was saved"
									>●</span
								>{/if}
						</button>
						<button
							type="button"
							class={['icon', { star: v.isDefault }]}
							title={v.isDefault
								? 'The dataset opens with this view; press to open it with none'
								: 'Open the dataset with this view'}
							aria-label="{v.name}: the default view"
							aria-pressed={v.isDefault}
							disabled={views.busy}
							onclick={() => void views.toggleDefault(v.id)}
						>
							<Star size={13} fill={v.isDefault ? 'currentColor' : 'none'} />
						</button>
						{#if isActive}
							<button
								type="button"
								class="icon"
								title="Save what is on screen into this view"
								aria-label="Save view"
								disabled={!views.dirty || views.busy}
								onclick={() => void views.save()}
							>
								<Save size={13} />
							</button>
						{/if}
						<button
							type="button"
							class="icon"
							title="Delete this view"
							aria-label="Delete view"
							disabled={views.busy}
							onclick={() => void views.remove(v.id)}
						>
							<Trash2 size={13} />
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</aside>

<style>
	.views {
		display: flex;
		flex: 0 0 auto;
		flex-direction: column;
		gap: 4px;
		width: 200px;
		padding: 8px;
		border-inline-end: 1px solid var(--border);
		background: var(--background);
		overflow-y: auto;
	}
	/* Above the dock on a narrow screen (the workbench page stacks them): full width, and short
	   enough to leave the panels the height. */
	@media (width < 768px) {
		.views {
			width: auto;
			max-height: 9rem;
			border-inline-end: 0;
			border-bottom: 1px solid var(--border);
		}
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.heading {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 2px 4px 2px 0;
		border: 0;
		background: none;
		font-size: 11px;
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.heading:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
	.body {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.body[hidden] {
		display: none;
	}
	ul {
		display: flex;
		flex-direction: column;
		gap: 1px;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	li {
		display: flex;
		align-items: center;
		gap: 2px;
	}
	.row {
		display: flex;
		flex: 1 1 auto;
		align-items: center;
		gap: 6px;
		min-width: 0;
		padding: 5px 6px;
		border: none;
		border-radius: calc(var(--radius) - 6px);
		background: transparent;
		color: var(--foreground);
		font-size: 12px;
		text-align: start;
		cursor: pointer;
	}
	.row:hover {
		background: var(--accent);
	}
	.row.on {
		color: var(--primary);
	}
	.star {
		color: var(--primary);
		opacity: 1;
	}
	.row:focus-visible,
	.icon:focus-visible,
	input:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dot {
		flex: 0 0 auto;
		font-size: 9px;
		color: var(--warning, var(--muted-foreground));
	}
	.icon {
		display: grid;
		place-items: center;
		width: 22px;
		height: 22px;
		border: none;
		border-radius: calc(var(--radius) - 6px);
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.icon:hover:not(:disabled) {
		color: var(--foreground);
		background: var(--accent);
	}
	.icon:disabled {
		opacity: 0.35;
		cursor: default;
	}
	input {
		width: 100%;
		padding: 4px 6px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 6px);
		background: var(--background);
		color: var(--foreground);
		font-size: 12px;
	}
	.empty,
	.problem {
		margin: 4px 2px;
		font-size: 11px;
		color: var(--muted-foreground);
	}
	.problem {
		color: var(--destructive);
	}
	.reset {
		align-self: flex-start;
		margin: 0 2px;
		padding: 2px 8px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 6px);
		background: transparent;
		color: var(--foreground);
		font-size: 11px;
		cursor: pointer;
	}
	.reset:hover:not(:disabled) {
		background: var(--accent);
	}
	.reset:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
</style>
