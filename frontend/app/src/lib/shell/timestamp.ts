import { formatAbsolute, toDate } from '@workbench/ui/utils';

const pad = (n: number): string => String(n).padStart(2, '0');

/** `UTC+01:00` for a zone `minutesEast` of UTC; `UTC` for UTC itself. */
function utcOffsetLabel(minutesEast: number): string {
	if (minutesEast === 0) return 'UTC';
	const sign = minutesEast > 0 ? '+' : '-';
	const abs = Math.abs(minutesEast);
	return `UTC${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

/** `YYYY-MM-DD HH:mm:ss UTC+01:00`: the full local date and time with the zone it is read in. `—`
 *  when there is nothing to format. */
export function formatZoned(value: number): string {
	const date = toDate(value);
	if (!date) return '—';
	return `${formatAbsolute(date)} ${utcOffsetLabel(-date.getTimezoneOffset())}`;
}
