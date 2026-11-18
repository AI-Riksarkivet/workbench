/**
 * The LABEL STREAM — the set of items the canvas can walk while you are labeling.
 *
 * It moves to the next item you claimed. Label Studio and CVAT make the queue itself the navigation
 * set, because the unit of work is the ITEM.
 *
 * WHOSE items. The stream is the tasks CLAIMED BY YOU, not every task in the project. A stream over
 * the whole queue would step you into items another annotator holds a lease on — the canvas would
 * open, you would draw, and the save would be refused by the server (or worse, accepted for someone
 * else's lease). Claimed-by-me is the set you can actually act on, which makes "next" always a legal
 * move rather than sometimes one.
 */

/** The fields of a task this module needs. Structural on purpose: the queue's `TaskDetail` fits, and
 *  so does a test fixture, without either importing the other's schema. */
export interface StreamTask {
	task_id: string;
	state: string;
	assignee?: string | null;
	source: { keys?: string[]; where?: string | null };
	/** Optional, and only the filmstrip reads it: the declared media kind picks the fallback glyph
	 *  when an item has no frame. Optional so a fixture that only exercises ORDERING does not have to
	 *  carry a media block it never looks at. */
	media?: { kind?: string } | null;
}

/**
 * The tasks you can walk, in the order the queue lists them.
 *
 * ORDER IS THE CALLER'S, deliberately: this filters, it does not sort. The queue is already sorted —
 * by the column you chose, with the filters you set — and a stream that re-sorted would mean "next"
 * on the canvas and "next row" in the table are different items. Sorting here would be a second
 * opinion about order, and there is no way for the person to see which one won.
 */
export function claimedStream<T extends StreamTask>(
	tasks: T[],
	me: string | null,
	authEnabled = true,
): T[] {
	const claimed = tasks.filter((t) => t.state === 'claimed');

	// AUTH OFF — there is one anonymous principal, so every claimed item IS yours.
	//
	// "No identity" is two conditions, and only one of them is dangerous: gating on `me` alone would
	// leave the stream empty on every stack that signs nobody in.
	if (!authEnabled) return claimed;

	// AUTH ON, identity unresolved → empty. THIS is the dangerous case: other principals exist, so
	// guessing hands you an item someone else holds a lease on — the canvas opens, you draw, the save
	// is refused. An empty stream degrades to the old no-cross-item behaviour, which is honest.
	if (!me) return [];

	return claimed.filter((t) => t.assignee === me);
}

/** Where the open task sits in the stream, or -1 when it is not in it at all. */
export function streamIndex(stream: StreamTask[], taskId: string | null): number {
	if (!taskId) return -1;
	return stream.findIndex((t) => t.task_id === taskId);
}

/**
 * The neighbour in a direction, or null at the end.
 *
 * It does NOT wrap. Wrapping hides the end of the queue: you press next, land on the item you
 * started from, and cannot tell "you have been round" from "nothing moved". A disabled control at
 * the boundary says you are done with what you claimed, which is information worth having.
 *
 * A task NOT in the stream (index -1) has no neighbours rather than defaulting to the first item —
 * the canvas can be opened on an item you do not hold (from the corpus browser, or after a lease
 * lapsed), and silently teleporting to someone else's first claimed task would be the worst possible
 * answer to pressing an arrow.
 */
export function neighbour(
	stream: StreamTask[],
	taskId: string | null,
	step: 1 | -1,
): StreamTask | null {
	const at = streamIndex(stream, taskId);
	if (at < 0) return null;
	return stream[at + step] ?? null;
}

/**
 * The labeling page of a task in the stream: `/projects/<project>/tasks/<task>`, an app path before it is resolved.
 *
 * It names the task and nothing about it. The page reads the task's own record for its keys, dataset and media kind,
 * so a link cannot point the canvas at an item the task does not hold; and the project rides the path, so the page's
 * way back is that project's queue.
 */
export function taskCanvasHref(task: StreamTask, projectId: string): string {
	return `/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(task.task_id)}`;
}

/**
 * The labeling page of a task walked as part of a pass (`browse`, `review`), its filter carried in the address so the
 * canvas walks the same items the screen listed, and a reload or a shared link walks them again.
 */
export function passHref(
	task: StreamTask,
	projectId: string,
	pass: 'browse' | 'review',
	filter: URLSearchParams = new URLSearchParams(),
): string {
	const query = new URLSearchParams(filter);
	query.delete('tab');
	query.delete('page');
	query.delete('size');
	query.set('pass', pass);
	return `${taskCanvasHref(task, projectId)}?${query}`;
}

/** What the canvas needs to render the stream control: position, bounds, and where the arrows go. */
export interface StreamPosition {
	/** 1-based for display, counted over the whole walk (a stream is one page of it, `window`); 0 when the open item is
	 *  not in the stream. */
	position: number;
	total: number;
	prevHref: string | null;
	nextHref: string | null;
	/** True when the canvas is walking a queue at all — false hides the control entirely. */
	active: boolean;
	/** The stream itself, so the FILMSTRIP renders the very list the arrows step through.
	 *
	 *  Carried here rather than computed again beside the strip: two computations of "your claimed
	 *  items, in queue order" would eventually disagree about what "next" is, and the person would
	 *  have two answers on one screen with no way to tell which was right. One call, both surfaces. */
	items: StreamTask[];
	/** Which item is open, and which queue it belongs to — the strip needs both to mark the active
	 *  tile and to build each tile's href. */
	taskId: string | null;
	projectId: string | null;
}

/** `link` resolves an app path into the href a page renders (`linkWith`, which carries the reader's project). */
export function streamPosition(
	stream: StreamTask[],
	taskId: string | null,
	projectId: string | null,
	link: (path: string) => string = (path) => path,
	window: { offset: number; total: number } = { offset: 0, total: stream.length },
	hrefOf: (task: StreamTask, projectId: string) => string = taskCanvasHref,
): StreamPosition {
	const at = streamIndex(stream, taskId);
	// No project → the canvas was not opened from a queue, so there is no stream to show. Rendering
	// "0 / 0" there would advertise a feature that does not apply to this canvas.
	const active = projectId !== null && at >= 0;
	const prev = active ? neighbour(stream, taskId, -1) : null;
	const next = active ? neighbour(stream, taskId, 1) : null;
	return {
		position: at < 0 ? 0 : window.offset + at + 1,
		total: window.total,
		prevHref: prev && projectId ? link(hrefOf(prev, projectId)) : null,
		nextHref: next && projectId ? link(hrefOf(next, projectId)) : null,
		active,
		// The strip renders whenever there IS a queue, even standing on an item outside it (a lapsed
		// lease, or a canvas opened from the browser while you hold other items). `active` gates the
		// ARROWS, which cannot step from a position that does not exist; the strip has no such
		// problem and hiding it would remove the only way back to your queue.
		items: projectId ? stream : [],
		taskId,
		projectId,
	};
}
