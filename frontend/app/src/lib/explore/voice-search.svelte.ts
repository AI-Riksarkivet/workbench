/**
 * Shared state for voice search ("find this voice": query by example).
 *
 * Three concerns live here so any panel can reach them without props:
 *
 *  - `status` / `built`  one `/api/voice/status` probe per dataset gates every voice affordance (hit-card
 *    buttons, the Settings toggle). `probe()` is idempotent; the dataset page's load calls it.
 *
 *  - `request()`  THE entry point for starting a voice search from anywhere: hit cards, the diarization
 *    timeline, the page's `?voice_doc=…&voice_turn=…|voice_speaker=…|voice_t=…` link. Each request goes
 *    to the listener (the explore state, `explore.svelte.ts`), held until one listens.
 *    `requestUpload(file)` is the same for an uploaded clip (the search bar's attach button).
 *
 *  - `includeSameDoc`  the Settings "include the same document" toggle (default OFF, so
 *    `exclude_same_doc=true` server-side); flipping it re-runs an anchored voice search at once.
 */

import { activeView, type VoiceAnchor, type VoiceStatus } from '@workbench/explorer-api';
import { voiceStatus } from '#lib/api/explore.api.ts';

export type VoiceRequest =
	| {
			anchor: VoiceAnchor;
			/** Chip label for the anchor (e.g. the video filename stem). Falls back to
			 *  the response's `doc_id` when omitted (timeline / deep-link callers). */
			label?: string | undefined;
			/** One-shot retry anchor used when `anchor` 404s. ASR chunk spans and
			 *  diarization turns disagree at boundaries, so a text hit's midpoint
			 *  `t`-anchor can land in a mid-speech diarization gap — hit-card passes
			 *  the chunk start here as a second try before an error surfaces. */
			fallback?: VoiceAnchor | undefined;
	  }
	| {
			/** Query-by-uploaded-clip: rank turns against the clip's own voiceprint
			 *  (POST `/api/voice/similar`) instead of a Lance anchor row. */
			upload: File;
			label: string;
	  };

/** What the voice store hands its requests and setting changes to. */
interface VoiceListener {
	take(request: VoiceRequest): void;
	sameDocChanged(): void;
}

class VoiceSearchStore {
	/** `null` until the one-shot status probe resolves. */
	status = $state<VoiceStatus | null>(null);
	/** A request made before anyone listened. */
	#pending: VoiceRequest | null = null;
	#includeSameDoc = $state(false);
	/** Who runs a request and re-runs on a changed setting: the explore state, once it exists. */
	#listener: VoiceListener | null = null;
	/** In-flight uploaded-clip fetches; a counter (not a boolean) so a rapid
	 *  second pick can't clear the spinner while its successor is still
	 *  running. The first upload after a backend restart lazy-loads the voice
	 *  encoder (~5 s), so the attach button must visibly spin. */
	#uploadsInFlight = $state(0);

	#probed = false;

	/** True once the backend reports the voice tables are built. */
	get built(): boolean {
		return this.status?.built ?? false;
	}

	/** Fetch `/api/voice/status` once; later calls are no-ops. A failed probe
	 *  reads as "not built" (all voice UI stays hidden) — never throws. */
	probe(): void {
		if (this.#probed) return;
		this.#probed = true;
		voiceStatus(activeView().id)
			.then((s) => {
				this.status = s;
			})
			.catch(() => {
				this.status = { built: false, turns: 0, speakers: 0 };
			});
	}

	/** Another dataset: its voice tables are its own, so the probe runs again and a request queued for
	 *  the previous one is dropped. */
	reset(): void {
		this.#probed = false;
		this.status = null;
		this.#pending = null;
	}

	/** True while an uploaded-clip search is in flight (attach-button spinner). */
	get uploadPending(): boolean {
		return this.#uploadsInFlight > 0;
	}

	/** Settings toggle: also surface hits from the anchor's own document. */
	get includeSameDoc(): boolean {
		return this.#includeSameDoc;
	}

	set includeSameDoc(next: boolean) {
		if (next === this.#includeSameDoc) return;
		this.#includeSameDoc = next;
		this.#listener?.sameDocChanged();
	}

	/** Install who takes the requests: one taken before is handed over at once. */
	listen(listener: VoiceListener): void {
		this.#listener = listener;
		this.#deliver();
	}

	/** Start a voice search for `anchor` (auto-applies — no submit step). */
	request(anchor: VoiceAnchor, label?: string, fallback?: VoiceAnchor): void {
		this.#pending = { anchor, label, fallback };
		this.#deliver();
	}

	#deliver(): void {
		const request = this.#pending;
		if (!request || !this.#listener) return;
		this.#pending = null;
		this.#listener.take(request);
	}

	/** Start a voice search ranked against an uploaded audio/video clip
	 *  (auto-applies, same pending/chip lifecycle as `request`).
	 *  `exclude_same_doc` doesn't apply — the clip isn't a Lance row. */
	requestUpload(file: File): void {
		this.#pending = { upload: file, label: `uploaded clip: ${file.name}` };
		this.#deliver();
	}

	/** Bracket one uploaded-clip fetch (the explore state wraps its
	 *  `voiceSimilarUpload` call) so `uploadPending` tracks it. */
	beginUpload(): void {
		this.#uploadsInFlight += 1;
	}

	endUpload(): void {
		this.#uploadsInFlight = Math.max(0, this.#uploadsInFlight - 1);
	}
}

export const voiceSearch = new VoiceSearchStore();
