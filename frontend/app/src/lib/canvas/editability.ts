/**
 * Whether the reader may edit a task's item on the canvas, or why not, from what the queue reads: the reader's
 * permissions on the project (its read's `permissions`), and the task's state, holder and submitter. The doors decide
 * (`annotator.annotations.access`); this only keeps the canvas from offering an edit they would refuse.
 *
 * The holder of a claim edits it, with `can_annotate`; a reviewer edits work in review they did not submit, with
 * `can_review` (a fix before `fix_and_accept`). Everyone else reads.
 */
import { personName } from '#lib/projects/people.ts';
import type { People } from '#lib/projects/types.ts';

export interface EditableTask {
	state: string;
	assignee?: string | null;
	submitted_by?: string | null;
}

/** Null when the reader may edit the item now; otherwise the reason, in words the canvas shows. */
export function readOnlyReason(
	task: EditableTask,
	permissions: readonly string[],
	me: string | null,
	people: People = {},
): string | null {
	if (task.state === 'claimed') {
		if (!permissions.includes('can_annotate'))
			return 'You view this project: the canvas is read-only.';
		if (task.assignee !== me)
			return `This item is held by ${task.assignee ? personName(task.assignee, people) : 'someone else'}: the canvas is read-only.`;
		return null;
	}
	if (task.state === 'in_review') {
		if (!permissions.includes('can_review'))
			return 'This item is in review: the canvas is read-only.';
		if (task.submitted_by === me)
			return 'You submitted this item, and someone else reviews it: the canvas is read-only.';
		return null;
	}
	if (task.state === 'unassigned' || task.state === 'changes_requested')
		return 'Claim this item in the queue to label it: the canvas is read-only.';
	return `This item is ${task.state.replace('_', ' ')}: the canvas is read-only.`;
}
