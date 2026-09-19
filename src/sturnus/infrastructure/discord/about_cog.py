"""The `/about` slash command: the AGPL section 13 network-use offer.

Sturnus is a self-hosted network service, not a distributed binary, so the
AGPL's copyleft only reaches the people who use it if the offer to receive
the corresponding source is made directly to them -- shipping a `LICENSE`
file in the repository is not enough on its own (AGPL-3.0 section 13,
"Remote Network Interaction"). `/about` makes that offer to whoever invokes
it, and -- like every other command in this codebase -- replies only to
that person: ephemerally.

`about_text` is a free function, deliberately separated from the Discord
plumbing, so the content of the notice -- and in particular that it keeps
naming the license and the repository -- is tested without a gateway
connection.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from sturnus import __version__

#: Canonical source location Sturnus offers under AGPL-3.0 section 13.
REPOSITORY_URL = "https://github.com/OneLiteFeatherNET/Sturnus"

#: SPDX identifier for the license Sturnus is distributed under; kept as a
#: constant so `pyproject.toml`'s `license` field and this notice cannot
#: silently drift apart.
LICENSE_NAME = "AGPL-3.0-or-later"


def about_text(
    *,
    transcription_host: str,
    version: str = __version__,
    repository_url: str = REPOSITORY_URL,
) -> str:
    """Builds the `/about` notice: name, version, license, source, endpoint.

    Kept separate from the Discord response so a future edit cannot
    silently drop the license name or the repository link without a test
    noticing -- AGPL-3.0 section 13 requires this offer to reach users of
    a modified, network-deployed version, not just live in a repository
    nobody using the bot ever sees.

    **`transcription_host` is here for the same reason the rest is.**
    `/about` is the one command whose whole purpose is telling a user what
    this deployment actually *is*, and since transcription happens at an
    endpoint the operator chose, where a person's voice is sent is part of
    that answer. It is a required argument rather than one with a default
    so that no deployment can answer the question by omission.

    The host only, never the key: `endpoint_host` takes the URL apart, and
    a base URL's path or query is where a credential would be.
    """
    return (
        f"**Sturnus** v{version}\n"
        f"Licensed under the GNU Affero General Public License v3.0 or later "
        f"({LICENSE_NAME}).\n"
        f"Source code, including this deployment's corresponding source, is "
        f"available at: {repository_url}\n"
        f"Speech is transcribed by: {transcription_host}"
    )


class AboutCog(commands.Cog):
    """`/about` command; open to everyone, and always ephemeral."""

    def __init__(self, transcription_host: str) -> None:
        self._transcription_host = transcription_host
        super().__init__()

    @app_commands.command(
        name="about",
        description="Show Sturnus's version, license and where to get the source code.",
    )
    async def about(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            about_text(transcription_host=self._transcription_host), ephemeral=True
        )


__all__ = ["AboutCog", "LICENSE_NAME", "REPOSITORY_URL", "about_text"]
