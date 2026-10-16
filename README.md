# todoist-sync

This script keeps my Todoist tasks and an Excel workbook in sync with one another. The format is opinionated, and everyone has their own way of getting things done. This happens to work moderately well for me because I can start, complete, or add notes to tasks from whatever device happens to be closest at hand.

I am publishing it here because someone else might find it useful, not because I want to convert anyone to my way of thinking or tell others what they should do.

The mind works in mysterious ways. What works for me might not work for you, despite what productivity gurus selling courses might want you to believe.

But maybe you will try this simple system and find that things in your life become a bit more organized.

## What it does

In short, I can add a task in either Todoist or Excel, edit it, or mark it complete. The next sync copies the change to the other side.

Most fields sync both ways. Completing a repeating task moves its row to the next due date (if it's a recurring task) and leaves a completed row behind as a record.

If I accidentally change the same field in both places, the record stored in Todoist wins. Deleting works both ways too, although the script stops unusually large batches of deletions for me to review because I am paranoid.

## Before you start

You will need:

* [uv](https://docs.astral.sh/uv/) to manage Python and the project dependencies
* A Todoist API token
* A program that can open `.xlsx` files

You can find your Todoist token under **Settings**, **Integrations**, **Developer**.

## Setup

Download or clone this project, then open a terminal in its folder and run:

```console
uv sync
```

Make a copy of `config.example.toml` named `config.toml` (`cp config.example.toml config.toml`, and fill in these values:

```toml
todoist_token = "your token"
workbook = "~/OneDrive/Tasks.xlsx"
owner = "Your name"
max_delete_percent = 25
```

`workbook` is where the spreadsheet file should go. You will want to adjust the path based on the platform you're running, i.e. "c:\\users\\you\\dropbox\\tasks.xlsx" on windows. `owner` is the name that represents you in the **Who** column.

If you don't want your API token stored on disk, you can also keep the token out of this file by setting the `TODOIST_API_TOKEN` environment variable.

Now run the first sync:

```console
uv run sync.py
```

If the workbook does not exist, the script creates it from your open Todoist tasks. Otherwise, new rows become Todoist tasks and open Todoist tasks become rows.

## The workbook

Here is how each column relates to Todoist:

| Workbook column | Todoist equivalent |
| --- | --- |
| Task | Task name |
| Project | Project name |
| Start date | Nothing. This stays in Excel. |
| Due date | Due date |
| Status | Todo, Complete, or Blocked. Blocked adds `@blocked`. |
| Who | A label such as `@who-Jane` |
| Follow-up on | A dated line in the task description |
| Notes | The rest of the description |
| Repeat | Todoist repeat text, such as `every monday` |
| Priority | P1 through P4 |
| Link | A link to the task in Todoist |

The hidden **ID** column matches rows with Todoist tasks and is for internal purposes only. This should be left alone. A new row with a blank ID becomes a new task.

## Running it automatically

I run the script every five minutes on my homeserver.

You will want to do at least one successful manual sync before scheduling it, just to ensure everything works as it should.

On Windows, open Command Prompt and adjust both paths:

```console
schtasks /Create /F /SC MINUTE /MO 5 /TN "Todoist sync" /TR "\"C:\path\to\todoist-sync\.venv\Scripts\pythonw.exe\" \"C:\path\to\todoist-sync\sync.py\""
```

On Linux, add this with `crontab -e` and adjust both paths:

```cron
*/5 * * * * /home/you/todoist-sync/.venv/bin/python /home/you/todoist-sync/sync.py
```

## Safety and troubleshooting

Close the workbook before a sync. If it is open, the script skips that run and tries again next time.

`sync.log` records each run, `state.json` remembers the last successful sync, and `backups/` keeps the ten most recent workbook copies.

The deletion guard stops a sync if it would delete more than `max_delete_percent` of the known tasks. This protects me if the script reads an empty, old, or incorrect workbook.

When the guard stops a manual run, it prints the tasks it would have deleted. If the list is correct, I allow that one sync with:

```console
uv run sync.py --allow-deletes
```

Only put `--allow-deletes` in a scheduled command if you don't care about the safeguard. In reality, if something goes wrong, you would just have to restore from your `backups` folder, so it's not the end of the world.

## Tests

To run the test suite:

```console
uv run pytest
```
