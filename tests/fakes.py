import copy
import datetime

import requests

INBOX = {"id": "p-inbox", "name": "Inbox", "inbox_project": True}
WORK = {"id": "p-work", "name": "Work", "inbox_project": False}
HOME = {"id": "p-home", "name": "Home", "inbox_project": False}
RECUR_DAYS = 7
NEXT_RECURRING_DATE = "2026-10-12"


class FakeTodoist:
	def __init__(self):
		self.project_list = [INBOX, WORK, HOME]
		self.tasks = {}
		self.completed = []
		self.deleted = []
		self.calls = []
		self.fail_on = None
		self.fail_status = None
		self.next_id = 1

	def add(self, content, project_id="p-work", **extra):
		task_id = f"t{self.next_id}"
		self.next_id += 1
		self.tasks[task_id] = {
			"id": task_id,
			"content": content,
			"description": "",
			"project_id": project_id,
			"labels": [],
			"priority": 1,
			"due": None,
			"checked": False,
			**extra,
		}
		return task_id

	def projects(self):
		return copy.deepcopy(self.project_list)

	def open_tasks(self):
		return [copy.deepcopy(task) for task in self.tasks.values() if not task["checked"]]

	def get_task(self, task_id):
		return copy.deepcopy(self.tasks.get(task_id))

	def completed_since(self, since, until):
		return copy.deepcopy(self.completed)

	def create_task(self, **fields):
		self._call("create_task", fields)
		due = None
		if "due_string" in fields:
			due = {"date": NEXT_RECURRING_DATE, "string": fields.pop("due_string"), "is_recurring": True}
		elif "due_date" in fields:
			due = {"date": fields.pop("due_date"), "string": "", "is_recurring": False}
		task_id = self.add(fields.pop("content"), due=due, **fields)
		return copy.deepcopy(self.tasks[task_id])

	def update_task(self, task_id, **fields):
		self._call("update_task", task_id, fields)
		self.tasks[task_id].update(fields)

	def move_task(self, task_id, project_id):
		self._call("move_task", task_id, project_id)
		self.tasks[task_id]["project_id"] = project_id

	def set_due(self, task_id, due):
		self._call("set_due", task_id, due)
		if due is None:
			self.tasks[task_id]["due"] = None
		elif "date" in due:
			string = due.get("string", "")
			self.tasks[task_id]["due"] = {"date": due["date"], "string": string, "is_recurring": bool(string)}
		else:
			self.tasks[task_id]["due"] = {"date": NEXT_RECURRING_DATE, "string": due["string"], "is_recurring": True}

	def close_task(self, task_id):
		self._call("close_task", task_id)
		task = self.tasks[task_id]
		if task["due"] and task["due"]["is_recurring"]:
			task["due"]["date"] = _next_date(task["due"]["date"])
			self.completed.append({"id": task_id})
		else:
			task["checked"] = True

	def reopen_task(self, task_id):
		self._call("reopen_task", task_id)
		self.tasks[task_id]["checked"] = False

	def delete_task(self, task_id):
		self._call("delete_task", task_id)
		self.deleted.append(task_id)
		del self.tasks[task_id]

	def _call(self, name, *args):
		self.calls.append((name, *args))
		if self.fail_on == name and self.fail_status:
			response = requests.Response()
			response.status_code = self.fail_status
			raise requests.HTTPError(f"{self.fail_status} {name} failed", response=response)
		if self.fail_on == name:
			raise requests.RequestException(f"{name} failed")


def _next_date(iso):
	return (datetime.date.fromisoformat(iso[:10]) + datetime.timedelta(days=RECUR_DAYS)).isoformat()
