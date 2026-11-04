# 常用命令入口。CI 与本地跑同一套，避免"本地绿、CI 红"。
# 环境：python -m venv .venv && pip install -e ".[dev]"

PY ?= python
DB ?= platform.db

.PHONY: help install check lint format type test cov smoke demo build clean

help:
	@echo "make install  安装开发依赖（editable）"
	@echo "make check    全套门禁：lint + format + type + test（含覆盖率门槛）"
	@echo "make demo     用示例数据跑一遍完整流程，产出 reports/dashboard_demo.html"
	@echo "make build    构建 sdist + wheel 到 dist/"
	@echo "make clean    清理构建与缓存产物（不动数据库）"

install:
	$(PY) -m pip install -e ".[dev]"

lint:
	ruff check src tests

format:
	ruff format --check src tests

type:
	mypy src

test:
	pytest -q

cov:
	pytest -q --cov=maldi_platform --cov-report=term-missing

smoke:
	$(PY) scripts/smoke.py

check: lint format type cov
	@echo "全部通过。"

demo:
	rm -f demo.db
	$(PY) -m maldi_platform init --db demo.db
	$(PY) -m maldi_platform dataset add --db demo.db --name synth-uti --version v1 \
		--classification synthetic --policy open --modality maldi-spectrum \
		--n-samples 40 --batch B1 --source ./examples/data
	$(PY) -m maldi_platform suite add --db demo.db --name m9-agent --dataset-version 1
	$(PY) -m maldi_platform run ingest --db demo.db --suite 1 --file examples/demo_report.json --label-prefix m9
	$(PY) -m maldi_platform compare --db demo.db --a 1 --b 3
	$(PY) -m maldi_platform gate --db demo.db --run 3 --baseline 1 || true
	$(PY) -m maldi_platform report --db demo.db --out reports/dashboard_demo.html

build: clean
	$(PY) -m build
	$(PY) -m zipfile -l dist/*.whl > /dev/null

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache .mypy_cache .coverage
