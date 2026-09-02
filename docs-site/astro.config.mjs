// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import sitemap from '@astrojs/sitemap';

// Where the site is served. Astro needs it for canonical URLs and the
// sitemap, and a card image has to be absolute, so both read it from here.
const SITE = 'https://docs.palmimo.dev';

const GOOGLE_FONTS =
	'https://fonts.googleapis.com/css2' +
	'?family=IBM+Plex+Mono:wght@400;500' +
	'&family=Shippori+Mincho:wght@600' +
	'&family=Zen+Kaku+Gothic+New:wght@400;500;700' +
	'&display=swap';

// https://astro.build/config
export default defineConfig({
	site: SITE,
	integrations: [
		starlight({
			title: 'Palmimo DevKit',
			description: 'Give AI agents a body. Developer docs for Palmimo DevKit, a six-legged tabletop AI robot you drive with your own code.',
			// The lockup spells the product's name, so it stands in for the title
			// rather than sitting beside a second copy of it. It lives in public/
			// because the splash hero reaches the same file by URL, and one copy
			// on disk cannot drift from itself.
			logo: {
				light: './public/palmimo-devkit-logo.png',
				dark: './public/palmimo-devkit-logo-white.png',
				replacesTitle: true,
			},
			customCss: ['./src/styles/palmimo.css'],
			favicon: '/favicon.ico',
			lastUpdated: true,
			head: [
				// Starlight emits one icon link, the .ico above. The PNG pair is
				// what a modern browser and an iOS home screen actually pick up,
				// and both are the same drawing the landing page ships.
				{
					tag: 'link',
					attrs: { rel: 'icon', type: 'image/png', sizes: '32x32', href: '/favicon-32.png' },
				},
				{ tag: 'link', attrs: { rel: 'apple-touch-icon', href: '/apple-touch-icon.png' } },
				{ tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.googleapis.com' } },
				{
					tag: 'link',
					attrs: { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: true },
				},
				{ tag: 'link', attrs: { rel: 'stylesheet', href: GOOGLE_FONTS } },
				// Starlight fills in every other card tag itself, but not the
				// image: a shared link previews as the robot on a desk, which is
				// the one thing about this kit a preview can say in a picture.
				{
					tag: 'meta',
					attrs: { property: 'og:image', content: `${SITE}/images/palmimo-dev-scene.jpg` },
				},
			],
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/Jizai-inc/palmimo-devkit' },
			],
			// The groups are doc/'s own taxonomy, and the pages under them are
			// whatever sync-doc.mjs rendered -- so a page added to doc/ reaches
			// the sidebar without being named twice.
			sidebar: [
				{ label: 'Guides', items: [{ autogenerate: { directory: 'guides' } }] },
				{ label: 'Reference', items: [{ autogenerate: { directory: 'reference' } }] },
				{ label: 'Explanation', items: [{ autogenerate: { directory: 'explanation' } }] },
			],
		}),
		sitemap(),
	],
});
