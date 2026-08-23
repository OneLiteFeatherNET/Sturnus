"""The names behind the ids the console makes people type.

- `GET /api/guilds/{guild_id}/directory`
- `GET /api/outline/collections`

**What this is for.** Every control in the console that today asks a human
to paste a snowflake: `voice_channel_id`, `consent_role_id`,
`admin_role_id` in the settings form, and `document_target`, which is an
Outline collection UUID. Each of them is stored as an id, shown back as
that id, and configured by copying a string out of another window and
hoping it was the right one. These two endpoints are what turns those
controls into pickers -- the name is the value, and the id is the subtext.

**What this is not.** It is not a Discord API proxy and it is not an
Outline API proxy. This process holds neither token and must not be given
either (Spec 13.2): it already holds S3 and the master key, so it can
decrypt every recording ever made, and a process with that reach is not
one to also hand the ability to act as the bot. So it answers only what
the bot and the worker mirrored, only as fresh as their last sweep, and
only for a guild the caller administers. Nothing here is live truth, which
is why every answer carries `synced_at` -- a console that says "as the bot
last saw it, four minutes ago" is describing what this actually is, and a
picker that silently presents a stale list is how somebody configures a
channel that was deleted last week.

**Deliberately not the whole guild.** `guild_member` holds the consent
role's and admin role's members and nobody else (see
`sturnus.application.directory_mirror.members_to_mirror`). A person the
mirror does not know is a person the console shows as a bare id with a
note that it could not be resolved -- never a blank and never a silently
dropped option, because a channel or a colleague that vanished from
Discord is a configuration problem an administrator needs to see.

**404, never 403.** A guild this person does not administer answers
exactly as a guild that does not exist -- the directory names the people
who consented to being recorded in it, and a 403 would confirm to somebody
just established as having no business with that list that it exists. The
collection list answers the same way to somebody who administers nothing
at all, for consistency rather than for secrecy: they never configure a
`document_target`, so the list addresses nothing for them.

**No decision is taken here.** Both authorisation rules live inside the
adapters (`sturnus.console.ports.GuildNames`, `CollectionNames`), where a
handler cannot forget to apply one, and the ordering is done in the
statements. This module is the shape of two HTTP responses and nothing
else.
"""

from __future__ import annotations

from datetime import datetime

from aiohttp import web

from sturnus.application.collection_mirror import MirroredCollection
from sturnus.application.directory_mirror import (
    MirroredChannel,
    MirroredMember,
    MirroredRole,
)
from sturnus.console.ports import CollectionNames, GuildNames

#: Where the collaborators are found. Their own keys rather than
#: parameters to `register`, so `build_api` stays a two-line edit --
#: several agents are adding sections to that function and each extra
#: line is a merge by hand.
GUILD_NAMES: web.AppKey[GuildNames] = web.AppKey("guild_names")
COLLECTION_NAMES: web.AppKey[CollectionNames] = web.AppKey("collection_names")

_DIRECTORY_PATH = "/api/guilds/{guild_id}/directory"
_COLLECTIONS_PATH = "/api/outline/collections"

#: The two refusals, each covering every reason there is to refuse. See
#: the module docstring on why "there is none" and "not yours" are one
#: answer.
_NO_SUCH_GUILD = "no such guild"
_NO_SUCH_COLLECTION_LIST = "no such collection list"

#: It names people who agreed to be recorded, and what a guild's channels
#: are called. Nothing in between this and the browser has any business
#: keeping a copy.
_PRIVATE = {"Cache-Control": "private, no-store"}


def register(app: web.Application) -> None:
    """Adds the name routes to an application that already has its mirrors."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_DIRECTORY_PATH, require_session(guild_directory)),
            web.get(_COLLECTIONS_PATH, require_session(outline_collections)),
        ]
    )


async def guild_directory(request: web.Request) -> web.Response:
    """One guild's channels, roles and named people, as last mirrored."""
    viewer = _caller(request)
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        # A path segment that is not a number names no guild, which is the
        # same answer as naming one that does not exist -- and the same
        # answer as naming one this person does not administer.
        return _refusal(_NO_SUCH_GUILD)

    directory = await request.app[GUILD_NAMES].for_guild(guild_id, requested_by=viewer)
    if directory is None:
        return _refusal(_NO_SUCH_GUILD)

    return web.json_response(
        {
            "guild_id": str(guild_id),
            "synced_at": _moment(directory.synced_at),
            "channels": [_channel_json(channel) for channel in directory.channels],
            "roles": [_role_json(role) for role in directory.roles],
            "members": [_member_json(member) for member in directory.members],
        },
        headers=_PRIVATE,
    )


async def outline_collections(request: web.Request) -> web.Response:
    """The collections the worker mirrored, for the `document_target` picker."""
    listing = await request.app[COLLECTION_NAMES].mirrored(requested_by=_caller(request))
    if listing is None:
        return _refusal(_NO_SUCH_COLLECTION_LIST)

    return web.json_response(
        {
            "synced_at": _moment(listing.synced_at),
            "collections": [_collection_json(entry) for entry in listing.collections],
        },
        headers=_PRIVATE,
    )


# ---------------------------------------------------------------------------
# Writing the response
# ---------------------------------------------------------------------------


def _channel_json(channel: MirroredChannel) -> dict[str, object]:
    return {
        # A Discord snowflake exceeds JavaScript's safe integer range,
        # where a JSON number silently loses its last digits and produces
        # an id that looks right and names nothing.
        "id": str(channel.channel_id),
        "name": channel.name,
        # A plain string rather than a closed set: Discord keeps adding
        # channel types, and a kind this build has never seen must be
        # something the console can render rather than something a
        # response cannot carry.
        "kind": channel.kind,
        # Carried so the console can render the guild's own order without
        # asking why two entries are adjacent.
        "position": channel.position,
    }


def _role_json(role: MirroredRole) -> dict[str, object]:
    return {"id": str(role.role_id), "name": role.name, "position": role.position}


def _member_json(member: MirroredMember) -> dict[str, object]:
    return {"discord_user_id": str(member.discord_user_id), "display_name": member.display_name}


def _collection_json(collection: MirroredCollection) -> dict[str, object]:
    # Not a snowflake -- Outline issues UUIDs -- but a string for the same
    # reason `document_target` stores one: the id is opaque and the
    # console's job is to hand it back unchanged.
    return {"id": collection.collection_id, "name": collection.name}


def _moment(when: datetime | None) -> str | None:
    """When the mirror was last written, or `None` if it never was.

    Present-and-null rather than absent, so a client never has to tell
    "the sweep has not run yet" from "an API that does not send this".
    """
    return None if when is None else when.isoformat()


def _refusal(reason: str) -> web.Response:
    """One refusal for every reason there is to refuse.

    "There is no such thing" and "you may not read it" are deliberately
    indistinguishable; see the module docstring.
    """
    return web.json_response({"error": reason}, status=404)


def _caller(request: web.Request) -> int:
    """The Discord id of the person making this request.

    Only ever reached from behind `require_session`, which is what
    guarantees there is one -- `current_user` raises rather than returning
    `None` if that is ever untrue, so a route registered without the
    wrapper fails loudly instead of quietly acting for somebody else.
    """
    from sturnus.console.app import current_user

    return current_user(request).discord_user_id
