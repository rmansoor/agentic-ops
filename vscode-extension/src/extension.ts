import { execFile } from "node:child_process";
import { promisify } from "node:util";
import * as vscode from "vscode";

const execFileAsync = promisify(execFile);

type ReviewSeverity = "nit" | "minor" | "major" | "critical";
type GateSeverity = "info" | "warning" | "error";

interface ReviewComment {
  path: string;
  line: number;
  severity: ReviewSeverity;
  category: string;
  body: string;
  suggestion?: string | null;
}

interface Finding {
  tool: string;
  rule: string;
  path: string;
  line: number;
  severity: GateSeverity;
  message: string;
}

interface ReviewResponse {
  summary: string;
  comments: ReviewComment[];
  findings: Finding[];
  blocked: boolean;
  tier: string;
}

interface AskResponse {
  answer: string;
  steps: string[];
}

const REVIEW_TO_VSCODE: Record<ReviewSeverity, vscode.DiagnosticSeverity> = {
  critical: vscode.DiagnosticSeverity.Error,
  major: vscode.DiagnosticSeverity.Warning,
  minor: vscode.DiagnosticSeverity.Information,
  nit: vscode.DiagnosticSeverity.Hint,
};

const GATE_TO_VSCODE: Record<GateSeverity, vscode.DiagnosticSeverity> = {
  error: vscode.DiagnosticSeverity.Error,
  warning: vscode.DiagnosticSeverity.Warning,
  info: vscode.DiagnosticSeverity.Information,
};

let diagnostics: vscode.DiagnosticCollection;
let output: vscode.OutputChannel;

function config() {
  const c = vscode.workspace.getConfiguration("agenticOps");
  return {
    serverUrl: c.get<string>("serverUrl", "http://localhost:8080").replace(/\/$/, ""),
    apiToken: c.get<string>("apiToken", ""),
    reviewOnSave: c.get<boolean>("reviewOnSave", false),
  };
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const { serverUrl, apiToken } = config();
  const res = await fetch(`${serverUrl}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  }
  return (await res.json()) as T;
}

function lineRange(doc: vscode.TextDocument | undefined, line: number): vscode.Range {
  const idx = Math.max(line - 1, 0);
  if (doc && idx < doc.lineCount) {
    return doc.lineAt(idx).range;
  }
  return new vscode.Range(idx, 0, idx, 200);
}

function toDiagnostics(result: ReviewResponse, root: vscode.Uri): Map<string, vscode.Diagnostic[]> {
  const byFile = new Map<string, vscode.Diagnostic[]>();
  const push = (path: string, d: vscode.Diagnostic) => {
    const key = vscode.Uri.joinPath(root, path).toString();
    byFile.set(key, [...(byFile.get(key) ?? []), d]);
  };
  const openDoc = (path: string) =>
    vscode.workspace.textDocuments.find((d) => d.uri.toString() === vscode.Uri.joinPath(root, path).toString());

  for (const c of result.comments) {
    const text = c.suggestion ? `${c.body}\n\nSuggestion:\n${c.suggestion}` : c.body;
    const d = new vscode.Diagnostic(lineRange(openDoc(c.path), c.line), text, REVIEW_TO_VSCODE[c.severity]);
    d.source = `agentic-ops (${c.category})`;
    push(c.path, d);
  }
  for (const f of result.findings) {
    if (!f.path) continue;
    const d = new vscode.Diagnostic(lineRange(openDoc(f.path), f.line), f.message, GATE_TO_VSCODE[f.severity]);
    d.source = f.tool;
    d.code = f.rule;
    push(f.path, d);
  }
  return byFile;
}

function applyResult(result: ReviewResponse, root: vscode.Uri, replaceAll: boolean): void {
  if (replaceAll) diagnostics.clear();
  for (const [uri, diags] of toDiagnostics(result, root)) {
    diagnostics.set(vscode.Uri.parse(uri), diags);
  }
  output.appendLine(`\n[${new Date().toISOString()}] tier=${result.tier} blocked=${result.blocked}`);
  output.appendLine(result.summary);
  const count = result.comments.length + result.findings.length;
  const msg = `Agentic Ops: ${count} finding(s), tier ${result.tier}${result.blocked ? " (gate would BLOCK)" : ""}`;
  if (result.blocked) {
    void vscode.window.showWarningMessage(msg);
  } else {
    void vscode.window.showInformationMessage(msg);
  }
}

function workspaceRoot(): vscode.Uri | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri;
}

async function reviewFile(doc?: vscode.TextDocument): Promise<void> {
  const document = doc ?? vscode.window.activeTextEditor?.document;
  const root = workspaceRoot();
  if (!document || !root) {
    void vscode.window.showErrorMessage("Open a file inside a workspace folder first.");
    return;
  }
  const path = vscode.workspace.asRelativePath(document.uri, false);
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Agentic Ops: reviewing ${path}` },
    async () => {
      try {
        const result = await post<ReviewResponse>("/api/review", { path, content: document.getText() });
        diagnostics.delete(document.uri);
        applyResult(result, root, false);
      } catch (err) {
        void vscode.window.showErrorMessage(`Agentic Ops review failed: ${(err as Error).message}`);
      }
    },
  );
}

async function reviewChanges(): Promise<void> {
  const root = workspaceRoot();
  if (!root) return;
  try {
    const { stdout } = await execFileAsync("git", ["diff", "--unified=3", "HEAD"], {
      cwd: root.fsPath,
      maxBuffer: 20 * 1024 * 1024,
    });
    if (!stdout.trim()) {
      void vscode.window.showInformationMessage("No uncommitted changes to review.");
      return;
    }
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Agentic Ops: reviewing changes" },
      async () => applyResult(await post<ReviewResponse>("/api/review", { diff: stdout }), root, true),
    );
  } catch (err) {
    void vscode.window.showErrorMessage(`Agentic Ops review failed: ${(err as Error).message}`);
  }
}

async function ask(): Promise<void> {
  const question = await vscode.window.showInputBox({
    prompt: "Ask the codebase",
    placeHolder: "Where do we verify GitHub webhook signatures?",
  });
  if (!question) return;
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Agentic Ops: thinking…" },
    async () => {
      try {
        const res = await post<AskResponse>("/api/ask", { question });
        const doc = await vscode.workspace.openTextDocument({
          language: "markdown",
          content: `# ${question}\n\n${res.answer}\n\n---\n**Agent steps**\n${res.steps.map((s) => `- \`${s}\``).join("\n")}\n`,
        });
        await vscode.window.showTextDocument(doc, { preview: true, viewColumn: vscode.ViewColumn.Beside });
      } catch (err) {
        void vscode.window.showErrorMessage(`Agentic Ops ask failed: ${(err as Error).message}`);
      }
    },
  );
}

export function activate(context: vscode.ExtensionContext): void {
  diagnostics = vscode.languages.createDiagnosticCollection("agentic-ops");
  output = vscode.window.createOutputChannel("Agentic Ops");
  context.subscriptions.push(
    diagnostics,
    output,
    vscode.commands.registerCommand("agenticOps.reviewFile", () => reviewFile()),
    vscode.commands.registerCommand("agenticOps.reviewChanges", reviewChanges),
    vscode.commands.registerCommand("agenticOps.ask", ask),
    vscode.commands.registerCommand("agenticOps.clear", () => diagnostics.clear()),
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (config().reviewOnSave) void reviewFile(doc);
    }),
  );
}

export function deactivate(): void {
  diagnostics?.dispose();
}
