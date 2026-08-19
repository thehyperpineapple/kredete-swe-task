.PHONY: run test clean

run:
	./run.sh

test:
	.venv/bin/python test_scenarios.py

clean:
	rm -f agent_runs.db agent_runs.db-wal agent_runs.db-shm
