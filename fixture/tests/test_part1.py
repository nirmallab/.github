import os

import ci_fixture


def test_double():
    assert ci_fixture.double(1) == 2


def test_isolated():
    assert "ci-home" in os.environ["XDG_CONFIG_HOME"]
