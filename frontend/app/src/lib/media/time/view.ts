/**
 * A view of a recording's time axis: the stretch `[start, end)` seconds drawn across a width, zoomed and scrolled.
 * Every lane under a recording (the waveform, the segments, the regions, a track's keyframes) is drawn through the same
 * view, so a moment is at the same x in all of them. Pure.
 */

export interface TimeView {
	start: number;
	end: number;
}

/** The shortest stretch a view shows: a tenth of a second, a few frames of video. */
const MIN_SPAN = 0.1;

/** The whole recording, or one second while its length is not known. */
export const wholeView = (duration: number): TimeView => ({
	start: 0,
	end: duration > 0 ? duration : 1,
});

/** `view` held inside `[0, duration]`, no shorter than `MIN_SPAN` (nor longer than the recording). */
export function clampView(view: TimeView, duration: number): TimeView {
	const total = duration > 0 ? duration : 1;
	const span = Math.min(total, Math.max(MIN_SPAN, view.end - view.start));
	const start = Math.min(Math.max(0, view.start), total - span);
	return { start, end: start + span };
}

/** The view zoomed by `factor` (above 1 closer) about the moment `at`, which stays where it was on the axis. */
export function zoomAt(view: TimeView, at: number, factor: number, duration: number): TimeView {
	const span = (view.end - view.start) / factor;
	const f = view.end > view.start ? (at - view.start) / (view.end - view.start) : 0.5;
	return clampView({ start: at - f * span, end: at - f * span + span }, duration);
}

/** The view moved by `seconds` (later when positive). */
export const panBy = (view: TimeView, seconds: number, duration: number): TimeView =>
	clampView({ start: view.start + seconds, end: view.end + seconds }, duration);

/** The view moved just enough to show `t`, with `margin` of the view's span kept on the side it comes in from. */
export function follow(view: TimeView, t: number, duration: number, margin = 0.1): TimeView {
	const span = view.end - view.start;
	if (t < view.start)
		return clampView({ start: t - span * margin, end: t - span * margin + span }, duration);
	if (t > view.end)
		return clampView({ start: t - span * (1 - margin), end: t + span * margin }, duration);
	return view;
}

/** Where `t` falls across a width of `width` pixels. */
export const xOf = (view: TimeView, t: number, width: number): number =>
	((t - view.start) / (view.end - view.start)) * width;

/** The moment at `x` pixels across a width of `width`. */
export const timeOfX = (view: TimeView, x: number, width: number): number =>
	view.start + (x / width) * (view.end - view.start);
