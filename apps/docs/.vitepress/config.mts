import { defineConfig } from "vitepress";

const repository = "https://github.com/peter-gy/anywidget-mcp";
const basePath = process.env.BASE_PATH?.replace(/\/$/, "");

export default defineConfig({
	base: basePath ? `${basePath}/` : "/",
	cleanUrls: true,
	description: "Expose AnyWidget classes and factories as interactive MCP Apps.",
	head: [
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
	themeConfig: {
		editLink: {
			pattern: `${repository}/edit/main/docs/:path`,
			text: "Edit this page on GitHub",
		},
		nav: [
			{ text: "Getting started", link: "/getting-started" },
			{ text: "How it works", link: "/how-it-works" },
			{
				text: "Guides",
				items: [
					{ text: "Factories and composition", link: "/factories" },
					{ text: "Model-visible state", link: "/state" },
					{ text: "Deployment", link: "/deployment" },
				],
			},
			{ text: "API reference", link: "/api" },
		],
		search: { provider: "local" },
		sidebar: [
			{
				text: "anywidget-mcp",
				items: [
					{ text: "Overview", link: "/" },
					{ text: "Getting started", link: "/getting-started" },
					{ text: "How it works", link: "/how-it-works" },
					{ text: "Factories and composition", link: "/factories" },
					{ text: "Model-visible state", link: "/state" },
					{ text: "Deployment", link: "/deployment" },
					{ text: "API reference", link: "/api" },
				],
			},
		],
		socialLinks: [{ icon: "github", link: repository }],
	},
	title: "anywidget-mcp",
});
