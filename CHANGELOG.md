# Changelog

## [0.5.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.4.0...v0.5.0) (2026-08-20)


### Features

* **worker:** transcribe for the language and the vocabulary of the room ([#44](https://github.com/OneLiteFeatherNET/Sturnus/issues/44)) ([8822b6b](https://github.com/OneLiteFeatherNET/Sturnus/commit/8822b6b2193008be487b5d2c61133525b818a408))


### Bug Fixes

* **chart:** route the Sentry DSN through the Secret ([#40](https://github.com/OneLiteFeatherNET/Sturnus/issues/40)) ([dc4e4fb](https://github.com/OneLiteFeatherNET/Sturnus/commit/dc4e4fb342f38e47061db07ef01cc87a4b2dfade))
* **protocol:** give the attribution and the words their own paragraphs ([#42](https://github.com/OneLiteFeatherNET/Sturnus/issues/42)) ([4ba2716](https://github.com/OneLiteFeatherNET/Sturnus/commit/4ba27160439bdb4e00cf4f323918bba0c4bb8ec4))
* **worker:** stop Silero VAD from throwing away every transcript ([#45](https://github.com/OneLiteFeatherNET/Sturnus/issues/45)) ([21d79be](https://github.com/OneLiteFeatherNET/Sturnus/commit/21d79be445ac72a9830510d4c5a64f668368b9fc))

## [0.4.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.3.1...v0.4.0) (2026-08-20)


### Features

* name the channel, the date and the local time in the protocol ([#34](https://github.com/OneLiteFeatherNET/Sturnus/issues/34)) ([0c6baa8](https://github.com/OneLiteFeatherNET/Sturnus/commit/0c6baa846fb0d2975f0fdc57883bd8bb2a04b32f))
* **observability:** report errors to Sentry without shipping content ([#36](https://github.com/OneLiteFeatherNET/Sturnus/issues/36)) ([f58aea8](https://github.com/OneLiteFeatherNET/Sturnus/commit/f58aea8d9206409936b666aa4377333c0c381245))


### Bug Fixes

* **bot:** apply configuration changes without restarting the process ([#35](https://github.com/OneLiteFeatherNET/Sturnus/issues/35)) ([a943f87](https://github.com/OneLiteFeatherNET/Sturnus/commit/a943f87da4372262361db95472cf65fe5e29960b))
* serve link's health port before waiting for the schema ([#33](https://github.com/OneLiteFeatherNET/Sturnus/issues/33)) ([dfd509c](https://github.com/OneLiteFeatherNET/Sturnus/commit/dfd509c93cff190347a4af1a152871f0af256a9e))
* **voice:** decode Opus ourselves so one bad frame cannot end a recording ([#37](https://github.com/OneLiteFeatherNET/Sturnus/issues/37)) ([757828d](https://github.com/OneLiteFeatherNET/Sturnus/commit/757828d2f87ee09c74ee9ecec5fe81082538f077))

## [0.3.1](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.3.0...v0.3.1) (2026-08-20)


### Bug Fixes

* **chart:** close the commonEnv route to plaintext credentials ([#29](https://github.com/OneLiteFeatherNET/Sturnus/issues/29)) ([84886ec](https://github.com/OneLiteFeatherNET/Sturnus/commit/84886ecdd94ed492b0b2dae776412f0d00790ee9))
* **chart:** give each component only the secret keys it reads ([#17](https://github.com/OneLiteFeatherNET/Sturnus/issues/17)) ([c643792](https://github.com/OneLiteFeatherNET/Sturnus/commit/c64379285fdef31f7728d3381ef2639746ebb728))
* **chart:** let the worker update, and let pods carry cluster labels ([#11](https://github.com/OneLiteFeatherNET/Sturnus/issues/11)) ([90aa4f7](https://github.com/OneLiteFeatherNET/Sturnus/commit/90aa4f740f18e49c2b5a8f0f3cf54f0e15e9b419))
* **ci:** let release-please bump the sturnus version in uv.lock ([#13](https://github.com/OneLiteFeatherNET/Sturnus/issues/13)) ([2d4cbf0](https://github.com/OneLiteFeatherNET/Sturnus/commit/2d4cbf001842a8e42152e8ac97a673d982fe95a2))
* **ci:** pin setup-uv to a tag that exists ([#30](https://github.com/OneLiteFeatherNET/Sturnus/issues/30)) ([f35d977](https://github.com/OneLiteFeatherNET/Sturnus/commit/f35d9776e2383cade9ebf3aa6685fafc1aacd97d))
* refuse to start on a blank required setting ([#31](https://github.com/OneLiteFeatherNET/Sturnus/issues/31)) ([5048de1](https://github.com/OneLiteFeatherNET/Sturnus/commit/5048de1b309cdc2f130a4f3c530ad734b95dc43e))
* ship the driver Alembic needs to run migrations ([#14](https://github.com/OneLiteFeatherNET/Sturnus/issues/14)) ([b3846e3](https://github.com/OneLiteFeatherNET/Sturnus/commit/b3846e3d6af61e4ea0555709bae3b50dcc5b614e))

## [0.3.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.1.0...v0.3.0) (2026-08-20)


### Features

* foundation — domain logic, persistence and CI ([#1](https://github.com/OneLiteFeatherNET/Sturnus/issues/1)) ([9c24350](https://github.com/OneLiteFeatherNET/Sturnus/commit/9c243507a01dd3710d1769e374b24d2bc0d562d5))
* the capture path, the worker, linking and deployment ([#7](https://github.com/OneLiteFeatherNET/Sturnus/issues/7)) ([73099c1](https://github.com/OneLiteFeatherNET/Sturnus/commit/73099c109c195bcfa0c0c39afd6f87aca9945eec))

## Changelog
