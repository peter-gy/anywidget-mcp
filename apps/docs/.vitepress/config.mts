import { fileURLToPath } from "node:url";

import { defineConfig } from "vitepress";

const repository = "https://github.com/peter-gy/anywidget-mcp";
const siteUrl = "https://peter-gy.github.io/anywidget-mcp/";
const description = "Run AnyWidgets as interactive MCP Apps.";
const socialImage = `${siteUrl}brand/anywidget-mcp-social-card-1200x630.png`;
const basePath = process.env.BASE_PATH?.replace(/\/$/, "");
const publicDir = fileURLToPath(new URL("../public", import.meta.url));
const publicPath = (path: string): string => `${basePath ?? ""}${path}`;

export default defineConfig({
	base: basePath ? `${basePath}/` : "/",
	cleanUrls: true,
	description,
	head: [
		["link", { href: publicPath("/favicon.svg"), rel: "icon", type: "image/svg+xml" }],
		[
			"link",
			{
				href: publicPath("/favicon-32x32.png"),
				rel: "icon",
				sizes: "32x32",
				type: "image/png",
			},
		],
		[
			"link",
			{ href: publicPath("/apple-touch-icon.png"), rel: "apple-touch-icon", sizes: "180x180" },
		],
		["meta", { content: "website", property: "og:type" }],
		["meta", { content: "anywidget-mcp", property: "og:title" }],
		["meta", { content: description, property: "og:description" }],
		["meta", { content: siteUrl, property: "og:url" }],
		["meta", { content: socialImage, property: "og:image" }],
		["meta", { content: "1200", property: "og:image:width" }],
		["meta", { content: "630", property: "og:image:height" }],
		["meta", { content: "anywidget-mcp", property: "og:image:alt" }],
		["meta", { content: "summary_large_image", name: "twitter:card" }],
		["meta", { content: socialImage, name: "twitter:image" }],
		["meta", { content: "anywidget-mcp", name: "twitter:image:alt" }],
		[
			"script",
			{
				defer: "",
				"data-website-id": "186c2175-f7d1-42f4-9896-18cd7107c0e5",
				src: "https://umami.peter.gy/script.js",
			},
		],
	],
	lastUpdated: true,
	srcDir: "../../docs",
	vite: { publicDir },
	themeConfig: {
		logo: {
			alt: "anywidget-mcp",
			dark: "/brand/anywidget-mcp-mark-inverse.svg",
			light: "/brand/anywidget-mcp-mark.svg",
		},
		siteTitle: "anywidget-mcp",
		editLink: {
			pattern: `${repository}/edit/main/docs/:path`,
			text: "Edit this page on GitHub",
		},
		nav: [
			{ text: "Getting started", link: "/getting-started" },
			{
				text: "Guides",
				items: [
					{ text: "Share state with the model", link: "/state" },
					{ text: "Pass input to widgets", link: "/factories" },
					{ text: "Deployment", link: "/deployment" },
				],
			},
			{ text: "How it works", link: "/how-it-works" },
			{ text: "API reference", link: "/api" },
		],
		search: { provider: "local" },
		sidebar: [
			{
				text: "anywidget-mcp",
				items: [
					{ text: "Overview", link: "/" },
					{ text: "Getting started", link: "/getting-started" },
					{ text: "Share state with the model", link: "/state" },
					{ text: "Pass input to widgets", link: "/factories" },
					{ text: "Deployment", link: "/deployment" },
					{ text: "How it works", link: "/how-it-works" },
					{ text: "API reference", link: "/api" },
				],
			},
		],
		socialLinks: [{ icon: "github", link: repository }],
	},
	title: "anywidget-mcp",
});
