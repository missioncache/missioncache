---
name: optimize-prompt
description: Transform prompts into structured, XML-tagged prompts with subagent and skill suggestions
effort: high
---

# Prompt Optimizer

Transform the provided prompt into a well-structured, optimized prompt using XML tags, with recommendations for subagents and skill-triggering keywords.

## Instructions

Analyze the user's prompt (provided after this command) and:

1. **Identify the intent** - What is the user trying to accomplish?
2. **Apply XML structure** - Separate context, instructions, examples, and constraints
3. **Recommend subagents** - Suggest specialized agents when beneficial
4. **Add skill-trigger keywords** - Ensure relevant skills will auto-activate
5. **Explain improvements** - Help the user understand the changes
6. **Copy to clipboard** - Automatically copy the optimized prompt to clipboard using pbcopy

## Output Format

```markdown
## Optimized Prompt

[The transformed prompt with XML tags, ready to copy/use]

## Changes Made
- [List of structural improvements]

## Recommended Subagent(s)
- `@agent-name` - [Why it's relevant]
  (Use: "Ask @agent-name to [task]" or include @agent-name in your prompt)

## Skills That Will Trigger
- `/skill-name` - triggered by: [keywords/patterns]
  (Invoke directly with `/skill-name` or include trigger keywords in prompt)
```

## Auto-Copy to Clipboard

**IMPORTANT**: After generating the optimized prompt, ALWAYS copy it to clipboard using:

```bash
cat << 'EOF' | pbcopy
[paste the optimized prompt content here - just the XML-tagged prompt, not the full output]
EOF
```

Then confirm: "✓ Optimized prompt copied to clipboard"

---

## XML Tagging Guide

### Common Tag Patterns

| Tag | Purpose |
|-----|---------|
| `<context>` | Background information, project details |
| `<instructions>` | Specific directions, numbered steps |
| `<examples>` | Sample inputs/outputs for reference |
| `<data>` | Source material, code snippets, logs |
| `<constraints>` | Limitations, requirements, must-haves |
| `<output_format>` | How the response should be structured |

### Best Practices

1. **Be consistent** - Use the same tag names and reference them explicitly
2. **Nest logically** - `<outer><inner></inner></outer>` for hierarchy
3. **Use descriptive names** - Make tag purpose obvious
4. **Separate concerns** - Don't mix instructions with examples

### Example Transformation

**Before:**
```
Write a function that validates domain data and make sure it handles the
trailing dot convention and also query ClickHouse to verify the data exists.
```

**After:**
```xml
<context>
Working in a data-pipeline testing framework with ClickHouse integration.
Domain data uses trailing dot convention (e.g., "example.com.").
</context>

<instructions>
1. Write a validation function for domain data
2. Handle trailing dot convention (add if missing, verify format)
3. Query ClickHouse to verify domain exists in the records table
4. Return validation result with clear error messages
</instructions>

<constraints>
- Use .query() for SELECT operations (not .execute())
- Domain format: must end with trailing dot
- Table: records
</constraints>
```

**Recommended**: Ask `@python-pro` to implement this since it involves Python with type hints and validation logic.

### Example with Subagent and Skill References

**Before:**
```
Review my PR for security issues and make sure the deployment configuration is correct.
```

**After:**
```xml
<context>
Reviewing a pull request that includes both application code and Kubernetes deployment manifests.
</context>

<instructions>
1. Run /pr-review-toolkit:review-pr for comprehensive PR review
2. Ask @security-reviewer to scan for security vulnerabilities
3. Ask @kubernetes-specialist to review the deployment configuration
4. Summarize findings with severity levels
</instructions>

<output_format>
Security findings grouped by severity (Critical, High, Medium, Low)
Deployment issues with recommended fixes
</output_format>
```

**Or invoke skill directly:** `/pr-review-toolkit:review-pr` then ask follow-up questions about specific findings.

---

## Available Subagents

Recommend these when specialized expertise improves the task. Reference them using `@agent-name` in your prompt.

### Core Development
| Agent | Use When |
|-------|----------|
| `@api-designer` | Designing REST/GraphQL APIs, OpenAPI specs, authentication flows |
| `@microservices-architect` | Service boundaries, communication patterns (REST/gRPC/messaging) |

### Language Specialists
| Agent | Use When |
|-------|----------|
| `@python-pro` | Python 3.11+, async/await, type hints, FastAPI, pytest |
| `@scala-expert` | Scala functional programming, pattern matching |

### Infrastructure
| Agent | Use When |
|-------|----------|
| `@docker-expert` | Dockerfiles, multi-stage builds, Docker Compose |
| `@kubernetes-specialist` | K8s deployments, Helm charts, RBAC, ingress |
| `@helm-charts-expert` | Helm chart development, values management, templating |
| `@terraform-engineer` | Terraform modules, state management |
| `@devops-engineer` | CI/CD pipelines, monitoring (Prometheus/Grafana) |
| `@deployment-engineer` | Blue-green/canary deployments, feature flags |
| `@argo-workflows-expert` | Argo Workflows, CronWorkflows, artifact management |
| `@argocd-expert` | ArgoCD, GitOps deployments, Application CRDs |
| `@github-actions-expert` | GitHub Actions workflows, Docker image builds, CI/CD |

### Quality & Security
| Agent | Use When |
|-------|----------|
| `@code-reviewer` | PR reviews, security vulnerabilities, code smells |
| `@code-quality-reviewer` | Function size, duplication, naming, complexity, readability |
| `@security-reviewer` | Security vulnerabilities, OWASP Top 10, credential scanning |
| `@test-automator` | Test frameworks, CI/CD test integration |
| `@test-coverage-analyzer` | Test coverage quality, test plan alignment |
| `@qa-expert` | Test strategies, quality metrics |
| `@debugger` | Complex bugs, stack traces, root cause analysis |
| `@architect-reviewer` | Architecture decisions, scalability assessment |
| `@architecture-reviewer` | Code architecture, separation of concerns, module organization |
| `@harsh-critic` | Adversarial code review, brutally honest feedback |
| `@unused-code-hunter` | Dead code detection, unused imports, unreachable code |

### Developer Experience
| Agent | Use When |
|-------|----------|
| `@tooling-engineer` | CLI tools, IDE extensions, code generators |
| `@mcp-developer` | MCP servers/clients, JSON-RPC 2.0 |
| `@refactoring-specialist` | Legacy code refactoring, design patterns |
| `@cli-developer` | CLI argument parsing, shell completions |
| `@git-workflow-manager` | Branching strategies, PR automation |

### Data & AI
| Agent | Use When |
|-------|----------|
| `@prompt-engineer` | System prompts, few-shot/chain-of-thought |
| `@llm-architect` | RAG systems, fine-tuning, inference optimization |

### Meta
| Agent | Use When |
|-------|----------|
| `@agent-organizer` | Complex multi-agent tasks, agent selection |

---

## Skill Triggers Reference

Skills can be invoked directly with `/skill-name` or auto-activate via keywords.

### User-Invocable Skills (use `/skill-name` directly)

| Skill | Use When |
|-------|----------|
| `/smart-commit` | Analyze branch changes, organize into logical commits |
| `/code-review` | Comprehensive code review using specialized agents |
| `/build-and-fix` | Run pytest collection and fix all errors iteratively |
| `/pr-review-toolkit:review-pr` | Comprehensive PR review using specialized agents |
| `/orbit:new` | Create orbit project (plan, context, tasks) for a new feature |
| `/orbit:go` | Resume work on an active orbit project |
| `/orbit:done` | Mark an active orbit project as completed |
| `/orbit:save` | Save orbit project progress before compaction or session end |
| `/sync-with-master` | Sync current branch with latest remote master |
| `/daily-report` | Generate daily sprint log from orbit tasks |

### Auto-Triggering Skills (include keywords in prompt)

| Skill | Keywords |
|-------|----------|
| `/pytest-patterns` | `pytest`, `fixture`, `parametrize`, `conftest`, `marker` |
| `/python-containerization` | `dockerfile`, `container`, `requirements.txt`, `ci/cd` |
| `/python-config-management` | `environment variable`, `.env`, `secret`, `kubernetes secret` |
| `/k8s-argo-workflows` | `argo workflow`, `kubectl`, `port forward`, `workflow submission` |
| `/data-pipeline-validation` | `validate data`, `delta table`, `prometheus metrics` |
| `/jira-issue-management` | `jira issue`, `ticket creation`, `time tracking` |
| `/message-polisher` | `slack message`, `polish message`, `make it casual/professional` |

---

## Making Agent Usage Prescriptive (for Task Prompts)

When generating prompts for autonomous execution (e.g., Ralph, orbit tasks), agents must be **required**, not just "available". Otherwise Claude may skip agent invocation for straightforward-seeming tasks.

### Pattern for Task Prompts

**Don't (informational only):**
```markdown
<agents>
## Available Agents
| Agent | Use For |
| python-pro | Python best practices |
</agents>
```

**Do (prescriptive):**
```markdown
<instructions>
...
6. Use the python-pro agent to review the implementation for best practices
</instructions>

<agents>
## Required Agents

Invoke these agents during task execution:

| Agent | Invoke With | When to Use |
|-------|-------------|-------------|
| python-pro | `subagent_type="python-pro"` | Review implementation before finalizing |
</agents>

<acceptance_criteria>
- [ ] Code reviewed by python-pro agent
</acceptance_criteria>
```

### Key Changes

1. **Add agent step to `<instructions>`** - Explicit numbered step to invoke agent
2. **Rename section to "Required Agents"** - Signals mandate, not option
3. **Add acceptance criterion** - Checkbox for agent review completion
4. **Include "When to Use"** - Clear action, not just capability

---

## Quick Checklist

When optimizing, ensure:

- [ ] Context separated in `<context>` tags
- [ ] Instructions numbered in `<instructions>` tags
- [ ] Examples included if helpful in `<examples>` tags
- [ ] Constraints explicit in `<constraints>` tags
- [ ] Subagents recommended with `@agent-name` format
- [ ] Skills suggested with `/skill-name` format
- [ ] Keywords included to trigger auto-activating skills
- [ ] **For task prompts**: Agent invocation in instructions + acceptance criteria
- [ ] **Optimized prompt copied to clipboard** (pbcopy)
