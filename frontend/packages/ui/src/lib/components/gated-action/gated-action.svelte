<script lang="ts">
	import type { Snippet } from 'svelte';
	import { on } from 'svelte/events';
	import * as Tooltip from '../tooltip/index.js';
	import { cn } from '../../utils/cn.js';

	// GATED ACTION — the owner ruling (#143): "every gated action SHOWS disabled with its denial
	// reason, never hidden."
	//
	// Hiding an action a user cannot take teaches them the capability does not EXIST. They then ask
	// for a feature that is already there, or conclude the product cannot do it. Showing it disabled,
	// with the reason attached, turns a dead end into a request they can make of whoever can grant it —
	// which in this estate is a real, reachable person, because the reason names the relation and the
	// object (`<subject> lacks <relation> on <object>`, the FGA denial's own wording).
	//
	// TWO THINGS MAKE THIS NON-TRIVIAL, and both are why the pattern belongs here rather than being
	// re-typed at every call site:
	//
	// 1. A `disabled` button fires NO pointer events, so a tooltip on it never opens — the reason
	//    would be attached to something that can never show it. This uses `aria-disabled` plus a
	//    capturing click/keydown guard instead: the control still receives hover and focus (so the
	//    reason is reachable by mouse AND keyboard), while the action genuinely cannot fire.
	// 2. It stays FOCUSABLE on purpose. `disabled` removes a control from the tab order, which hides
	//    the explanation from exactly the users who most need it announced.
	//
	// The guard is capture-phase and calls `stopImmediatePropagation`, so a handler bound directly on
	// the child never runs — a bubble-phase guard would fire after it.
	//
	// 3. The wrapper is the tooltip's trigger, so it must take the pointer and see focus: a
	//    `display: contents` box takes no pointer, so it is a real inline box, and a trigger's own
	//    `focus` handler never hears the control inside (`focus` does not bubble), so the wrapper opens
	//    the tooltip on `focusin` and closes it on `focusout`, which do.
	// 4. The control inside is what a screen reader lands on, so it carries `aria-disabled` and is
	//    described by the reason (`aria-describedby`), set on its first focusable element.

	interface Props {
		/** Whether the caller is allowed. `true` renders the child untouched. */
		allowed: boolean;
		/** WHY it is refused, in the denial's own words. Shown on hover and focus, and announced. */
		reason: string;
		/** Optional label for what is being refused, prepended to the tooltip. */
		action?: string;
		class?: string;
		children: Snippet;
	}

	let { allowed, reason, action, class: className, children }: Props = $props();

	const message = $derived(action ? `${action} — ${reason}` : reason);
	const id = $props.id();
	let open = $state(false);

	function block(node: HTMLElement) {
		const stop = (event: Event) => {
			if (allowed) return;
			event.preventDefault();
			event.stopImmediatePropagation();
		};
		// `on` from `svelte/events`, not `addEventListener`: it preserves Svelte's own delegated-handler
		// ordering, so a child's `onclick=` cannot slip in ahead of this guard, and it returns its own
		// unsubscriber. `{ capture: true }` is the load-bearing half either way.
		const control = node.querySelector<HTMLElement>(
			'button, a[href], input, select, textarea, [tabindex]',
		);
		control?.setAttribute('aria-disabled', 'true');
		control?.setAttribute('aria-describedby', `gated-${id}`);
		const offClick = on(node, 'click', stop, { capture: true });
		const offKey = on(
			node,
			'keydown',
			(event) => {
				if (event.key === 'Enter' || event.key === ' ') stop(event);
			},
			{ capture: true },
		);
		return () => {
			offClick();
			offKey();
			control?.removeAttribute('aria-disabled');
			control?.removeAttribute('aria-describedby');
		};
	}
</script>

{#if allowed}
	{@render children()}
{:else}
	<Tooltip.Provider>
		<Tooltip.Root {open} onOpenChange={(next) => (open = next)}>
			<!-- The `child` snippet, NOT the default trigger. `Tooltip.Trigger` renders a <button> of its
			     own, and the thing being gated is almost always a <button> too — nesting them is invalid
			     HTML, so the browser repairs it by hoisting the inner one out, content shifts, and SSR
			     warns `node_invalid_placement_ssr` followed by a hydration mismatch. (Observed, not
			     theorised: the home settings suite printed exactly that before this snippet existed.)
			     Delegating lets the trigger BE this span, so there is one element and it is not a button. -->
			<Tooltip.Trigger>
				{#snippet child({ props })}
					<!-- An inline box the size of the control, so the pointer reaches it (see 3 above). -->
					<span
						{...props}
						data-slot="gated-action"
						class={cn('inline-flex [&_*]:cursor-not-allowed [&_*]:opacity-50', className)}
						onfocusin={() => (open = true)}
						onfocusout={() => (open = false)}
						{@attach block}
					>
						{@render children()}
					</span>
				{/snippet}
			</Tooltip.Trigger>
			<!-- `role="status"`: the reason is an explanation of the current state, not an alert. -->
			<Tooltip.Content role="status" class="max-w-xs text-xs">{message}</Tooltip.Content>
		</Tooltip.Root>
	</Tooltip.Provider>
	<span id="gated-{id}" class="sr-only">{reason}</span>
{/if}
