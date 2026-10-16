import argparse
import datetime
import json
import os
import textwrap
import tomllib
import traceback
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import requests

import workbook
from mapping import DATE_LENGTH, DONE_SUFFIX, FIELDS, Record, build_description, labels_for, task_to_record, to_api_priority
from todoist import TodoistClient, TodoistError
from workbook import Row

DEFAULT_MAX_DELETE_PERCENT = 25
COMPLETED_LOOKBACK_DAYS = 89
API_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PERCENT = 100
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "sync.log"
BACKUP_DIR = BASE_DIR / "backups"
BACKUPS_KEPT = 10
TOKEN_ENV = "TODOIST_API_TOKEN"
CLIENT_ERRORS = range(400, 500)
TOO_MANY_REQUESTS = 429
TRACE_INDENT = "\t"


class DeleteGuardError(Exception):
	def __init__(self, names):
		super().__init__(f"Sync stopped: it would delete {len(names)} tasks.")
		self.names = names


@dataclass
class Result:
	rows: list
	state: dict
	project_names: list
	log: list = field(default_factory=list)
	error: Exception | None = None


def merge(base, excel, todoist):
	final = {}
	push = {}
	for name in FIELDS:
		base_value = getattr(base, name)
		excel_value = getattr(excel, name)
		todoist_value = getattr(todoist, name)
		if todoist_value != base_value:
			final[name] = todoist_value
		elif excel_value != base_value:
			final[name] = excel_value
			push[name] = excel_value
		else:
			final[name] = base_value
	return Record(**final), push


def due_payload(record, task, repeat_changed):
	current = task.get("due") or {}
	if repeat_changed and record.repeat:
		return {"string": record.repeat}
	if not record.due:
		return None
	payload = {"date": record.due + current.get("date", "")[DATE_LENGTH:]}
	if current.get("is_recurring") and not repeat_changed:
		payload["string"] = current["string"]
	return payload


def http_status(error):
	return getattr(error.response, "status_code", None) if isinstance(error, requests.HTTPError) else None


def is_row_error(error):
	status = http_status(error)
	return isinstance(error, TodoistError) or (status in CLIENT_ERRORS and status != TOO_MANY_REQUESTS)


def run_sync(rows, client, state, owner, now, max_delete_percent=DEFAULT_MAX_DELETE_PERCENT, allow_deletes=False):
	return SyncRun(client, owner, now).run(rows, state, max_delete_percent, allow_deletes)


class SyncRun:
	def __init__(self, client, owner, now):
		self.client = client
		self.owner = owner
		self.now = now
		self.log = []
		self.handled = []
		self.kept_complete = 0

	def run(self, rows, state, max_delete_percent, allow_deletes):
		projects = self.client.projects()
		self.project_names = {project["id"]: project["name"] for project in projects}
		self.project_ids = {project["name"]: project["id"] for project in projects}
		self.inbox_id = next((project["id"] for project in projects if project.get("inbox_project")), None)
		self.open_tasks = {task["id"]: task for task in self.client.open_tasks()}
		snapshot = {task_id: Record(**data) for task_id, data in state.get("tasks", {}).items()}
		rows = self._split_copies(rows, snapshot)
		row_ids = {row.id for row in rows if row.id}
		self.taken = row_ids | snapshot.keys()
		self.completed_ids = self._completed_ids(state.get("last_sync"))
		fetched = self._fetch_missing(snapshot, row_ids)
		self._check_deletes(snapshot, row_ids, fetched, max_delete_percent, allow_deletes)
		old_handled = state.get("handled_completions", [])
		completions = self._recurring_completions() - set(old_handled)
		new_rows = []
		new_state = {}
		error = None
		for row in rows:
			written = len(new_rows)
			mark = len(self.handled)
			if error is None:
				try:
					self._sync_row(row, snapshot, fetched, completions, new_rows, new_state)
					continue
				except requests.RequestException as exc:
					if is_row_error(exc):
						self.log.append(f"row skipped, Todoist said {http_status(exc) or exc}: {row.record.content}")
					else:
						error = exc
			if len(new_rows) == written:
				new_rows.append(row)
				del self.handled[mark:]
			if row.id in snapshot:
				new_state[row.id] = snapshot[row.id]
		if self.kept_complete:
			self.log.append(f"kept {self.kept_complete} Complete rows without an ID in Excel only")
		if error is None:
			error = self._finish(snapshot, row_ids, new_rows, new_state)
		else:
			for task_id in snapshot.keys() - row_ids:
				new_state[task_id] = snapshot[task_id]
		last_sync = self.now.isoformat()
		handled = self.handled
		if error is not None:
			last_sync = state.get("last_sync", last_sync)
			handled = list(dict.fromkeys(old_handled + handled))
		new_state_data = {
			"last_sync": last_sync,
			"handled_completions": handled,
			"tasks": {task_id: asdict(item) for task_id, item in new_state.items()},
		}
		return Result(new_rows, new_state_data, list(self.project_names.values()), self.log, error)

	def _split_copies(self, rows, snapshot):
		owners = {}
		for index, row in enumerate(rows):
			if not row.id or row.id.endswith(DONE_SUFFIX):
				continue
			owner = owners.get(row.id)
			if owner is None or (rows[owner].record != snapshot.get(row.id) and row.record == snapshot.get(row.id)):
				owners[row.id] = index
		result = []
		for index, row in enumerate(rows):
			if owners.get(row.id, index) != index:
				self.log.append(f"copied row becomes a new task: {row.record.content}")
				row = replace(row, id=None)
			result.append(row)
		return result

	def _fetch_missing(self, snapshot, row_ids):
		fetched = {}
		for task_id in row_ids - self.open_tasks.keys():
			base = snapshot.get(task_id)
			if task_id.endswith(DONE_SUFFIX) or (base and base.status == "Complete"):
				continue
			fetched[task_id] = self.client.get_task(task_id)
		return fetched

	def _check_deletes(self, snapshot, row_ids, fetched, max_delete_percent, allow_deletes):
		names = [snapshot[task_id].content for task_id in snapshot if task_id not in row_ids and task_id in self.open_tasks]
		names += [
			snapshot[task_id].content for task_id in snapshot
			if task_id in fetched and fetched[task_id] is None and task_id not in self.completed_ids
		]
		if names and not allow_deletes and len(names) * PERCENT > max_delete_percent * len(snapshot):
			raise DeleteGuardError(names)

	def _completed_ids(self, last_sync):
		if not last_sync:
			return set()
		oldest = self.now - datetime.timedelta(days=COMPLETED_LOOKBACK_DAYS)
		since = max(datetime.datetime.fromisoformat(last_sync), oldest)
		items = self.client.completed_since(since.strftime(API_TIME_FORMAT), self.now.strftime(API_TIME_FORMAT))
		return {item.get("task_id") or item["id"] for item in items}

	def _recurring_completions(self):
		return {
			task_id for task_id in self.completed_ids
			if (self.open_tasks.get(task_id, {}).get("due") or {}).get("is_recurring")
		}

	def _sync_row(self, row, snapshot, fetched, completions, new_rows, new_state):
		if row.id and row.id.endswith(DONE_SUFFIX):
			new_rows.append(row)
			return
		if row.id is None and row.record.status == "Complete":
			self.kept_complete += 1
			new_rows.append(row)
			return
		if row.id is None:
			match = self._find_match(row.record)
			if match is None:
				self._create(row, new_rows, new_state)
				return
			self.log.append(f"linked row to existing task: {row.record.content}")
			row = replace(row, id=match)
		base = snapshot.get(row.id)
		if not row.record.content:
			self.log.append(f"row skipped, the Task cell is blank: {row.id}")
			new_rows.append(row)
			if base is not None:
				new_state[row.id] = base
			return
		task = self.open_tasks.get(row.id) or fetched.get(row.id)
		if task is not None:
			todoist = task_to_record(task, self.project_names, self.owner)
		elif row.id in self.completed_ids:
			todoist = replace(base or row.record, status="Complete")
		elif base is not None and base.status == "Complete":
			todoist = base
		elif base is not None:
			self.log.append(f"removed row: {base.content}")
			return
		else:
			self.log.append(f"unknown ID kept: {row.id}")
			new_rows.append(row)
			return
		if base is None:
			final, push = todoist, {}
		else:
			final, push = merge(base, row.record, todoist)
		history = []
		if row.id in completions and base is not None:
			history.append(self._history_row(row, base))
			self.handled.append(row.id)
			if push.get("status") == "Complete":
				del push["status"]
				final = replace(final, status=todoist.status)
		final, push = self._drop_invalid(final, push, todoist)
		if push:
			task = task or self.client.get_task(row.id)
			if task is None and todoist.status != "Complete":
				self.log.append(f"removed row: {final.content}")
				return
			if task is None:
				self.log.append(f"completed task can't be read, Excel edits not sent: {final.content}")
				final, push = todoist, {}
		if push:
			if push.get("status") == "Complete" and todoist.repeat:
				history.append(self._history_row(row, replace(final, due=todoist.due)))
			pushed = self._push(task, push, final)
			closed = push.get("status") == "Complete" and not todoist.repeat
			if pushed is None and not closed:
				self.log.append(f"removed row: {final.content}")
				return
			if pushed is not None:
				final = task_to_record(pushed, self.project_names, self.owner)
			self.log.append(f"updated in Todoist: {final.content}")
		elif final != row.record:
			self.log.append(f"updated row: {final.content}")
		new_rows.append(replace(row, record=final))
		new_rows.extend(history)
		new_state[row.id] = final

	def _history_row(self, row, record):
		return replace(row, id=row.id + DONE_SUFFIX, record=replace(record, status="Complete", repeat=None))

	def _drop_invalid(self, final, push, todoist):
		if "project" in push and push["project"] not in self.project_ids:
			self.log.append(f"unknown project skipped: {push['project']}")
			del push["project"]
			final = replace(final, project=todoist.project)
		if "repeat" in push and "due" in push:
			del push["due"]
			final = replace(final, due=todoist.due)
		return final, push

	def _push(self, task, push, final):
		task_id = task["id"]
		updates = {}
		if "content" in push:
			updates["content"] = final.content
		if "follow_up" in push or "notes" in push:
			updates["description"] = build_description(final.follow_up, final.notes)
		if "status" in push or "who" in push:
			updates["labels"] = labels_for(final, task.get("labels") or [])
		if "priority" in push:
			updates["priority"] = to_api_priority(final.priority)
		if updates:
			self.client.update_task(task_id, **updates)
		if "project" in push:
			self.client.move_task(task_id, self.project_ids[final.project])
		if "repeat" in push or "due" in push:
			self.client.set_due(task_id, due_payload(final, task, "repeat" in push))
		if "status" in push:
			if final.status == "Complete" and not task.get("checked"):
				self.client.close_task(task_id)
				if (task.get("due") or {}).get("is_recurring"):
					self.handled.append(task_id)
			elif final.status != "Complete" and task.get("checked"):
				self.client.reopen_task(task_id)
		return self.client.get_task(task_id)

	def _project_id(self, record):
		return self.project_ids.get(record.project) if record.project else self.inbox_id

	def _find_match(self, record):
		project_id = self._project_id(record)
		if not record.content or project_id is None:
			return None
		for task_id, task in self.open_tasks.items():
			if task_id not in self.taken and task["project_id"] == project_id and task["content"].strip() == record.content:
				self.taken.add(task_id)
				return task_id
		return None

	def _create(self, row, new_rows, new_state):
		record = row.record
		project_id = self._project_id(record)
		if not record.content or project_id is None:
			self.log.append(f"row skipped, needs a task name and a known project: {record.content}")
			new_rows.append(row)
			return
		fields = {
			"content": record.content,
			"description": build_description(record.follow_up, record.notes),
			"project_id": project_id,
			"labels": labels_for(record, []),
			"priority": to_api_priority(record.priority),
		}
		if record.repeat:
			fields["due_string"] = record.repeat
		elif record.due:
			fields["due_date"] = record.due
		created = self.client.create_task(**fields)
		task_id = created["id"]
		final = task_to_record(created, self.project_names, self.owner)
		new_rows.append(replace(row, id=task_id, record=final))
		new_state[task_id] = final
		self.log.append(f"created in Todoist: {final.content}")

	def _finish(self, snapshot, row_ids, new_rows, new_state):
		pending = [task_id for task_id in snapshot if task_id not in row_ids]
		for index, task_id in enumerate(pending):
			if task_id not in self.open_tasks:
				continue
			try:
				self.client.delete_task(task_id)
			except requests.RequestException as error:
				for rest in pending[index:]:
					new_state[rest] = snapshot[rest]
				return error
			self.log.append(f"deleted in Todoist: {snapshot[task_id].content}")
		for task_id, task in self.open_tasks.items():
			if task_id in self.taken:
				continue
			record = task_to_record(task, self.project_names, self.owner)
			new_rows.append(Row(task_id, record))
			new_state[task_id] = record
			self.log.append(f"added row: {record.content}")
		return None


def load_config(path):
	with open(path, "rb") as file:
		config = tomllib.load(file)
	return {
		"token": os.environ.get(TOKEN_ENV) or config["todoist_token"],
		"workbook": Path(config["workbook"]).expanduser(),
		"owner": config["owner"],
		"max_delete_percent": config.get("max_delete_percent", DEFAULT_MAX_DELETE_PERCENT),
	}


def load_state(path):
	if not path.exists():
		return {}
	return json.loads(path.read_text(encoding="utf-8"))


def save_state(path, state):
	temp = path.with_suffix(".tmp")
	temp.write_text(json.dumps(state, indent="\t"), encoding="utf-8")
	temp.replace(path)


def write_log(path, now, message):
	with open(path, "a", encoding="utf-8") as file:
		file.write(f"{now.isoformat(timespec='seconds')} {message}\n")


def describe_failure(error):
	trace = "".join(traceback.format_exception(error)).rstrip()
	return f"error: {type(error).__name__}: {error}\n" + textwrap.indent(trace, TRACE_INDENT)


def main(argv=None):
	parser = argparse.ArgumentParser(description="Sync Todoist tasks with an Excel workbook.")
	parser.add_argument("--allow-deletes", action="store_true", help="skip the mass delete check for this one run")
	parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.toml")
	args = parser.parse_args(argv)
	now = datetime.datetime.now(datetime.timezone.utc)
	try:
		return sync_once(args, now)
	except Exception as error:
		write_log(LOG_PATH, now, describe_failure(error))
		return 1


def sync_once(args, now):
	config = load_config(args.config)
	path = config["workbook"]
	owner = config["owner"]
	if workbook.is_open(path):
		write_log(LOG_PATH, now, "workbook is open, skipped")
		return 0
	if path.exists():
		try:
			rows = workbook.load_rows(path, owner)
		except workbook.WorkbookError as error:
			write_log(LOG_PATH, now, f"workbook problem, nothing changed: {error}")
			return 1
		state = load_state(STATE_PATH)
	else:
		rows, state = [], {}
	client = TodoistClient(config["token"])
	try:
		result = run_sync(rows, client, state, owner, now, config["max_delete_percent"], args.allow_deletes)
	except DeleteGuardError as error:
		print(str(error))
		print("Tasks it would delete:")
		for name in error.names:
			print(f"  {name}")
		print("If you meant this, run: uv run sync.py --allow-deletes")
		write_log(LOG_PATH, now, f"{error} Nothing changed.")
		return 1
	except requests.RequestException as error:
		write_log(LOG_PATH, now, f"Todoist error, nothing changed: {error}")
		return 1
	summary = "; ".join(result.log) or "no changes"
	if result.error:
		summary += f"; stopped early, Todoist error: {result.error}"
	workbook.backup(path, BACKUP_DIR, BACKUPS_KEPT)
	try:
		workbook.save_rows(path, result.rows, result.project_names, owner)
	except Exception as error:
		write_log(LOG_PATH, now, f"{summary}; workbook not saved, so state.json was not saved. {describe_failure(error)}")
		return 1
	save_state(STATE_PATH, result.state)
	write_log(LOG_PATH, now, summary)
	return 1 if result.error else 0


if __name__ == "__main__":
	raise SystemExit(main())
