test:
	PYTHONPATH=orbit-db:mcp-server/src:orbit-auto:orbit-dashboard:hooks \
	python3.11 -m pytest -v --tb=short

test-fast:
	PYTHONPATH=orbit-db:mcp-server/src:orbit-auto:orbit-dashboard:hooks \
	python3.11 -m pytest -x -q
