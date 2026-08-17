import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, parse } from "node:path";

import type { Plugin } from "vite";

interface PackageJson {
	name: string;
	version: string;
}

interface PackageNotice {
	name: string;
	version: string;
	licenses: Array<{ filename: string; text: string }>;
}

const LICENSE_FILENAME = /^(?:licen[cs]e|copying)(?:[._-]|$)/i;

function findPackageNotice(moduleId: string): PackageNotice | undefined {
	const queryIndex = moduleId.indexOf("?");
	const filename = queryIndex === -1 ? moduleId : moduleId.slice(0, queryIndex);
	if (!filename.includes("node_modules")) return undefined;

	let directory = dirname(filename);
	const root = parse(directory).root;
	while (directory !== root) {
		const packageJsonPath = join(directory, "package.json");
		if (existsSync(packageJsonPath)) {
			const packageJson = parsePackageJson(readFileSync(packageJsonPath, "utf8"));
			if (packageJson) {
				const licenseFiles = readdirSync(directory)
					.filter((entry) => LICENSE_FILENAME.test(entry))
					.sort();
				if (licenseFiles.length === 0) {
					throw new Error(
						`Bundled package ${packageJson.name}@${packageJson.version} has no license file`,
					);
				}
				return {
					name: packageJson.name,
					version: packageJson.version,
					licenses: licenseFiles.map((licenseFile) => ({
						filename: licenseFile,
						text: readFileSync(join(directory, licenseFile), "utf8").trimEnd(),
					})),
				};
			}
		}
		directory = dirname(directory);
	}
	return undefined;
}

function parsePackageJson(text: string): PackageJson | undefined {
	const value: unknown = JSON.parse(text);
	if (!isPlainObject(value)) return undefined;
	const name: unknown = Object.getOwnPropertyDescriptor(value, "name")?.value;
	const version: unknown = Object.getOwnPropertyDescriptor(value, "version")?.value;
	if (!isString(name) || !isString(version)) return undefined;
	return { name, version };
}

function isPlainObject<Value>(value: Value): value is Value & object {
	return Object(value) === value && !Array.isArray(value);
}

function isString<Value>(value: Value): value is Value & string {
	return Object(value) !== value && Object.getPrototypeOf(Object(value)) === String.prototype;
}

function renderNotices(moduleIds: Iterable<string>): string {
	const packages = new Map<string, PackageNotice>();
	for (const moduleId of moduleIds) {
		const notice = findPackageNotice(moduleId);
		if (notice) packages.set(`${notice.name}@${notice.version}`, notice);
	}

	const entries = [...packages.values()].sort((left, right) =>
		`${left.name}@${left.version}`.localeCompare(`${right.name}@${right.version}`),
	);
	if (entries.length === 0) {
		throw new Error("The browser bundle contains no third-party packages");
	}

	const sections = entries.map((entry) => {
		const licenses = entry.licenses
			.map((license) => `--- ${license.filename} ---\n\n${license.text}`)
			.join("\n\n");
		return `${"=".repeat(79)}\n${entry.name} ${entry.version}\n${"=".repeat(79)}\n\n${licenses}`;
	});

	return [
		"Third-Party Notices",
		"",
		"This distribution includes compiled browser code from the packages listed here.",
		"The license files supplied by each package follow its name and version.",
		"",
		...sections,
		"",
	].join("\n");
}

export function thirdPartyNotices(outputFile: string): Plugin {
	return {
		name: "anywidget-mcp-third-party-notices",
		apply: "build",
		generateBundle(_options, bundle) {
			const moduleIds = new Set<string>();
			for (const output of Object.values(bundle)) {
				if (output.type !== "chunk") continue;
				for (const moduleId of Object.keys(output.modules)) moduleIds.add(moduleId);
			}

			const notices = renderNotices(moduleIds);
			const current = existsSync(outputFile) ? readFileSync(outputFile, "utf8") : undefined;
			if (current !== notices) writeFileSync(outputFile, notices);
		},
	};
}
