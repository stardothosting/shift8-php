#!/usr/bin/env bats

setup() {
    PROJECT_ROOT=$(cd "$BATS_TEST_DIRNAME/.." && pwd)
}

@test "help identifies the non-deploying milestone boundary" {
    run bash "$PROJECT_ROOT/shift8-php" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Build, deploy and rollback are disabled"* ]]
}

@test "approval does not bypass deployment gate" {
    run bash "$PROJECT_ROOT/shift8-php" deploy php-cli --yes
    [ "$status" -eq 3 ]
    [[ "$output" == *"no services were restarted"* ]]
}

@test "milestone regression fixtures pass offline" {
    run python3 -m unittest discover -s "$PROJECT_ROOT/tests" -p 'test_*.py'
    [ "$status" -eq 0 ]
}
