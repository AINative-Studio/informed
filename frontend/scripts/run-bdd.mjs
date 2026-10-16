/**
 * Runs the frontend BDD suite (story 1.1).
 *
 * Builds the app, serves the build on a fixed port, waits for it to answer,
 * then runs Cucumber against it and shuts the server down. Keeping this in one
 * script means `npm run test:bdd` behaves the same locally and in CI.
 */
import { spawn } from 'node:child_process';
import process from 'node:process';

const HOST = '127.0.0.1';
const PORT = Number(process.env.INFORMED_WEB_PORT ?? 4173);
const BASE_URL = `http://${HOST}:${PORT}`;
const READY_TIMEOUT_MS = 60_000;

/** Runs a command to completion and resolves with its exit code. */
function run(command, args, extraEnv = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: 'inherit',
      shell: process.platform === 'win32',
      env: { ...process.env, ...extraEnv },
    });
    child.on('error', reject);
    child.on('exit', (code) => resolve(code ?? 1));
  });
}

async function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2_000) });
      if (response.ok) return;
    } catch {
      // Server not up yet — retry until the deadline.
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`Preview server did not become ready at ${url} within ${timeoutMs}ms`);
}

const buildCode = await run('npx', ['vite', 'build', '--logLevel', 'warn']);
if (buildCode !== 0) {
  console.error('Frontend build failed; skipping BDD run.');
  process.exit(buildCode);
}

const server = spawn(
  'npx',
  ['vite', 'preview', '--host', HOST, '--port', String(PORT), '--strictPort'],
  { stdio: 'inherit', shell: process.platform === 'win32' },
);

const stopServer = () => {
  if (!server.killed) server.kill('SIGTERM');
};
process.on('exit', stopServer);
process.on('SIGINT', () => {
  stopServer();
  process.exit(130);
});

let exitCode = 1;
try {
  await waitForServer(BASE_URL, READY_TIMEOUT_MS);
  exitCode = await run('npx', ['cucumber-js'], {
    INFORMED_WEB_URL: BASE_URL,
    NODE_OPTIONS: `${process.env.NODE_OPTIONS ?? ''} --import tsx/esm`.trim(),
  });
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  exitCode = 1;
} finally {
  stopServer();
}

process.exit(exitCode);
