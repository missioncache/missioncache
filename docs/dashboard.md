# Dashboard

A local web page that shows every project you work on, what you owe, who owes you, and where your hours went.

Open **http://localhost:8787**. The full install starts it for you and keeps it running.

## The four views

| View | Use it to |
|---|---|
| **Attention** | See what needs you today. It opens here. |
| **Projects** | Find any project, active or done, and open its files. |
| **Activity** | See how much time you spent, per day, per project, per repo. |
| **Auto** | Watch a `missioncache-auto` run as it happens. |

### Attention

Three boxes on one screen:

- **My work.** Everything you owe: your action items, and the Waiting-on rows in any project that name you. Click **Done** when it is done.
- **Waiting on people.** What you asked other people for, grouped by how long they have been quiet: under a week, 1-2 weeks, 2-4 weeks, over a month.
- **Projects.** Only the projects with something open, each with its next step.

A project with nothing owed in either direction is not listed here. It is in **Projects**.

### Projects

Two tables, active and completed. Search, filter by category or repo, and click a row to open the project page. The page has tabs for an overview, the tasks, the context, the plan, the action items, and a graph of which tasks run by hand and which run on auto.

The page reads your files on every open, so an edit you make in your editor shows up after a refresh.

### Activity

Today's time, an hourly chart, a timeline of your sessions, and a heatmap of the last weeks.

The time counts every Claude Code session, even ones where no project was loaded. Those show up as **Untracked**. The **All / Tracked only** switch hides them.

### Auto

A graph of each running `missioncache-auto` job: every task, its state, and what it waits for. Open a run to follow its log live.

## Settings

Click **Settings** in the side bar. It has four tabs.

| Tab | What you set there |
|---|---|
| Statusline | Which lines the Claude Code statusline shows, and your own extra fields. |
| Repos & commits | A display name per repo, and which repos count toward stats. |
| Projects | Links for ticket keys like `PROJ-123` (one entry per prefix), and your own project categories with an emoji and a colour. |
| Calendar | Where your calendar comes from, for the day's schedule in `/missioncache:brief`. |

The theme switch is at the top right.

## Running it yourself

```bash
missioncache-dashboard status              # is it installed, is it running
missioncache-dashboard serve               # run it in this terminal
missioncache-dashboard serve --port 9000   # on another port
missioncache-dashboard install-service     # start it at login and keep it running
missioncache-dashboard uninstall-service   # stop that
```

The service is launchd on macOS, systemd on Linux, and a Task Scheduler task on Windows. On Windows its log is `~/.claude/logs/missioncache-dashboard-windows.log`.

If you move it to another port, set `MISSIONCACHE_DASHBOARD_URL` in your shell so the statusline links point to the right place.

## Links into the dashboard

| Link | Opens |
|---|---|
| `http://localhost:8787/#attention` | Attention |
| `http://localhost:8787/#projects` | Projects |
| `http://localhost:8787/#project/<name>` | One project's page |
| `http://localhost:8787/#activity` | Activity |
| `http://localhost:8787/#auto` | Auto |

The statusline uses these. Click the project name in your terminal and the page opens.

## When something looks wrong

**The page does not load.** Run `missioncache-dashboard status`. If it is not running, `missioncache-dashboard install-service`. If you just changed Python versions, `missioncache-dashboard reinstall-service`.

**The data looks old.** The dashboard copies your data to its own fast store once a minute. To do it now:

```bash
curl -X POST http://localhost:8787/api/sync
```

**Everything is empty, but your projects are fine.** The fast store (`~/.missioncache/tasks.duckdb`) can break when a machine shuts down hard. Your real data is in `~/.missioncache/tasks.db` and is not affected. Stop the service, delete `tasks.duckdb`, and start it again. It is rebuilt from `tasks.db`.

**The time here is higher than in the CLI.** Expected. The dashboard also counts Claude Code sessions outside any project, and takes the larger of the two numbers.

**No untracked sessions show up.** They come from Claude Code's own logs under `~/.claude/projects/`. If that folder is empty, there is nothing to show.

## More

How the time is counted, the full API and how to add a view: [internals/dashboard.md](internals/dashboard.md).
