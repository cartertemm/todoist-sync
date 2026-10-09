# todoist-sync

Keeps my Todoist tasks and an Excel workbook in sync. Change a task in either place and the next run copies it over to the other side.

## What you need

* Python 3.11 or newer
* A Todoist API token. In Todoist, open Settings, then Integrations, then Developer, and copy the token.

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux
pip install -r requirements.txt
```

Copy `config.example.toml` to `config.toml` and fill it in:

* `todoist_token`: your token. If you'd rather not keep it in a file, set the `TODOIST_API_TOKEN` environment variable instead. When both are set, the variable wins.
* `workbook`: where the spreadsheet lives, for example `~/OneDrive/Tasks.xlsx`.
* `owner`: your name. A task with you (or nobody) in the Who column gets no `who-` label.
* `max_delete_percent`: see the section on `--allow-deletes` below.

Then run:

```
python sync.py
```

If the workbook doesn't exist yet, the script builds it from your Todoist tasks.

## How the sheet lines up with Todoist

| Column | In Todoist |
| --- | --- |
| Task | The task name |
| Project | The project with that name. Pick it from the dropdown. |
| Start date | Nothing. It only lives in Excel. |
| Due date | The due date |
| Status | Todo, Complete, or Blocked. Blocked adds the `@blocked` label. |
| Who | A `@who-<name>` label, like `@who-Jane` |
| Follow-up on | A `Follow-up on: 2026-10-12` line in the description |
| Notes | The rest of the description |
| Repeat | The repeat text, like `every monday` |
| Priority | P1 to P4 |
| Link | Opens the task in Todoist |

There's also a hidden ID column. Leave it alone. It's how the script knows which row goes with which task. To add a task from Excel, fill in a new row and leave the ID blank.

## Close the workbook before a sync

The script can't write to the workbook while Excel or LibreOffice has it open. When it sees the file is open, it skips that run and notes it in `sync.log`. The next run tries again.

## When both sides change

If you change the same thing on both sides between syncs (say, the task name), Todoist wins. Changes to different things on the same task both stick.

## Repeating tasks

When you finish a repeating task, on either side, its row stays as Todo and moves to the next date. A new row shows up with Status Complete and the old date, so you keep a record. Those record rows never sync back to Todoist.

## The --allow-deletes flag

Deleting a row in Excel deletes the task in Todoist, and deleting a task in Todoist removes its row. That's what you want most of the time. The danger is a sync that reads a broken file. If the script ever reads an empty sheet, an old copy, or the wrong file, every task looks deleted, and Todoist has no undo for deletes made this way.

So there's a guard. If one sync would delete more than `max_delete_percent` of your tasks (25% unless you change it), it stops before it touches anything and prints the tasks it was about to delete.

Read that list. If you really did mean to delete them, for example because you cleared out an old project, run this once by hand:

```
python sync.py --allow-deletes
```

That turns the guard off for that one run only. Don't add the flag to your scheduled task, or the guard will never catch anything.

Deleting a row that's already Complete doesn't touch Todoist. It just tidies up the sheet.

## Run it every 5 minutes

Windows (run in a normal command prompt, and fix the paths):

```
schtasks /Create /SC MINUTE /MO 5 /TN "Todoist sync" /TR "\"C:\path\to\todoist-sync\.venv\Scripts\pythonw.exe\" \"C:\path\to\todoist-sync\sync.py\""
```

`pythonw.exe` keeps a console window from popping up every 5 minutes.

Linux, with `crontab -e`:

```
*/5 * * * * /home/you/todoist-sync/.venv/bin/python /home/you/todoist-sync/sync.py
```

## Files it makes

* `state.json`: what both sides looked like after the last sync. If you delete it, the next run treats Todoist as correct for every task.
* `sync.log`: one line per run.
* `backups/`: the last 10 copies of the workbook, taken before each write.

## Tests

```
python -m pytest
```
