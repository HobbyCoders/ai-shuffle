"""The advertised shell recipe preserves the check command's exit status."""
import re
import subprocess
import sys

import pytest

from fluid.tools.fragments import CI_WAIT_GUIDANCE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell recipe")
@pytest.mark.parametrize("status", [0, 1, 8], ids=["passed", "failed-or-cancelled", "pending"])
def test_ci_recipe_preserves_check_exit_code(status):
    recipe = re.search(r"sleep 20 && (.*?), all inside Monitor", CI_WAIT_GUIDANCE).group(0)
    recipe = recipe.removesuffix(", all inside Monitor").replace("<PR>", "123")
    # Stub only the external commands, not shell sequencing/status propagation.
    command = ('sleep() { :; }; gh() { printf "%s " "$@"; return '
               + str(status) + '; }; ' + recipe)
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=5)
    assert result.returncode == status
    assert result.stdout == "pr checks 123 --watch --fail-fast "
