.PHONY: test lint format bats legacy

test:
	python3 -m unittest discover -s tests -p 'test_*.py' -v

lint:
	bash -n shift8-php
	@for file in lib/*.sh tests/*.sh; do bash -n "$$file" || exit; done
	shellcheck -x shift8-php lib/*.sh tests/*.sh
	shfmt -d -i 4 -ci shift8-php lib/*.sh tests/cli.bats tests/*.sh

format:
	shfmt -w -i 4 -ci shift8-php lib/*.sh tests/cli.bats tests/*.sh

bats:
	bats tests/cli.bats

legacy:
	docker run --rm --network none --read-only --cap-drop=ALL \
		--security-opt=no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,exec \
		-v "$(CURDIR):/project:ro" -w /project python:2.7 \
		bash -c 'python -B tests/legacy_runtime.py && bash tests/legacy_cli.sh'
