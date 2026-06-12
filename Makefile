test:
	PYTHONPATH=missioncache-db:mcp-server/src:missioncache-auto:missioncache-dashboard:hooks \
	python3.11 -m pytest -v --tb=short

test-fast:
	PYTHONPATH=missioncache-db:mcp-server/src:missioncache-auto:missioncache-dashboard:hooks \
	python3.11 -m pytest -x -q
