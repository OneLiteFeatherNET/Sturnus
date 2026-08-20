"""Regression test for Defect C: `setup_hook` must register every cog it owns.

A cog can exist, compile, and pass its own tests while never being
reachable at runtime -- the exact failure mode here was `SetupCog`,
`AudioCog` and the never-written `LinkCog` all sitting unregistered.
`setup_hook` is not run directly: doing so would reach `self.tree.sync()`,
a real Discord API call this test suite has no gateway connection for.
Parsing its source is enough to catch the one thing that actually broke --
a cog missing from the `add_cog(...)` calls.
"""

import ast
import inspect
import textwrap

from sturnus.infrastructure.discord.client import SturnusClient

#: Every cog `setup_hook` is expected to register. Extend this set in the
#: same commit that adds a new cog -- that is the whole point of this test.
EXPECTED_COGS = {
    "ConsentCog",
    "ConfigCog",
    "AboutCog",
    "SetupCog",
    "AudioCog",
    "LinkCog",
    "QueueCog",
}


def _registered_cog_names() -> set[str]:
    source = textwrap.dedent(inspect.getsource(SturnusClient.setup_hook))
    tree = ast.parse(source)
    return {
        call.func.id
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id.endswith("Cog")
    }


def test_setup_hook_registers_every_cog() -> None:
    assert _registered_cog_names() == EXPECTED_COGS
