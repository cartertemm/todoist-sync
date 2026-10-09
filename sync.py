import datetime
from dataclasses import asdict, dataclass, field, replace

import requests

from mapping import DATE_LENGTH, DONE_SUFFIX, FIELDS, Record, build_description, labels_for, task_to_record, to_api_priority
from workbook import Row

DEFAULT_MAX_DELETE_PERCENT = 25
COMPLETED_LOOKBACK_DAYS = 89
API_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PERCENT = 100


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


def run_sync(rows, client, state, owner, now, max_delete_percent=DEFAULT_MAX_DELETE_PERCENT, allow_deletes=False):
	return SyncRun(client, owner, now).run(rows, state, max_delete_percent, allow_deletes)


class SyncRun:
	def __init__(self, client, owner, now):
		self.client = client
		self.owner = owner
		self.now = now
		self.log = []

	def run(self, rows, state, max_delete_percent, allow_deletes):
		projects = self.client.projects()
		self.project_names = {project["id"]: project["name"] for project in projects}
		self.project_ids = {project["name"]: project["id"] for project in projects}
		self.inbox_id = next(project["id"] for project in projects if project.get("inbox_project"))
		self.open_tasks = {task["id"]: task for task in self.client.open_tasks()}
		snapshot = {task_id: Record(**data) for task_id, data in state.get("tasks", {}).items()}
		row_ids = {row.id for row in rows if row.id}
		fetched = self._fetch_missing(snapshot, row_ids)
		self._check_deletes(snapshot, row_ids, fetched, max_delete_percent, allow_deletes)
		completions = self._recurring_completions(state.get("last_sync"))
		new_rows = []
		new_state = {}
		error = None
		for row in rows:
			if error is None:
				try:
					self._sync_row(row, snapshot, fetched, completions, new_rows, new_state)
					continue
				except requests.RequestException as exc:
					error = exc
			new_rows.append(row)
			if row.id in snapshot:
				new_state[row.id] = snapshot[row.id]
		if error is None:
			error = self._finish(snapshot, row_ids, new_rows, new_state)
		else:
			for task_id in snapshot.keys() - row_ids:
				new_state[task_id] = snapshot[task_id]
		new_state_data = {"last_sync": self.now.isoformat(), "tasks": {task_id: asdict(item) for task_id, item in new_state.items()}}
		return Result(new_rows, new_state_data, list(self.project_names.values()), self.log, error)

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
		names += [snapshot[task_id].content for task_id in snapshot if task_id in fetched and fetched[task_id] is None]
		if names and not allow_deletes and len(names) * PERCENT > max_delete_percent * len(snapshot):
			raise DeleteGuardError(names)

	def _recurring_completions(self, last_sync):
		if not last_sync:
			return set()
		oldest = self.now - datetime.timedelta(days=COMPLETED_LOOKBACK_DAYS)
		since = max(datetime.datetime.fromisoformat(last_sync), oldest)
		items = self.client.completed_since(since.strftime(API_TIME_FORMAT), self.now.strftime(API_TIME_FORMAT))
		ids = {item.get("task_id") or item["id"] for item in items}
		return {task_id for task_id in ids if (self.open_tasks.get(task_id, {}).get("due") or {}).get("is_recurring")}

	def _sync_row(self, row, snapshot, fetched, completions, new_rows, new_state):
		if row.id and row.id.endswith(DONE_SUFFIX):
			new_rows.append(row)
			return
		if row.id is None:
			self._create(row, new_rows, new_state)
			return
		base = snapshot.get(row.id)
		task = self.open_tasks.get(row.id) or fetched.get(row.id)
		if task is not None:
			todoist = task_to_record(task, self.project_names, self.owner)
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
			if push.get("status") == "Complete":
				del push["status"]
				final = replace(final, status=todoist.status)
		final, push = self._drop_invalid(final, push, todoist)
		if push:
			task = task or self.client.get_task(row.id)
			if task is None:
				self.log.append(f"removed row: {final.content}")
				return
			if push.get("status") == "Complete" and todoist.repeat:
				history.append(self._history_row(row, replace(final, due=todoist.due)))
			task = self._push(task, push, final)
			final = task_to_record(task, self.project_names, self.owner)
			self.log.append(f"updated in Todoist: {final.content}")
		elif final != row.record:
			self.log.append(f"updated row: {final.content}")
		new_rows.append(Row(row.id, final, row.start_date))
		new_rows.extend(history)
		new_state[row.id] = final

	def _history_row(self, row, record):
		return Row(row.id + DONE_SUFFIX, replace(record, status="Complete", repeat=None), row.start_date)

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
			elif final.status != "Complete" and task.get("checked"):
				self.client.reopen_task(task_id)
		return self.client.get_task(task_id)

	def _create(self, row, new_rows, new_state):
		record = row.record
		project_id = self.project_ids.get(record.project) if record.project else self.inbox_id
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
		task_id = self.client.create_task(**fields)["id"]
		final = task_to_record(self.client.get_task(task_id), self.project_names, self.owner)
		new_rows.append(Row(task_id, final, row.start_date))
		new_state[task_id] = final
		self.log.append(f"created in Todoist: {final.content}")
		if record.status == "Complete":
			self.client.close_task(task_id)
			new_rows[-1] = Row(task_id, task_to_record(self.client.get_task(task_id), self.project_names, self.owner), row.start_date)
			new_state[task_id] = new_rows[-1].record

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
			if task_id in row_ids or task_id in snapshot:
				continue
			record = task_to_record(task, self.project_names, self.owner)
			new_rows.append(Row(task_id, record))
			new_state[task_id] = record
			self.log.append(f"added row: {record.content}")
		return None
