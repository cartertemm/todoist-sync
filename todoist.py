import json
import uuid

import requests

BASE_URL = "https://api.todoist.com/api/v1"
TIMEOUT_SECONDS = 30
PAGE_LIMIT = 200
NOT_FOUND = 404
SYNC_OK = "ok"


class TodoistError(requests.RequestException):
	pass


class TodoistClient:
	def __init__(self, token, session=None):
		self.session = session or requests.Session()
		self.session.headers["Authorization"] = f"Bearer {token}"

	def projects(self):
		return self._paged("/projects")

	def open_tasks(self):
		return self._paged("/tasks")

	def get_task(self, task_id):
		response = self.session.request("GET", f"{BASE_URL}/tasks/{task_id}", timeout=TIMEOUT_SECONDS)
		if response.status_code == NOT_FOUND:
			return None
		response.raise_for_status()
		task = response.json()
		return None if task.get("is_deleted") else task

	def completed_since(self, since, until):
		return self._paged("/tasks/completed/by_completion_date", key="items", params={"since": since, "until": until})

	def create_task(self, **fields):
		return self._request("POST", "/tasks", json=fields)

	def update_task(self, task_id, **fields):
		self._request("POST", f"/tasks/{task_id}", json=fields)

	def move_task(self, task_id, project_id):
		self._request("POST", f"/tasks/{task_id}/move", json={"project_id": project_id})

	def set_due(self, task_id, due):
		command = {"type": "item_update", "uuid": str(uuid.uuid4()), "args": {"id": task_id, "due": due}}
		result = self._request("POST", "/sync", data={"commands": json.dumps([command])})
		status = result["sync_status"][command["uuid"]]
		if status != SYNC_OK:
			raise TodoistError(f"Could not set due date on {task_id}: {status}")

	def close_task(self, task_id):
		self._request("POST", f"/tasks/{task_id}/close")

	def reopen_task(self, task_id):
		self._request("POST", f"/tasks/{task_id}/reopen")

	def delete_task(self, task_id):
		self._request("DELETE", f"/tasks/{task_id}")

	def _request(self, method, path, **kwargs):
		response = self.session.request(method, f"{BASE_URL}{path}", timeout=TIMEOUT_SECONDS, **kwargs)
		response.raise_for_status()
		return response.json() if response.content else None

	def _paged(self, path, key="results", params=None):
		params = {**(params or {}), "limit": PAGE_LIMIT}
		items = []
		while True:
			page = self._request("GET", path, params=dict(params))
			items.extend(page[key])
			if not page.get("next_cursor"):
				return items
			params["cursor"] = page["next_cursor"]
