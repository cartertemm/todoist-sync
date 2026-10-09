import json

import pytest
import requests

from todoist import BASE_URL, TodoistClient, TodoistError


class StubResponse:
	def __init__(self, payload=None, status_code=200):
		self.status_code = status_code
		self._payload = payload
		self.content = b"" if payload is None else json.dumps(payload).encode()

	def json(self):
		return self._payload

	def raise_for_status(self):
		if self.status_code >= 400:
			raise requests.HTTPError(str(self.status_code))


class StubSession:
	def __init__(self, *responses):
		self.responses = list(responses)
		self.requests = []
		self.headers = {}

	def request(self, method, url, **kwargs):
		self.requests.append((method, url, kwargs))
		return self.responses.pop(0)


def test_sets_auth_header():
	session = StubSession()
	TodoistClient("secret", session)
	assert session.headers["Authorization"] == "Bearer secret"


def test_open_tasks_follows_cursor():
	session = StubSession(
		StubResponse({"results": [{"id": "t1"}], "next_cursor": "abc"}),
		StubResponse({"results": [{"id": "t2"}], "next_cursor": None}),
	)
	tasks = TodoistClient("secret", session).open_tasks()
	assert [task["id"] for task in tasks] == ["t1", "t2"]
	assert session.requests[0][1] == f"{BASE_URL}/tasks"
	assert "cursor" not in session.requests[0][2]["params"]
	assert session.requests[1][2]["params"]["cursor"] == "abc"


def test_completed_since_reads_items():
	session = StubSession(StubResponse({"items": [{"id": "t1"}], "next_cursor": None}))
	items = TodoistClient("secret", session).completed_since("2026-10-08T00:00:00Z", "2026-10-09T00:00:00Z")
	assert items == [{"id": "t1"}]
	assert session.requests[0][1] == f"{BASE_URL}/tasks/completed/by_completion_date"
	assert session.requests[0][2]["params"]["since"] == "2026-10-08T00:00:00Z"


def test_get_task_returns_none_when_missing_or_deleted():
	session = StubSession(StubResponse({"error": "not found"}, status_code=404), StubResponse({"id": "t1", "is_deleted": True}))
	client = TodoistClient("secret", session)
	assert client.get_task("t1") is None
	assert client.get_task("t1") is None


def test_get_task_raises_on_server_error():
	client = TodoistClient("secret", StubSession(StubResponse({}, status_code=500)))
	with pytest.raises(requests.RequestException):
		client.get_task("t1")


def test_set_due_sends_sync_command():
	session = StubSession()
	client = TodoistClient("secret", session)

	def respond(method, url, **kwargs):
		session.requests.append((method, url, kwargs))
		command = json.loads(kwargs["data"]["commands"])[0]
		return StubResponse({"sync_status": {command["uuid"]: "ok"}})

	session.request = respond
	client.set_due("t1", {"date": "2026-10-16", "string": "every friday"})
	method, url, kwargs = session.requests[0]
	command = json.loads(kwargs["data"]["commands"])[0]
	assert (method, url) == ("POST", f"{BASE_URL}/sync")
	assert command["type"] == "item_update"
	assert command["args"] == {"id": "t1", "due": {"date": "2026-10-16", "string": "every friday"}}


def test_set_due_raises_when_sync_command_fails():
	session = StubSession()
	client = TodoistClient("secret", session)

	def respond(method, url, **kwargs):
		command = json.loads(kwargs["data"]["commands"])[0]
		return StubResponse({"sync_status": {command["uuid"]: {"error": "bad date"}}})

	session.request = respond
	with pytest.raises(TodoistError):
		client.set_due("t1", {"date": "nope"})


def test_write_calls_hit_expected_endpoints():
	session = StubSession(*[StubResponse() for _ in range(5)], StubResponse({"id": "t9"}))
	client = TodoistClient("secret", session)
	client.update_task("t1", content="New")
	client.move_task("t1", "p2")
	client.close_task("t1")
	client.reopen_task("t1")
	client.delete_task("t1")
	assert client.create_task(content="Made") == {"id": "t9"}
	calls = [(method, url.removeprefix(BASE_URL)) for method, url, _ in session.requests]
	assert calls == [
		("POST", "/tasks/t1"),
		("POST", "/tasks/t1/move"),
		("POST", "/tasks/t1/close"),
		("POST", "/tasks/t1/reopen"),
		("DELETE", "/tasks/t1"),
		("POST", "/tasks"),
	]
	assert session.requests[0][2]["json"] == {"content": "New"}
	assert session.requests[1][2]["json"] == {"project_id": "p2"}
