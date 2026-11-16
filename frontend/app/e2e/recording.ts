import type { Page } from '@playwright/test';

// A real recording for the specs that play one: the stand-in viewer's signed links serve pictures, and an element
// handed a picture fails to decode it.

/** A silent mono WAV, `seconds` long, at 8 kHz: a real recording for the element to play. */
function silentWav(seconds: number): Buffer {
	const rate = 8000;
	const data = rate * seconds * 2;
	const wav = Buffer.alloc(44 + data);
	wav.write('RIFF', 0);
	wav.writeUInt32LE(36 + data, 4);
	wav.write('WAVEfmt ', 8);
	wav.writeUInt32LE(16, 16);
	wav.writeUInt16LE(1, 20);
	wav.writeUInt16LE(1, 22);
	wav.writeUInt32LE(rate, 24);
	wav.writeUInt32LE(rate * 2, 28);
	wav.writeUInt16LE(2, 32);
	wav.writeUInt16LE(16, 34);
	wav.write('data', 36);
	wav.writeUInt32LE(data, 40);
	return wav;
}

/** Every signed link on the page serves a thirty-second silent WAV, in the byte ranges the element asks for, as a media
 *  server does, so it can seek. */
export async function serveRecording(page: Page): Promise<void> {
	const wav = silentWav(30);
	await page.route('**/api/viewer/api/signed-media/*', (route) => {
		const range = /bytes=(\d+)-(\d*)/.exec(route.request().headers()['range'] ?? '');
		if (!range)
			return route.fulfill({
				body: wav,
				contentType: 'audio/wav',
				headers: { 'accept-ranges': 'bytes' },
			});
		const from = Number(range[1]);
		const to = range[2] ? Math.min(Number(range[2]), wav.length - 1) : wav.length - 1;
		return route.fulfill({
			status: 206,
			body: wav.subarray(from, to + 1),
			contentType: 'audio/wav',
			headers: { 'accept-ranges': 'bytes', 'content-range': `bytes ${from}-${to}/${wav.length}` },
		});
	});
}
