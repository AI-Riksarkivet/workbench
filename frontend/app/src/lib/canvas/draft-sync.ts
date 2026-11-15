/**
 * Canvas → task draft: after a task's canvas saves its unit, the unit's annotation rows are copied into the task's
 * DRAFT, the document the publish reads, so shapes drawn on the canvas travel into the published table.
 *
 * One write per save, guarded by the draft's revision: the draft is read first (null before the first is written), and
 * a stale revision is a 409 from the door rather than an overwrite. A failed read writes nothing, since a draft written
 * without its guard could clobber whatever is there.
 */
import type { Table } from 'apache-arrow';
import { fetchDraft, saveDraft } from '#lib/api/projects.api.ts';
import { rowsToShapes } from './draft-shapes.ts';
import type { AnnoLink } from './controller.svelte.ts';

/** Snapshot the table into the task's draft: null on success, or the failure in the door's words, which the canvas
 *  says out loud (work that looks saved but never publishes is the outcome this exists to prevent). */
export async function syncTaskDraft(
	taskId: string,
	table: Table,
	links: AnnoLink[] = [],
): Promise<string | null> {
	const current = await fetchDraft(taskId);
	if (!current.ok) return `could not read the task draft: ${current.detail}`;
	const saved = await saveDraft(taskId, {
		shapes: rowsToShapes(table),
		// Relations ride the draft beside the shapes: a task declaring a required relation refuses a submit
		// without them.
		links,
		...(current.data === null ? {} : { base_revision: current.data.revision }),
	});
	return saved.ok ? null : saved.detail;
}
