<script lang="ts">
	// Who may do what on this project. The rungs already existed in the model, concentric and
	// directly assignable — what did not exist was any way to grant one, so a project's creator got
	// `owner` at create and nobody else could be added. Every collaborative project therefore needed
	// someone with direct store access, which is the shape where people end up sharing an account.
	//
	// `can_manage`-gated server-side on READ as well as write: who has access to a thing names the
	// people worth phishing.
	import { Trash2, UserPlus } from '@lucide/svelte';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import Subject from '#lib/identity/Subject.svelte';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';

	import {
		fetchCandidates,
		fetchMembers,
		grantMember,
		revokeMember,
	} from '#lib/api/projects.api.ts';
	import PersonName from './PersonName.svelte';
	import { shortPrincipal } from './people.ts';
	import type { People } from './types.ts';

	let { projectId }: { projectId: string } = $props();

	let members = $state<{ user: string; relation: string }[]>([]);
	/** The members' names, where the annotator keeps one (`people.ts`). */
	let people = $state<People>({});
	/** Supplied by the SERVER so this never keeps a second copy of the model's ladder. */
	let grantable = $state<string[]>([]);
	let loaded = $state(false);
	/** The READ failed — there is no list to show, so the error replaces it. */
	let loadError = $state('');
	/** A grant or revoke failed — the list is still valid and must STAY, with the refusal beside it.
	 *
	 *  One `error` for both conflated them: a refused revoke hid the very list the manager needs to
	 *  act on the refusal ("grant another owner first" is impossible if you cannot see who there is).
	 *  Caught by the e2e, not by looking. */
	let actionError = $state('');
	let who = $state('');
	let rung = $state('annotator');
	let busy = $state(false);
	/** The people offered by name: those the annotator knows who hold a role on a project of this organisation the
	 *  manager manages (`member_candidates`), read once the list is shown. Anyone else is granted by principal. */
	const candidatesRead = $derived(loaded && !loadError ? fetchCandidates({ projectId }) : null);
	const offered = $derived(candidatesRead?.current?.ok ? candidatesRead.current.data.people : []);
	const incomplete = $derived(
		candidatesRead?.current?.ok ? candidatesRead.current.data.truncated : false,
	);

	async function load(): Promise<void> {
		const result = await fetchMembers({ projectId });
		loaded = true;
		if (!result.ok) {
			// The server's words. A 403 here means the viewer is not a manager, which is a different
			// thing from "this project has no members" and must not render as an empty list.
			loadError = result.detail;
			return;
		}
		loadError = '';
		members = result.data.members;
		people = result.data.people;
		grantable = result.data.grantable;
		if (grantable.length > 0 && !grantable.includes(rung)) rung = grantable[0]!;
	}

	async function grant(): Promise<void> {
		if (busy || !who.trim()) return;
		busy = true;
		const result = await grantMember({ projectId, user: who.trim(), relation: rung });
		busy = false;
		if (result.ok) {
			members = result.data.members;
			people = result.data.people;
			who = '';
			actionError = '';
		} else actionError = result.detail;
	}

	async function revoke(user: string, relation: string): Promise<void> {
		if (busy) return;
		busy = true;
		const result = await revokeMember({ projectId, user, relation });
		busy = false;
		// The refusal that matters arrives here: removing the last owner/manager is a named 409, not
		// a silent no-op, because a project nobody can administer cannot be undone from inside.
		if (result.ok) {
			members = result.data.members;
			people = result.data.people;
			actionError = '';
		} else actionError = result.detail;
	}
</script>

<section class="flex flex-col gap-2" data-testid="members-panel">
	<div class="flex items-center gap-2">
		<h2 class="text-muted-foreground text-xs font-medium uppercase">Access</h2>
		{#if !loaded}
			<Button variant="ghost" size="sm" data-testid="load-members" onclick={() => void load()}>
				Show who has access
			</Button>
		{/if}
	</div>

	{#if loadError || actionError}
		<p class="text-destructive text-sm" data-testid="members-error">{loadError || actionError}</p>
	{/if}

	<!-- Gated on the LOAD only. A refused action leaves the list intact, because acting on the
	     refusal ("grant another owner first") requires seeing who there is. -->
	{#if loaded && !loadError}
		<ul class="flex flex-col gap-1">
			{#each members as m (m.user + m.relation)}
				{@const principal = m.user.replace(/^user:/, '')}
				{@const called = people[principal] ?? shortPrincipal(principal)}
				<li class="flex items-center gap-2 text-sm" data-testid="member-row">
					<!-- By the name the annotator keeps from their own token (they signed in or wrote here); someone
					     granted who has not signed in yet is their principal, shortened by `Subject`, the full value its
					     tooltip, which a revoke sends back. -->
					{#if people[principal]}
						<PersonName {principal} {people} />
					{:else}
						<Subject value={m.user} class="text-xs" />
						<span class="text-muted-foreground text-xs" data-testid="member-not-signed-in"
							>has not signed in here yet</span
						>
					{/if}
					<Badge variant="outline">{m.relation}</Badge>
					<Button
						variant="ghost"
						size="icon-sm"
						class="ml-auto"
						disabled={busy}
						data-testid="revoke-member"
						aria-label="Revoke {m.relation} from {called}"
						title="Revoke {m.relation} from {called}"
						onclick={() => void revoke(m.user, m.relation)}
					>
						<Trash2 class="size-3.5" />
					</Button>
				</li>
			{:else}
				<li class="text-muted-foreground text-sm">No direct grants yet.</li>
			{/each}
		</ul>

		<form
			class="flex flex-wrap items-center gap-2"
			onsubmit={(e) => {
				e.preventDefault();
				void grant();
			}}
		>
			{#if offered.length > 0}
				<Select
					bind:value={who}
					ariaLabel="Person to grant"
					options={[
						{ value: '', label: 'Choose a person…' },
						...offered.map((c) => ({ value: c.principal, label: c.name })),
					]}
				/>
			{/if}
			<Input
				bind:value={who}
				placeholder={offered.length > 0
					? 'or their principal (idp~user id)'
					: 'their principal (idp~user id)'}
				aria-label="Principal to grant"
				class="h-8 w-56"
			/>
			<Select
				bind:value={rung}
				ariaLabel="Role to grant"
				options={grantable.map((g) => ({ value: g, label: g }))}
			/>
			<Button type="submit" size="sm" disabled={busy || !who.trim()} data-testid="grant-member">
				<UserPlus class="size-3.5" /> Grant
			</Button>
		</form>
		<!-- Said out loud: a tenant admin reaches this project through `admin from tenant`, is not a
		     direct grant, and cannot be revoked here. Listing them with a remove button that cannot
		     work would be worse than not listing them. -->
		{#if incomplete}
			<p class="text-muted-foreground text-xs" data-testid="candidates-incomplete">
				Not everyone you could grant is listed: type a principal if the person is not listed.
			</p>
		{/if}
		<p class="text-muted-foreground text-xs">
			Direct grants only. An organisation's admins own every project of it, and are managed with the
			organisation.
		</p>
	{/if}
</section>
