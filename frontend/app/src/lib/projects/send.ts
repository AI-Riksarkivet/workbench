/**
 * Turning a selection (a dataset's search results, or a region of its atlas) into items sent to a project. Pure, so the
 * rules that decide what a project receives are pinned without a page; the dangerous one is the partial selection.
 */
import type { SendItem } from './types.ts';

/** The annotator's own bound on one send (`SendItemsRequest.items`, `max_length=1000`); a send of more is refused. */
export const SEND_TASK_CAP = 1000;

/** The pre-annotation a bulk label attaches: ONE whole-item tag, since a region of a map has no geometry to draw. No
 *  `source`: the annotator stamps it, and a sender able to write it could mark items nobody looked at as human work.
 *  Absent when no label was chosen, which sends the items blank for someone to draw on. */
export function predictionFor(label: string): { shape_type: string; label: string }[] | undefined {
	const name = label.trim();
	return name ? [{ shape_type: 'tag', label: name }] : undefined;
}

/**
 * Why this selection cannot be sent, or `null` when it can.
 *
 * The atlas lists a selection's rows capped while it reports the TRUE selection size, so a lasso over five thousand
 * points lists a thousand. Sending the listed rows would queue a fifth of what was selected and report success, while
 * the count on screen said five thousand: refused, with both numbers. Checked apart from the cap, so changing either
 * bound cannot silently truncate a send.
 */
export function sendRefusal(opts: {
	fetched: number;
	total: number;
	cap?: number;
	projectChosen?: boolean;
}): string | null {
	const { fetched, total, cap = SEND_TASK_CAP, projectChosen = true } = opts;
	if (total === 0) return 'Search, or select a region of the atlas, to have items to send.';
	if (total > cap) {
		return `${total.toLocaleString()} items selected exceeds the ${cap.toLocaleString()} one send takes: narrow the selection.`;
	}
	if (fetched < total) {
		return `Only ${fetched.toLocaleString()} of ${total.toLocaleString()} selected items have loaded: narrow the selection so all of them can be sent.`;
	}
	if (!projectChosen) return 'Choose a project to send to.';
	return null;
}

/** The key path of each row, joined as every key the explorer builds is (`DatasetView.keyPath`, each part
 *  percent-encoded), de-duplicated (two tasks for one item is two people labelling it), and blank ones dropped. */
export function selectionKeys<T>(rows: readonly T[], keyPath: (row: T) => string[]): string[] {
	return [...new Set(rows.map((row) => keyPath(row).join('/')).filter(Boolean))];
}

/** Where a send's items come from: the dataset, its version when captured (the reproducibility pin a publish records;
 *  null is an honest "not captured"), and the kind of media its rows carry. */
export interface SendSource {
	dataset: string;
	version: number | null;
	mediaKind: 'image' | 'audio' | 'video' | 'none';
}

/** The items of a send: one per key, with the bulk label when one was chosen. The media is named by its kind alone (a
 *  dataset of text rows sends `text`): the viewer signs a task's picture from its key, never from a link the sender
 *  supplies. */
export function sendItemsFor(keys: readonly string[], from: SendSource, label: string): SendItem[] {
	const prediction = predictionFor(label);
	const kind = from.mediaKind === 'none' ? 'text' : from.mediaKind;
	return keys.map((key) => ({
		source: { kind: 'chunks', keys: [key], where: from.dataset, dataset_version: from.version },
		media: { kind },
		...(prediction ? { prediction } : {}),
	}));
}

/** The keys a send carries while a sampling proposal is shown: the proposed ones the person left ticked, or every
 *  candidate when they asked to send the rest too; with no proposal, every candidate. */
export function keysToSend(
	keys: readonly string[],
	proposal: { ticked: ReadonlySet<string>; everyCandidate: boolean } | null,
): string[] {
	if (proposal === null || proposal.everyCandidate) return [...keys];
	return keys.filter((key) => proposal.ticked.has(key));
}
