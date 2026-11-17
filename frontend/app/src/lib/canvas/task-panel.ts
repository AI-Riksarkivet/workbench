/**
 * What the canvas's side panel says the person is doing with this item, from the task's state, its holder and
 * submitter, the person and their permissions on the project. Pure, so each case is pinned without a page.
 *
 * The panel offers only the transitions the task's own `legal_events` list and the person's permissions would let
 * through; the door decides (`annotator.projects.machines`).
 */
import type { TaskDetail } from '#lib/projects/types.ts';

export type PanelMode =
	| 'annotating'
	| 'held'
	| 'reviewing'
	| 'waiting'
	| 'changes'
	| 'accepted'
	| 'unclaimed'
	| 'skipped';

export interface PanelState {
	mode: PanelMode;
	/** The panel's title. */
	title: string;
	/** One line under it: whose work, since when. */
	detail: string;
	/** The transitions offered, by event name. */
	actions: string[];
	/** The reviewer's note, when a review sent the item back. */
	note: { by: string; message: string } | null;
}

/** What a principal is called in the panel: the subject after its idp (`dex~ada` reads `ada`). */
const personName = (principal: string | null | undefined): string =>
	!principal ? 'someone' : (principal.split('~').at(-1) ?? principal);

/** The transitions this person may fire from the task's state now, among `wanted`. */
function offered(task: TaskDetail, permissions: readonly string[], wanted: string[]): string[] {
	return task.legal_events
		.filter((e) => wanted.includes(e.event) && permissions.includes(e.permission))
		.map((e) => e.event);
}

export function panelState(
	task: TaskDetail,
	permissions: readonly string[],
	me: string | null,
): PanelState {
	const notes = task.review_notes.filter((n) => n.message);
	const last = notes.at(-1);
	const sentBack =
		last && (last.action === 'request_changes' || task.state === 'changes_requested')
			? { by: personName(last.by), message: last.message }
			: null;
	switch (task.state) {
		case 'claimed':
			if (task.assignee === me)
				return {
					mode: 'annotating',
					title: 'Labeling',
					detail: sentBack
						? `Sent back by ${sentBack.by}: fix and submit again.`
						: 'Your claim: label the item, then submit it for review.',
					actions: offered(task, permissions, ['submit', 'release']),
					note: sentBack,
				};
			return {
				mode: 'held',
				title: `Held by ${personName(task.assignee)}`,
				detail: 'Someone else is labeling this item: you see it as it was last saved.',
				actions: [],
				note: null,
			};
		case 'in_review':
			if (task.submitted_by !== me && permissions.includes('can_review'))
				return {
					mode: 'reviewing',
					title: `Reviewing ${personName(task.submitted_by)}’s work`,
					detail: 'Accept it, or send it back with a note saying what to change.',
					actions: offered(task, permissions, ['accept', 'request_changes']),
					note: null,
				};
			return {
				mode: 'waiting',
				title: 'In review',
				detail:
					task.submitted_by === me
						? 'You submitted this item: a reviewer accepts it or sends it back.'
						: `Submitted by ${personName(task.submitted_by)}, waiting for a reviewer.`,
				actions: [],
				note: null,
			};
		case 'changes_requested':
			return {
				mode: 'changes',
				title: 'Changes requested',
				detail: sentBack
					? `${sentBack.by} sent it back. Claim it to make the changes.`
					: 'A reviewer sent it back. Claim it to make the changes.',
				actions: offered(task, permissions, ['claim']),
				note: sentBack,
			};
		case 'accepted':
			return {
				mode: 'accepted',
				title: 'Accepted',
				detail: task.reviewed_by
					? `Accepted by ${personName(task.reviewed_by)}: the item is done.`
					: 'The item is done.',
				actions: [],
				note: null,
			};
		case 'skipped':
			return {
				mode: 'skipped',
				title: 'Skipped',
				detail: 'This item was skipped; a manager can put it back in the queue.',
				actions: [],
				note: null,
			};
		default:
			return {
				mode: 'unclaimed',
				title: 'Not claimed',
				detail: 'Claim the item to label it.',
				actions: offered(task, permissions, ['claim']),
				note: null,
			};
	}
}
