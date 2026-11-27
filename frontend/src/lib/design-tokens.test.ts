import { readFileSync } from 'node:fs';
import { expect, test } from 'vitest';

const css = readFileSync('src/app.css', 'utf8');
const html = readFileSync('src/app.html', 'utf8');

test('defines the obsidian and light surface ladders', () => {
	expect(css).toContain('--canvas: #060606');
	expect(css).toContain('--panel: #0b0b0e');
	expect(css).toContain('--elevated: #111116');
	expect(css).toContain('--elevated-hover: #16161d');
	expect(css).toContain('rgba(255, 255, 255, 0.08)');
	expect(css).toContain('--canvas: #fafafb');
	expect(css).toContain('--accent: #1721ef');
	expect(css).toContain('--accent-data: #2143ce');
	expect(css).toContain('--warning: #dba72f');
	expect(css).toContain('font-variant-numeric: tabular-nums');
	expect(css).toContain('--font-mono');
});

test('has no glowing gradient treatment', () => {
	expect(css).not.toMatch(/linear-gradient|radial-gradient|box-shadow:\s*0\s+0\s+\d+px/i);
	expect(html).toContain('class="dark"');
	expect(html).toContain("localStorage.getItem('agent-voice-theme') === 'light'");
});
