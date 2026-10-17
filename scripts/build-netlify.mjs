import { mkdir, copyFile } from 'node:fs/promises';
// Publish an explicit allowlist, never the repository root or server secrets.
await mkdir('dist', { recursive: true });
for (const file of ['index.html', 'app.js', 'styles.css', 'favicon.svg']) {
  await copyFile(file, `dist/${file}`);
}
