import * as vscode from 'vscode';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import * as os from 'node:os';
import * as path from 'node:path';
import { ProjectOutline, ProjectTree, TreeInput } from './tree';

const run = promisify(execFile);

const MISSIONCACHE_ROOT = path.join(os.homedir(), '.missioncache');
const DASHBOARD_URL = process.env.MISSIONCACHE_DASHBOARD_URL ?? 'http://localhost:8787';
const PICK_KEY = 'missioncache.pickedProject';
const SCHEMA = 1;
const UPDATE_HINT = 'uvx --refresh missioncache-install@latest --update';
const INSTALL_HINT = 'uvx missioncache-install';

interface Project {
    id: number;
    name: string;
    repo_path: string | null;
    dir_match: boolean;
    status: string;
    last_worked_on: string | null;
    context_saved_at: string | null;
    fork_of: string | null;
    has_docs: boolean;
    completed_count: number | null;
    total_count: number | null;
    completion_pct: number | null;
    remaining_summary: string | null;
    tasks_file: string | null;
    context_file: string | null;
}

interface ExtensionState {
    schema: number;
    update_available: boolean;
    update_command: string | null;
    projects: Project[];
}

type CliResult<T> =
    | { kind: 'ok'; state: T }
    | { kind: 'not-installed' }
    | { kind: 'old-cli' }
    | { kind: 'error'; detail: string };

type DataState = CliResult<ExtensionState>;

function isOldCliOutput(text: string | undefined): boolean {
    return !!text && (text.includes('Unknown command') || text.includes('Usage:'));
}

/** Run a `missioncache-db` JSON verb, mapping every way it can fail to a state the UI renders. */
async function runCli<T extends { schema: number }>(args: string[]): Promise<CliResult<T>> {
    try {
        const { stdout } = await run('missioncache-db', args, { timeout: 10000 });
        try {
            const state = JSON.parse(stdout) as T;
            // A schema we don't know renders wrong silently - route it to the
            // same update path as a missing command.
            if (state.schema !== SCHEMA) { return { kind: 'old-cli' }; }
            return { kind: 'ok', state };
        } catch {
            // Binary exists, answered with prose: a CLI older than this verb.
            return isOldCliOutput(stdout)
                ? { kind: 'old-cli' }
                : { kind: 'error', detail: stdout.slice(-300) };
        }
    } catch (err: unknown) {
        const e = err as NodeJS.ErrnoException & { stdout?: string; stderr?: string };
        if (e.code === 'ENOENT') { return { kind: 'not-installed' }; }
        if (isOldCliOutput(e.stdout) || isOldCliOutput(e.stderr)) { return { kind: 'old-cli' }; }
        const detail = (e.stderr || e.stdout || e.message || 'unknown error').trim().slice(-300);
        return { kind: 'error', detail };
    }
}

function fetchState(): Promise<DataState> {
    const dir = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    return runCli<ExtensionState>(['extension-state', ...(dir ? ['--dir', dir] : [])]);
}

function pickProject(context: vscode.ExtensionContext, projects: Project[]): Project | undefined {
    if (projects.length === 0) { return undefined; }
    const remembered = context.workspaceState.get<string>(PICK_KEY);
    if (remembered) {
        const match = projects.find(p => p.name === remembered);
        if (match) { return match; }
    }
    // Auto-pick never lands on a paused project; an explicit override may.
    const auto = projects.filter(p => p.status === 'active');
    return auto.find(p => p.dir_match) ?? auto[0] ?? projects[0];
}

function relativeTime(ts: string | null): string {
    if (!ts) { return 'unknown'; }
    const then = new Date(ts.replace(' ', 'T')).getTime();
    if (Number.isNaN(then)) { return ts; }
    const mins = Math.round((Date.now() - then) / 60000);
    if (mins < 1) { return 'just now'; }
    if (mins < 60) { return `${mins}m ago`; }
    if (mins < 60 * 24) { return `${Math.round(mins / 60)}h ago`; }
    return `${Math.round(mins / (60 * 24))}d ago`;
}

async function openFile(filePath: string, line?: number): Promise<void> {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(filePath));
    // `line` is 1-based, as missioncache-db reports it.
    const selection = line ? new vscode.Range(line - 1, 0, line - 1, 0) : undefined;
    await vscode.window.showTextDocument(doc, { selection });
}

export function activate(context: vscode.ExtensionContext): void {
    const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    item.command = 'missioncache.showMenu';
    item.text = '$(checklist) MissionCache';
    item.show();
    context.subscriptions.push(item);

    const tree = new ProjectTree();
    context.subscriptions.push(tree, vscode.window.registerTreeDataProvider('missioncache.project', tree));

    let latest: DataState | undefined;
    let fetchSeq = 0;

    async function refresh(): Promise<void> {
        const seq = ++fetchSeq;
        const result = await fetchState();
        if (seq !== fetchSeq) { return; } // a newer refresh already landed
        latest = result;
        render();
        const input = await treeInput(result);
        if (seq !== fetchSeq) { return; }
        tree.update(input);
    }

    function cliMessage(result: Exclude<CliResult<unknown>, { kind: 'ok' }>): TreeInput {
        switch (result.kind) {
            case 'not-installed':
                return { kind: 'message', text: 'MissionCache CLI not found', tooltip: `Install with: ${INSTALL_HINT}` };
            case 'old-cli':
                return { kind: 'message', text: 'Update MissionCache to see this view', tooltip: `Run: ${UPDATE_HINT}` };
            case 'error':
                return { kind: 'message', text: 'missioncache-db failed', tooltip: result.detail };
        }
    }

    async function treeInput(result: DataState): Promise<TreeInput> {
        if (result.kind !== 'ok') { return cliMessage(result); }
        const project = pickProject(context, result.state.projects);
        if (!project) {
            return { kind: 'message', text: 'No active MissionCache project' };
        }
        // old-cli here means a CLI that has extension-state but predates extension-project.
        const outline = await runCli<ProjectOutline>(['extension-project', String(project.id)]);
        if (outline.kind !== 'ok') { return cliMessage(outline); }
        if (!outline.state.found) {
            return { kind: 'message', text: `Project ${project.name} not found` };
        }
        const others = result.state.projects
            .filter(p => p.name !== project.name)
            .map(p => ({
                name: p.name,
                detail: `${p.status !== 'active' ? `${p.status} - ` : ''}${relativeTime(p.last_worked_on)}`,
            }));
        return { kind: 'outline', outline: outline.state, others };
    }

    async function switchProject(name: string | undefined): Promise<void> {
        await context.workspaceState.update(PICK_KEY, name);
        render(); // the status bar switches now, the sidebar after the fetch
        await refresh();
    }

    function render(): void {
        if (!latest) { return; }
        switch (latest.kind) {
            case 'not-installed':
                item.text = '$(circle-slash) MissionCache';
                item.tooltip = `MissionCache CLI not found. Install with: ${INSTALL_HINT}`;
                return;
            case 'old-cli':
                item.text = '$(arrow-up) MissionCache: update needed';
                item.tooltip = `This extension needs a newer MissionCache. Run: ${UPDATE_HINT}`;
                return;
            case 'error':
                item.text = '$(warning) MissionCache';
                item.tooltip = `missioncache-db failed:\n${latest.detail}`;
                return;
        }
        const project = pickProject(context, latest.state.projects);
        if (!project) {
            item.text = '$(checklist) MissionCache: no active project';
            item.tooltip = 'No active MissionCache projects. Create one with /missioncache-new in your AI tool.';
            return;
        }
        const progress = project.total_count
            ? ` ${project.completed_count}/${project.total_count}`
            : '';
        const upBadge = latest.state.update_available ? ' $(arrow-up)' : '';
        item.text = `$(checklist) ${project.name}${progress}${upBadge}`;
        const lines = [
            `**${project.name}**${project.fork_of ? ` (fork of ${project.fork_of})` : ''}${project.status !== 'active' ? ` [${project.status}]` : ''}`,
            project.completion_pct !== null ? `Progress: ${project.completed_count}/${project.total_count} (${project.completion_pct}%)` : undefined,
            `Last worked: ${relativeTime(project.last_worked_on)}`,
            `Context saved: ${relativeTime(project.context_saved_at)}`,
            project.remaining_summary ? `Remaining: ${project.remaining_summary}` : undefined,
            latest.state.update_available ? 'MissionCache update available' : undefined,
        ].filter(Boolean);
        item.tooltip = new vscode.MarkdownString(lines.join('\n\n'));
    }

    async function showMenu(): Promise<void> {
        if (!latest || latest.kind !== 'ok') {
            await refresh();
        }
        if (!latest || latest.kind !== 'ok') {
            const message =
                latest?.kind === 'not-installed'
                    ? `MissionCache CLI not found on PATH. Install with: ${INSTALL_HINT}`
                    : latest?.kind === 'error'
                        ? `missioncache-db failed: ${latest.detail}`
                        : `This extension needs a newer MissionCache. Run: ${UPDATE_HINT}`;
            void vscode.window.showWarningMessage(message);
            return;
        }
        const state = latest.state;
        const current = pickProject(context, state.projects);
        const overridden = !!context.workspaceState.get<string>(PICK_KEY);
        const items: (vscode.QuickPickItem & { action?: () => void | Promise<void> })[] = [];
        if (current) {
            if (current.tasks_file) {
                items.push({
                    label: '$(go-to-file) Open tasks file',
                    description: current.name,
                    action: () => openFile(current.tasks_file!),
                });
            }
            if (current.context_file) {
                items.push({
                    label: '$(book) Open context file',
                    description: current.name,
                    action: () => openFile(current.context_file!),
                });
            }
            items.push({
                label: '$(browser) Open in dashboard',
                description: current.name,
                action: () => { void vscode.env.openExternal(vscode.Uri.parse(`${DASHBOARD_URL}/#projects?task=${current.name}`)); },
            });
        }
        if (overridden) {
            items.push({
                label: '$(discard) Back to automatic project detection',
                action: () => switchProject(undefined),
            });
        }
        for (const project of state.projects) {
            if (project.name === current?.name) { continue; }
            items.push({
                label: `$(repo) ${project.name}`,
                description: `${project.dir_match ? 'this workspace - ' : ''}${project.status !== 'active' ? `${project.status} - ` : ''}${relativeTime(project.last_worked_on)}`,
                action: () => switchProject(project.name),
            });
        }
        if (state.update_available) {
            items.push({
                label: '$(arrow-up) Update MissionCache',
                description: state.update_command ?? '',
                action: () => { void vscode.window.showInformationMessage(`Run in a terminal: ${state.update_command ?? UPDATE_HINT}`); },
            });
        }
        items.push({ label: '$(refresh) Refresh', action: refresh });
        const picked = await vscode.window.showQuickPick(items, {
            placeHolder: current ? `MissionCache: ${current.name}` : 'MissionCache',
        });
        try {
            await picked?.action?.();
        } catch (err: unknown) {
            void vscode.window.showWarningMessage(`MissionCache: ${(err as Error).message}`);
        }
    }

    context.subscriptions.push(
        vscode.commands.registerCommand('missioncache.refresh', refresh),
        vscode.commands.registerCommand('missioncache.openDashboard', () => {
            void vscode.env.openExternal(vscode.Uri.parse(DASHBOARD_URL));
        }),
        vscode.commands.registerCommand('missioncache.showMenu', showMenu),
        vscode.commands.registerCommand('missioncache.switchProject', switchProject),
        vscode.commands.registerCommand('missioncache.openAtLine', async (file: string, line: number) => {
            try {
                await openFile(file, line);
            } catch (err: unknown) {
                void vscode.window.showWarningMessage(`MissionCache: ${(err as Error).message}`);
            }
        }),
    );

    // Event-driven refresh: MissionCache markdown writes (a *.md glob never
    // matches the SQLite/DuckDB churn) and window focus. No polling.
    const watcher = vscode.workspace.createFileSystemWatcher(
        new vscode.RelativePattern(vscode.Uri.file(MISSIONCACHE_ROOT), '**/*.md')
    );
    let debounce: ReturnType<typeof setTimeout> | undefined;
    const debouncedRefresh = () => {
        if (debounce) { clearTimeout(debounce); }
        debounce = setTimeout(() => { void refresh(); }, 500);
    };
    watcher.onDidChange(debouncedRefresh);
    watcher.onDidCreate(debouncedRefresh);
    watcher.onDidDelete(debouncedRefresh);
    context.subscriptions.push(watcher);
    context.subscriptions.push({ dispose: () => { if (debounce) { clearTimeout(debounce); } } });
    context.subscriptions.push(
        vscode.window.onDidChangeWindowState(e => { if (e.focused) { debouncedRefresh(); } })
    );

    void refresh();
}

export function deactivate(): void { /* disposal happens via context.subscriptions */ }
