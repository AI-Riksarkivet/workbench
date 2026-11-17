/**
 * What a task's canvas labels: a picture, or the recording or video its unit is a part of, with what the dataset says
 * of that document's time (its parts and its speakers) and the task's own stretch of it.
 */

/** A part of a document: one of its segments (a speaker's turn, a shot), with what it says. */
export interface Part {
	start: number;
	end: number;
	text: string;
	speaker?: string;
	/** The task's own part, which the view opens on. */
	own: boolean;
}

export type CanvasMedia =
	| { kind: 'image'; url: string }
	| {
			kind: 'audio' | 'video';
			url: string;
			dataset: string;
			docId: string;
			parts: readonly Part[];
			turns: readonly { speaker: string; start: number; end: number }[];
			focus: { start: number; end: number } | null;
	  };
