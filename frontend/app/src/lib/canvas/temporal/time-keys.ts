/**
 * The keys of an item that has time (a recording, a video), with what each does. One list for the handler that acts on
 * them, the help that names them and the tool rail, which leaves a letter to the time when both would claim it: on such
 * an item a key means one thing.
 */

/** A key of the time canvas and what it does, as the help lists it. */
export interface TimeKey {
	keys: string;
	does: string;
	/** Only on a video. */
	video?: true;
}

export const TIME_KEYS: readonly TimeKey[] = [
	{ keys: 'Space · Shift+Space', does: 'Play or pause · play the selected region' },
	{
		keys: '← →',
		does: 'Step a frame back or on (a video), 100 ms (a recording); Shift a second, Alt 10 ms',
	},
	{
		keys: '[ · ]',
		does: 'Set the selected region’s start · end at the playhead (nothing selected: mark one)',
	},
	{
		keys: ', . · < >',
		does: 'Nudge the selected region’s start · end a frame or 10 ms (Alt: 100 ms)',
	},
	{ keys: 'N · P', does: 'Next · previous region' },
	{ keys: 'L · S', does: 'Loop the selected region · snap edges to the playhead and each other' },
	{
		keys: 'T',
		does: 'Copy the selected track’s box to the next frame, as a keyframe',
		video: true,
	},
	{ keys: 'O', does: 'Mark the selected track outside the picture here, or back in', video: true },
	{ keys: '+ · − · 0', does: 'Zoom the time strip in · out · to the whole' },
];

/** The letters the time canvas takes, which no tool's key may also claim on an item with time. */
export const TIME_LETTERS: ReadonlySet<string> = new Set(['n', 'p', 'l', 's', 't', 'o']);
