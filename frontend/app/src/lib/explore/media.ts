/**
 * A row's media, as the renderers ask for it: its thumbnail, its chunk's frame, its document's media.
 * Each is a signed link from the batched loader (`#lib/api/media.api.ts`), in the dataset the row's own
 * view names; `undefined` until the viewer answered, and when it named no link (the answer then says why,
 * through `mediaLink`).
 */
import { viewForHit, type Row } from '@workbench/explorer-api';
import { mediaLinks } from '#lib/api/media.api.ts';
import type { MediaItem, MediaLink } from '#lib/api/media-links.svelte.ts';

/** The frame item of a row: the key fields after the doc key, which the viewer takes as integers.
 *  Null when one of them is not an integer, which no frame is addressed by. */
function frameItem(row: Row): MediaItem | null {
	const view = viewForHit(row);
	const keys = view.keyFields
		.filter((key) => key !== view.docKeyField)
		.map((key) => Number(row[key]));
	if (!keys.every(Number.isInteger)) return null;
	return { role: 'frame', doc_id: view.docId(row), keys, frame_idx: 0 };
}

const urlOf = (link: MediaLink | undefined): string | undefined => link?.url ?? undefined;

function thumbnailUrl(row: Row): string | undefined {
	const view = viewForHit(row);
	if (!view.hasThumbnail) return undefined;
	return urlOf(mediaLinks.link(view.id, { role: 'thumbnail', doc_id: view.docId(row) }));
}

/**
 * What a tile or a table row shows for a row: its thumbnail when the dataset declares one, else its chunk's frame
 * (when the dataset declares frames and the row names one), else, for a still-image dataset, its document's image.
 * `undefined` while that link is being signed or when the viewer named none; `null` when the dataset has no image to
 * show for the row at all, where a renderer shows the media kind instead of an empty box.
 */
export function previewUrl(row: Row): string | undefined | null {
	const view = viewForHit(row);
	if (view.hasThumbnail) return thumbnailUrl(row);
	const frame = frameItem(row);
	if (frame !== null && view.hasCapability('frames')) return urlOf(mediaLinks.link(view.id, frame));
	if (view.mediaKind === 'image') return urlOf(mediaLink(row));
	return null;
}

export function chunkFrameUrl(row: Row): string | undefined {
	const item = frameItem(row);
	return item === null ? undefined : urlOf(mediaLinks.link(viewForHit(row).id, item));
}

/** The document's media link, or why there is none (`undefined` while it is being signed). */
export function mediaLink(row: Row): MediaLink | undefined {
	const view = viewForHit(row);
	return mediaLinks.link(view.id, { role: 'media', doc_id: view.docId(row) });
}

/** The row's frame link, or why there is none. */
export function frameLink(row: Row): MediaLink | undefined {
	const item = frameItem(row);
	return item === null
		? { url: null, detail: 'the row names no frame' }
		: mediaLinks.link(viewForHit(row).id, item);
}

/** The document's media link once signed, for a player that sets its `src` itself; `fresh` signs a
 *  new one (the element's source stopped serving). */
export function resolveMedia(row: Row, fresh = false): Promise<MediaLink> {
	const view = viewForHit(row);
	return mediaLinks.resolve(view.id, { role: 'media', doc_id: view.docId(row) }, fresh);
}

/** The row's frame link once signed; `fresh` signs a new one (a reader asking again after a failure). */
export function resolveFrame(row: Row, fresh = false): Promise<MediaLink> {
	const item = frameItem(row);
	if (item === null) return Promise.resolve({ url: null, detail: 'the row names no frame' });
	return mediaLinks.resolve(viewForHit(row).id, item, fresh);
}
