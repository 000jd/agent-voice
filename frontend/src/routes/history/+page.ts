import { redirect } from '@sveltejs/kit';
import { resolve } from '$app/paths';

/** Old `/history` links land on the persistent Library. */
export function load(): never {
	redirect(307, resolve('library'));
}
