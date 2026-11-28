/** One rail, in the order the Studio is meant to be used. */
import { resolve } from '$app/paths';
import type { Path } from '$app/types';
import type { Component } from 'svelte';
import AudioLines from '@lucide/svelte/icons/audio-lines';
import AudioWaveform from '@lucide/svelte/icons/audio-waveform';
import Boxes from '@lucide/svelte/icons/boxes';
import Library from '@lucide/svelte/icons/library';
import Mic from '@lucide/svelte/icons/mic';
import Settings from '@lucide/svelte/icons/settings';

export type NavItem = {
	label: string;
	href: Path;
	icon: Component;
};

export const NAV_ITEMS: readonly NavItem[] = [
	{ label: 'Speak', href: 'speak', icon: AudioLines },
	{ label: 'Voices', href: 'voices', icon: Mic },
	{ label: 'Transcribe', href: 'transcribe', icon: AudioWaveform },
	{ label: 'Library', href: 'library', icon: Library },
	{ label: 'Models', href: 'models', icon: Boxes },
	{ label: 'Settings', href: 'settings', icon: Settings }
];

export function navLabelFor(pathname: string): string {
	const speakPath = resolve('speak');
	const basePath = speakPath.endsWith('/speak')
		? speakPath.slice(0, -'/speak'.length).replace(/\/+$/, '')
		: '';
	let normalized = pathname.replace(/\/+$/, '') || '/';
	if (basePath && (normalized === basePath || normalized.startsWith(`${basePath}/`))) {
		normalized = normalized.slice(basePath.length) || '/';
	}
	const match = NAV_ITEMS.find((item) => `/${item.href}` === normalized);
	return match?.label ?? 'Speak';
}
