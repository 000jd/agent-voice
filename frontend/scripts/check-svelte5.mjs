import { readdirSync, readFileSync } from 'node:fs';
import { extname, join, relative, resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const src = join(root, 'src');
const allowedExtensions = new Set(['.svelte', '.ts', '.js']);
const ignored = new Set([join(src, 'lib', 'api', 'schema.d.ts')]);

const rules = [
	{ name: 'export let', pattern: /\bexport\s+let\b/ },
	{ name: '$$props/$$restProps', pattern: /\$\$(?:props|restProps)\b/ },
	{ name: '$: reactive statement', pattern: /^\s*\$:/m },
	{ name: 'on: event directive', pattern: /(?:^|\s)on:[a-z]/m },
	{ name: '<slot>', pattern: /<slot\b/ },
	{ name: 'slot attribute', pattern: /(?:^|\s)slot=/ },
	{ name: 'let: directive', pattern: /\blet:/ },
	{ name: 'createEventDispatcher', pattern: /\bcreateEventDispatcher\b/ },
	{ name: 'new Component({ target })', pattern: /\bnew\s+[A-Z]\w*\s*\(\s*\{\s*target\b/ },
	{ name: '<svelte:component>', pattern: /<svelte:component\b/ },
	{ name: '<svelte:self>', pattern: /<svelte:self\b/ },
	{ name: 'beforeUpdate/afterUpdate', pattern: /\b(?:beforeUpdate|afterUpdate)\b/ },
	{ name: '$app/stores', pattern: /\$app\/stores/ },
	{ name: '$lib import', pattern: /from\s+['"]\$lib(?:\/|['"])/ },
	{ name: '{@const}', pattern: /\{@const\b/ }
];

function walk(directory) {
	for (const entry of readdirSync(directory, { withFileTypes: true })) {
		const path = join(directory, entry.name);
		if (entry.isDirectory()) {
			walk(path);
		} else if (allowedExtensions.has(extname(entry.name)) && !ignored.has(path)) {
			check(path);
		}
	}
}

let failures = 0;
let checked = 0;

function check(path) {
	checked += 1;
	const lines = readFileSync(path, 'utf8').split('\n');
	for (const [index, line] of lines.entries()) {
		for (const rule of rules) {
			if (rule.pattern.test(line)) {
				failures += 1;
				console.error(`${relative(root, path)}:${index + 1}: ${rule.name}: ${line.trim()}`);
			}
		}
	}
}

walk(src);

if (failures > 0) {
	console.error(`\nSvelte 5 blocklist failed with ${failures} finding(s).`);
	process.exit(1);
}

console.log(`Svelte 5 blocklist clean (${checked} files).`);
