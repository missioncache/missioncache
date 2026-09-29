import * as vscode from 'vscode';

export interface OutlineTask {
    line: number;
    checked: boolean;
    number: string | null;
    text: string;
    section: string | null;
}

export interface OutlineItem {
    line: number;
    text: string;
}

export interface OutlineWaiting {
    line: number;
    what: string;
    who: string;
    since: string;
    gates: string;
}

export interface ProjectOutline {
    schema: number;
    id: number;
    name: string | null;
    found: boolean;
    tasks_file: string | null;
    context_file: string | null;
    tasks: OutlineTask[];
    next_steps: OutlineItem[];
    waiting_on: OutlineWaiting[];
}

export interface OtherProject {
    name: string;
    detail: string;
}

/** What the sidebar shows: a project outline, or one line saying why not. */
export type TreeInput =
    | { kind: 'outline'; outline: ProjectOutline; others: OtherProject[] }
    | { kind: 'message'; text: string; tooltip?: string };

type Node = vscode.TreeItem & { children?: Node[] };

const LABEL_MAX = 80;

/** First sentence of a long line, capped, so 200-word items stay one row. */
function shortLabel(text: string, prefix = ''): string {
    const label = prefix + (text.match(/^.*?[.!?](?=\s|$)/s)?.[0] ?? text);
    return label.length > LABEL_MAX ? `${label.slice(0, LABEL_MAX - 1)}…` : label;
}

function taskLeaf(task: OutlineTask, file: string | null): Node {
    // The number comes pre-split from missioncache-db, so "86." is never read as the sentence.
    const prefix = task.number ? `${task.number}. ` : '';
    return leaf(shortLabel(task.text, prefix), task.checked ? 'pass-filled' : 'circle-large-outline',
        file, task.line, prefix + task.text);
}

function leaf(label: string, icon: string, file: string | null, line: number, tooltip: string): Node {
    const node: Node = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
    node.iconPath = new vscode.ThemeIcon(icon);
    node.tooltip = tooltip;
    if (file) {
        node.command = { command: 'missioncache.openAtLine', title: 'Open', arguments: [file, line] };
    }
    return node;
}

function group(label: string, children: Node[], expanded: boolean, icon?: string, emptyText?: string): Node {
    const node: Node = new vscode.TreeItem(
        label,
        expanded ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.Collapsed,
    );
    if (icon) { node.iconPath = new vscode.ThemeIcon(icon); }
    node.children = children.length || !emptyText
        ? children
        : [Object.assign(new vscode.TreeItem(emptyText), { iconPath: new vscode.ThemeIcon('dash') })];
    return node;
}

function taskNodes(outline: ProjectOutline): Node[] {
    const file = outline.tasks_file;
    const open = outline.tasks.filter(t => !t.checked);
    const done = outline.tasks.filter(t => t.checked);
    const sections = new Map<string, Node[]>();
    for (const task of open) {
        const key = task.section ?? 'Tasks';
        const nodes = sections.get(key) ?? [];
        nodes.push(taskLeaf(task, file));
        sections.set(key, nodes);
    }
    const children = [...sections].map(([name, nodes]) => group(`${name} (${nodes.length})`, nodes, true));
    if (done.length) {
        children.push(group(
            `Completed (${done.length})`,
            done.map(t => taskLeaf(t, file)),
            false,
        ));
    }
    return children;
}

function buildRoots(input: TreeInput): Node[] {
    if (input.kind === 'message') {
        const node: Node = new vscode.TreeItem(input.text);
        node.iconPath = new vscode.ThemeIcon('info');
        node.tooltip = input.tooltip ?? input.text;
        return [node];
    }
    const { outline, others } = input;
    const openCount = outline.tasks.filter(t => !t.checked).length;
    const context = outline.context_file;
    return [
        group(`Tasks (${openCount} open)`, taskNodes(outline), true, 'checklist', 'No tasks file'),
        group('Next Steps', outline.next_steps.map(s => leaf(shortLabel(s.text), 'arrow-right', context, s.line, s.text)),
            true, 'milestone', 'No next steps recorded'),
        group(`Waiting on (${outline.waiting_on.length})`, outline.waiting_on.map(w => leaf(
            shortLabel(`${w.what} - ${w.who}`), 'watch', context, w.line,
            `${w.what}\nWho: ${w.who}\nSince: ${w.since}\nGates: ${w.gates}`,
        )), outline.waiting_on.length > 0, 'clock', 'Nothing waiting'),
        group('Other projects', others.map(p => {
            const node: Node = new vscode.TreeItem(p.name);
            node.iconPath = new vscode.ThemeIcon('repo');
            node.description = p.detail;
            node.tooltip = `Switch MissionCache to ${p.name}`;
            node.command = { command: 'missioncache.switchProject', title: 'Switch', arguments: [p.name] };
            return node;
        }), false, 'folder-library', 'No other active projects'),
    ];
}

/** Read-only sidebar. No checkboxes: toggling one would mean writing the tasks file outside the MCP lock. */
export class ProjectTree implements vscode.TreeDataProvider<Node> {
    private readonly changed = new vscode.EventEmitter<void>();
    readonly onDidChangeTreeData = this.changed.event;
    private roots: Node[] = buildRoots({ kind: 'message', text: 'Loading…' });

    update(input: TreeInput): void {
        this.roots = buildRoots(input);
        this.changed.fire();
    }

    getTreeItem(node: Node): vscode.TreeItem {
        return node;
    }

    getChildren(node?: Node): Node[] {
        return node ? node.children ?? [] : this.roots;
    }

    dispose(): void {
        this.changed.dispose();
    }
}
