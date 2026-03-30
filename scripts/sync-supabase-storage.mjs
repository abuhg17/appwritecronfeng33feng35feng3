import { mkdir, readdir, readFile, rm, stat, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

const requiredEnv = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_BUCKET"];

for (const name of requiredEnv) {
  if (!process.env[name]) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
}

const supabaseUrl = process.env.SUPABASE_URL.replace(/\/+$/, "");
const supabaseAnonKey = process.env.SUPABASE_ANON_KEY;
const bucket = process.env.SUPABASE_BUCKET;
const outputRoot = path.resolve("synced", bucket);
const manifestPath = path.resolve("synced", `${bucket}-manifest.json`);

const headers = {
  apikey: supabaseAnonKey,
  Authorization: `Bearer ${supabaseAnonKey}`,
  "Content-Type": "application/json",
};

async function listObjects(prefix = "") {
  const items = [];
  let offset = 0;
  const limit = 100;

  while (true) {
    const response = await fetch(`${supabaseUrl}/storage/v1/object/list/${encodeURIComponent(bucket)}`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        prefix,
        limit,
        offset,
        sortBy: { column: "name", order: "asc" },
      }),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Failed to list "${prefix || "/"}": ${response.status} ${errorText}`);
    }

    const page = await response.json();
    if (!Array.isArray(page) || page.length === 0) {
      break;
    }

    items.push(...page);
    if (page.length < limit) {
      break;
    }

    offset += page.length;
  }

  return items;
}

async function flattenFiles(prefix = "") {
  const entries = await listObjects(prefix);
  const files = [];

  for (const entry of entries) {
    const entryPath = prefix ? `${prefix}/${entry.name}` : entry.name;
    const isFolder = entry.id === null;

    if (isFolder) {
      files.push(...await flattenFiles(entryPath));
      continue;
    }

    files.push({
      path: entryPath,
      name: entry.name,
      metadata: entry.metadata ?? {},
      updated_at: entry.updated_at ?? null,
      created_at: entry.created_at ?? null,
      last_accessed_at: entry.last_accessed_at ?? null,
    });
  }

  return files;
}

async function downloadFile(filePath) {
  const encodedSegments = filePath
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");

  const response = await fetch(
    `${supabaseUrl}/storage/v1/object/authenticated/${encodeURIComponent(bucket)}/${encodedSegments}`,
    {
      headers: {
        apikey: supabaseAnonKey,
        Authorization: `Bearer ${supabaseAnonKey}`,
      },
    },
  );

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`Failed to download "${filePath}": ${response.status} ${errorText}`);
  }

  const destination = path.join(outputRoot, filePath);
  await mkdir(path.dirname(destination), { recursive: true });
  const buffer = Buffer.from(await response.arrayBuffer());
  await writeFile(destination, buffer);
}

async function walkFiles(dir) {
  const found = new Set();

  try {
    const entries = await readdir(dir, { withFileTypes: true });
    for (const entry of entries) {
      const absolutePath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        const nested = await walkFiles(absolutePath);
        for (const file of nested) {
          found.add(file);
        }
      } else {
        found.add(absolutePath);
      }
    }
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return found;
    }
    throw error;
  }

  return found;
}

async function pruneDeletedFiles(expectedFiles) {
  const existingFiles = await walkFiles(outputRoot);

  for (const existingFile of existingFiles) {
    const relativePath = path.relative(outputRoot, existingFile).replace(/\\/g, "/");
    if (!expectedFiles.has(relativePath)) {
      await unlink(existingFile);
    }
  }
}

async function ensureCleanRoot() {
  const syncedRoot = path.resolve("synced");
  await mkdir(syncedRoot, { recursive: true });

  try {
    const info = await stat(outputRoot);
    if (!info.isDirectory()) {
      await rm(outputRoot, { recursive: true, force: true });
      await mkdir(outputRoot, { recursive: true });
    }
  } catch (error) {
    if (error && error.code === "ENOENT") {
      await mkdir(outputRoot, { recursive: true });
      return;
    }
    throw error;
  }
}

async function main() {
  await ensureCleanRoot();

  const files = await flattenFiles();
  const expectedFiles = new Set(files.map((file) => file.path));

  await pruneDeletedFiles(expectedFiles);

  for (const file of files) {
    await downloadFile(file.path);
  }

  const manifest = {
    bucket,
    synced_at: new Date().toISOString(),
    file_count: files.length,
    files,
  };

  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(`Synced ${files.length} file(s) from bucket "${bucket}".`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
