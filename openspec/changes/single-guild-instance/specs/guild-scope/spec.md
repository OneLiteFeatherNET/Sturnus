# Spec Delta

## Purpose

Which Discord guild an installation serves, where its commands exist, who may
run them, and the one guard that holds if the platform setting protecting the
installation is wrong.

## ADDED Requirements

### Requirement: An installation names the one guild it serves

The guild the bot serves SHALL be named by configuration and MUST have no
default value. A bot started without it MUST fail to start rather than serve
whichever guilds it happens to find itself in.

#### Scenario: No guild configured

- **WHEN** the bot is started without a configured guild
- **THEN** it fails to start and reports the missing configuration
- **AND** no gateway session is established

#### Scenario: Guild configured

- **WHEN** the bot is started with a configured guild
- **THEN** it serves that guild

### Requirement: Commands exist only in the served guild

The bot SHALL register its application commands for the configured guild only,
and MUST remove any commands it previously registered globally. A command MUST
NOT exist in any other guild.

#### Scenario: Startup registers guild-scoped commands

- **WHEN** the bot starts with a configured guild
- **THEN** its commands are synchronised to that guild alone
- **AND** no command is registered globally

#### Scenario: Upgrade from global registration

- **WHEN** the bot starts after a release that registered commands globally
- **THEN** the previously registered global commands are removed
- **AND** the served guild's commands are never absent in between

#### Scenario: The bot is in another guild

- **WHEN** the bot is present in a guild other than the configured one
- **THEN** none of its commands are offered there

### Requirement: Command access is decided by Discord's permissions

Administrative commands (`/config`, `/queue`, `/audio-purge`, `/setup`) SHALL
declare Manage Server as their default member permission and rely on Discord to
withhold them from members who lack it. The bot MUST NOT apply a second,
in-process administrator check to these commands. Overrides a guild administrator
sets in Server Settings > Integrations apply as Discord applies them.

#### Scenario: Member without the permission

- **WHEN** a member without the required permission, and without an override
  granting it, looks for an administrative command
- **THEN** Discord does not offer or deliver it, and the bot performs no action

#### Scenario: Override in Integrations

- **WHEN** a server administrator grants or removes access to a command for a
  role or user in Integrations
- **THEN** the bot honours that outcome without consulting `admin_role_id`

#### Scenario: Commands open to everyone

- **WHEN** a member runs `/consent`, `/link` or `/about`
- **THEN** they work without administrative permission, as before

### Requirement: The console administrator mirror is preserved

The `admin_role_id` configuration and the mirroring of that role's members into
`admin_member` SHALL continue unchanged, because console authorization depends
on them, independent of how commands are permissioned.

#### Scenario: Role members still reach the console

- **WHEN** a member holds the role stored as `admin_role_id`
- **THEN** they are mirrored into `admin_member`
- **AND** console sign-in through Outline authorizes them as before

#### Scenario: Role no longer affects commands

- **WHEN** a member holds `admin_role_id` but lacks Manage Server and any
  Integrations override
- **THEN** they cannot run administrative commands

### Requirement: No other guild is ever acted on

As defence in depth, the system SHALL treat every guild other than the configured
one as out of scope at the head of the reconcile path that all recording starts
pass through. For such a guild it MUST NOT begin a recording, write
configuration, apply a setup, or join a voice channel.

#### Scenario: The bot is added elsewhere

- **WHEN** the bot is added to a guild other than the configured one
- **THEN** nothing is recorded there and no configuration is written for it
- **AND** the refusal is logged

#### Scenario: A meeting starts in another guild

- **WHEN** people join a voice channel of a guild that is not the configured one
- **THEN** no voice connection is opened and no session is created

#### Scenario: The refusal carries no names

- **WHEN** a guild is refused
- **THEN** the log line contains the configured and the seen guild id
- **AND** no guild name, and it is emitted per reconcile and not per audio frame

### Requirement: The system never leaves a guild on its own

The system SHALL NOT remove itself from any guild as a consequence of
configuration. Leaving is a deliberate act for a person to take.

#### Scenario: Misconfigured guild id

- **WHEN** the configured guild id names a guild the bot is not in, or names the
  wrong one
- **THEN** the bot remains in every guild it is in
- **AND** correcting the configuration and restarting is enough to restore
  service, with no re-invitation and no repeated setup
