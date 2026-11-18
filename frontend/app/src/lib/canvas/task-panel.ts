/**
 * What the canvas's side panel says the person is doing with this item, from the task's state, its holder and
 * submitter, the person and their permissions on the project, and whether they are browsing. Pure, so each case is
 * pinned without a page.
 *
 * The panel offers only the transitions the task's own `legal_events` list: what the annotator says this person may
 * fire on it now (their role and the identity rules, `annotator.projects.machines.caller_events`), the rule the data
 * manager's table and its quick view read too. The door still decides.
 */
import { personName as nameIn } from '#lib/projects/people.ts';
import { VERDICTS } from '#lib/projects/presentation.ts';
import type { TaskDetail } from '#lib/projects/types.ts';

export type PanelMode =
	| 'annotating'
	| 'held'
	| 'reviewing'
	| 'waiting'
	| 'changes'
	| 'accepted'
	| 'unclaimed'
	| 'skipped'
	| 'browsing';

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

/** The transitions the annotator says this person may fire on the task now, among `wanted`. */
function offered(task: TaskDetail, wanted: string[]): string[] {
	return task.legal_events.filter((e) => wanted.includes(e.event)).map((e) => e.event);
}

/** The sentence's first letter upper-cased. */
const sentence = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);

export function panelState(
	task: TaskDetail,
	permissions: readonly string[],
	me: string | null,
	{ browsing = false }: { browsing?: boolean } = {},
): PanelState {
	// People by the name they last wrote under, where the annotator keeps one (`#lib/projects/people.ts`).
	const personName = (principal: string | null | undefined): string =>
		nameIn(principal, task.people);
	const notes = task.review_notes.filter((n) => n.message);
	const last = notes.at(-1);
	const sentBack =
		last && (last.action === 'request_changes' || task.state === 'changes_requested')
			? { by: personName(last.by), message: last.message }
			: null;
	// The verdict as the data manager's table words it ("fixed & accepted by …").
	const verdict = task.reviewed_by
		? `${sentence(VERDICTS[task.review_action ?? ''] ?? 'accepted')} by ${personName(task.reviewed_by)}`
		: null;
	if (browsing) return browsed(task, personName, verdict, sentBack);
	switch (task.state) {
		case 'claimed':
			if (task.assignee === me)
				return {
					mode: 'annotating',
					title: 'Labeling',
					detail: sentBack
						? `Sent back by ${sentBack.by}: fix and submit again.`
						: 'Your claim: label the item, then submit it for review.',
					actions: offered(task, ['submit', 'release']),
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
					detail:
						'Accept it, fix it here and accept it with your fixes, or send it back with a note saying what to change.',
					actions: offered(task, ['accept', 'fix_and_accept', 'request_changes']),
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
				actions: offered(task, ['claim']),
				note: sentBack,
			};
		case 'accepted':
			return {
				mode: 'accepted',
				title: 'Accepted',
				detail: verdict ? `${verdict}: the item is done.` : 'The item is done.',
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
				actions: offered(task, ['claim']),
				note: null,
			};
	}
}

/** A browse pass's panel, whatever the item's state: where the item stands and whose work it is, read-only, offering
 *  nothing (a browse claims and judges nothing). */
function browsed(
	task: TaskDetail,
	personName: (principal: string | null | undefined) => string,
	verdict: string | null,
	sentBack: { by: string; message: string } | null,
): PanelState {
	const where: Record<TaskDetail['state'], string> = {
		unassigned: 'Not claimed yet',
		claimed: `Being labeled by ${personName(task.assignee)}`,
		in_review: `Submitted by ${personName(task.submitted_by)}, waiting for a reviewer`,
		changes_requested: `Sent back${sentBack ? ` by ${sentBack.by}` : ''}, waiting for its annotator`,
		accepted: verdict ?? 'Accepted',
		skipped: 'Skipped',
	};
	return {
		mode: 'browsing',
		title: 'Browsing',
		detail: `${where[task.state]}. You see the item as it was last saved; browsing changes nothing.`,
		actions: [],
		note: sentBack,
	};
}

/** The keys a reviewer judges an item by (with Shift, so the canvas's own A and R keep judging one annotation). */
export const REVIEW_KEYS: Record<string, string> = {
	a: 'accept',
	f: 'fix_and_accept',
	r: 'request_changes',
};
