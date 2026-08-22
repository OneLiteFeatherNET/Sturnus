# Changelog

## [0.14.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.13.0...v0.14.0) (2026-08-22)


### Features

* **bot:** ask Discord for video, since nothing in the stack ever does ([#98](https://github.com/OneLiteFeatherNET/Sturnus/issues/98)) ([e40643c](https://github.com/OneLiteFeatherNET/Sturnus/commit/e40643c28bcb6d3478ffbcf5a1d4117098fb0182))
* **console:** a guild-wide view of what the transcription queue still owes ([#106](https://github.com/OneLiteFeatherNET/Sturnus/issues/106)) ([8d71b7d](https://github.com/OneLiteFeatherNET/Sturnus/commit/8d71b7d7ad70f42c8c8ae647436be53b64fb94bd))
* **console:** let a participant find one recording among hundreds ([#101](https://github.com/OneLiteFeatherNET/Sturnus/issues/101)) ([f1542ac](https://github.com/OneLiteFeatherNET/Sturnus/commit/f1542ac2ce10bc69bf49c86126df97623bbb6c69))
* **console:** let a participant label their own recordings ([#99](https://github.com/OneLiteFeatherNET/Sturnus/issues/99)) ([f6518f2](https://github.com/OneLiteFeatherNET/Sturnus/commit/f6518f2ef1c2f0db4b1683b38ed9b451c3dac634))
* **console:** let an administrator withdraw a person's recording consent ([#105](https://github.com/OneLiteFeatherNET/Sturnus/issues/105)) ([8615772](https://github.com/OneLiteFeatherNET/Sturnus/commit/861577262143acc8cb20ad7a5e344dfc1af45787))
* **console:** report what a guild's recording adds up to ([#107](https://github.com/OneLiteFeatherNET/Sturnus/issues/107)) ([1d0fe78](https://github.com/OneLiteFeatherNET/Sturnus/commit/1d0fe781d2c862222f34584ea55f68cfd596ad7b))
* **console:** serve the recordings list one page at a time ([#111](https://github.com/OneLiteFeatherNET/Sturnus/issues/111)) ([e8693ff](https://github.com/OneLiteFeatherNET/Sturnus/commit/e8693ff296f0144d8d4cc8cdda5a7616d13180ef))
* **console:** split the navigation into a User View and an Admin View ([#104](https://github.com/OneLiteFeatherNET/Sturnus/issues/104)) ([036b216](https://github.com/OneLiteFeatherNET/Sturnus/commit/036b216b27839217e511c2f3081164e892d8fb69))
* **worker:** let a re-queue name the model, and record what actually ran ([#112](https://github.com/OneLiteFeatherNET/Sturnus/issues/112)) ([f1b8a8c](https://github.com/OneLiteFeatherNET/Sturnus/commit/f1b8a8c943cff7d41b18b535ac39d2e30063ff72))


### Bug Fixes

* **console:** make the colours of every control readable in both themes ([#103](https://github.com/OneLiteFeatherNET/Sturnus/issues/103)) ([6486859](https://github.com/OneLiteFeatherNET/Sturnus/commit/6486859244aa9db6c6317363d51a65d1dad91278))
* **console:** repair the recording flow's leaks, traps and dead ends ([#102](https://github.com/OneLiteFeatherNET/Sturnus/issues/102)) ([ea20bbf](https://github.com/OneLiteFeatherNET/Sturnus/commit/ea20bbfbad046fefca988548af8efe8f4f5e57cb))
* **console:** stop a failed queue read from revealing the admin panel ([#108](https://github.com/OneLiteFeatherNET/Sturnus/issues/108)) ([dea1f8b](https://github.com/OneLiteFeatherNET/Sturnus/commit/dea1f8bf1a8cebec254de721acfbc3df4af032e9))

## [0.13.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.12.1...v0.13.0) (2026-08-22)


### Features

* **bot:** find out whether Discord sends this bot video at all ([#95](https://github.com/OneLiteFeatherNET/Sturnus/issues/95)) ([c4b39db](https://github.com/OneLiteFeatherNET/Sturnus/commit/c4b39db80e16b26cd1745619aebca8674e3fda25))


### Bug Fixes

* **audio:** give the resampler the frame before the one it is converting ([#96](https://github.com/OneLiteFeatherNET/Sturnus/issues/96)) ([1c20918](https://github.com/OneLiteFeatherNET/Sturnus/commit/1c20918e821aa8a57f0ac9556326beec9dbf3c29))

## [0.12.1](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.12.0...v0.12.1) (2026-08-22)


### Bug Fixes

* **bot:** decrypt Discord's end-to-end layer before decoding Opus ([#93](https://github.com/OneLiteFeatherNET/Sturnus/issues/93)) ([ce43ad3](https://github.com/OneLiteFeatherNET/Sturnus/commit/ce43ad39fc599367da93b692157de412a8498cf1))

## [0.12.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.11.0...v0.12.0) (2026-08-22)


### Features

* **bot:** check the arithmetic that cuts the payload out of the packet ([#91](https://github.com/OneLiteFeatherNET/Sturnus/issues/91)) ([109fe8e](https://github.com/OneLiteFeatherNET/Sturnus/commit/109fe8ed71ae5aa6dba53ad9db65cdde97b4c145))

## [0.11.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.10.0...v0.11.0) (2026-08-22)


### Features

* **bot:** find where the real Opus packet starts ([#89](https://github.com/OneLiteFeatherNET/Sturnus/issues/89)) ([1712941](https://github.com/OneLiteFeatherNET/Sturnus/commit/1712941c6cc0f90912472451fbf7858863b645e6))

## [0.10.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.9.0...v0.10.0) (2026-08-22)


### Features

* **bot:** measure a live capture, to find where the noise comes from ([#87](https://github.com/OneLiteFeatherNET/Sturnus/issues/87)) ([0e574dc](https://github.com/OneLiteFeatherNET/Sturnus/commit/0e574dcb766ccfc8785924a865ba88dfcbf78a08))


### Bug Fixes

* **console:** make the recording page reachable, and stop logging hangups ([#86](https://github.com/OneLiteFeatherNET/Sturnus/issues/86)) ([1660a15](https://github.com/OneLiteFeatherNET/Sturnus/commit/1660a15ec9173c973f811e886642418f593be1ec))

## [0.9.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.8.1...v0.9.0) (2026-08-22)


### Features

* **console:** a canonical page per recording, with spectrograms ([#80](https://github.com/OneLiteFeatherNET/Sturnus/issues/80)) ([4334cca](https://github.com/OneLiteFeatherNET/Sturnus/commit/4334cca8e634e9b0dbb11439de429e2ae38432db))
* **console:** let an administrator re-run a transcription, and watch it ([#83](https://github.com/OneLiteFeatherNET/Sturnus/issues/83)) ([86f4c73](https://github.com/OneLiteFeatherNET/Sturnus/commit/86f4c737a434553e4336aa0aba5585020d619f0a))
* **scripts:** let an operator listen to a slice of a recording ([#59](https://github.com/OneLiteFeatherNET/Sturnus/issues/59)) ([7515520](https://github.com/OneLiteFeatherNET/Sturnus/commit/7515520a184f2db437122fb72f9ad7c20b8dfb86))

## [0.8.1](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.8.0...v0.8.1) (2026-08-22)


### Bug Fixes

* **console:** serve recordings in the format the bot actually writes ([#77](https://github.com/OneLiteFeatherNET/Sturnus/issues/77)) ([eb95cf2](https://github.com/OneLiteFeatherNET/Sturnus/commit/eb95cf2901b8a572d4d0fbfec17037e4fb4d17b0))

## [0.8.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.7.0...v0.8.0) (2026-08-22)


### Features

* **chart:** deploy the console and its API ([#73](https://github.com/OneLiteFeatherNET/Sturnus/issues/73)) ([7a8e197](https://github.com/OneLiteFeatherNET/Sturnus/commit/7a8e1973eeba367ce1fcdf3ac9f02a33a18e9c80))
* **console:** read and write the bot's runtime settings from the console ([#67](https://github.com/OneLiteFeatherNET/Sturnus/issues/67)) ([fb91dd2](https://github.com/OneLiteFeatherNET/Sturnus/commit/fb91dd2df5ffcb296f04136e8bb9f134f774ef03))
* **console:** read endpoints for the dashboard, sessions and calendar ([#65](https://github.com/OneLiteFeatherNET/Sturnus/issues/65)) ([0d9dd0b](https://github.com/OneLiteFeatherNET/Sturnus/commit/0d9dd0bb1a1077b0cbab716d5bf611295011cedd))
* **console:** stream a session's audio, decrypting on the way out ([#66](https://github.com/OneLiteFeatherNET/Sturnus/issues/66)) ([cd005eb](https://github.com/OneLiteFeatherNET/Sturnus/commit/cd005eb3b68e62bf61eaae3ee785a2f5f55e3969))
* **console:** the API process, its session, and signing in ([#62](https://github.com/OneLiteFeatherNET/Sturnus/issues/62)) ([32ff035](https://github.com/OneLiteFeatherNET/Sturnus/commit/32ff035c451f1cb68983fdf9123d5dfa10312127))
* **console:** the calendar heatmap and day timeline ([#70](https://github.com/OneLiteFeatherNET/Sturnus/issues/70)) ([d5ebeb9](https://github.com/OneLiteFeatherNET/Sturnus/commit/d5ebeb9c508d8d8da93782d7545be37789fe6941))
* **console:** the dashboard ([#68](https://github.com/OneLiteFeatherNET/Sturnus/issues/68)) ([a6f9188](https://github.com/OneLiteFeatherNET/Sturnus/commit/a6f91881c3999ec449aae3554e1e497622af223c))
* **console:** the Nuxt application, its layout and its front door ([#63](https://github.com/OneLiteFeatherNET/Sturnus/issues/63)) ([b1e3b65](https://github.com/OneLiteFeatherNET/Sturnus/commit/b1e3b651fd83e9a53a9165b690ff88145e280ccc))
* **console:** the recordings page and its multi-track player ([#69](https://github.com/OneLiteFeatherNET/Sturnus/issues/69)) ([399f163](https://github.com/OneLiteFeatherNET/Sturnus/commit/399f163bca7c425b0b42614750bf48488c339d97))
* **console:** the settings page, and what it says about taking effect ([#71](https://github.com/OneLiteFeatherNET/Sturnus/issues/71)) ([55461ef](https://github.com/OneLiteFeatherNET/Sturnus/commit/55461ef41982932b9d275e30d47d1252749e33f8))
* **db:** persist what a job measured, and mirror who administers the bot ([#61](https://github.com/OneLiteFeatherNET/Sturnus/issues/61)) ([3ff2c01](https://github.com/OneLiteFeatherNET/Sturnus/commit/3ff2c0135c63600b1d290785172ee1ec3b22e235))


### Bug Fixes

* **console:** show a page when a page cannot be rendered ([#75](https://github.com/OneLiteFeatherNET/Sturnus/issues/75)) ([210ffe4](https://github.com/OneLiteFeatherNET/Sturnus/commit/210ffe4f9fccdcd8f91e9b0ffa7db51d2bb973d8))

## [0.7.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.6.0...v0.7.0) (2026-08-21)


### Features

* **publishing:** mention every recorded speaker when the link is posted ([#53](https://github.com/OneLiteFeatherNET/Sturnus/issues/53)) ([ee4019f](https://github.com/OneLiteFeatherNET/Sturnus/commit/ee4019f619b07ef9e3f49d6d584e032aa3aac1e8))


### Bug Fixes

* **chart:** let every component finish starting before liveness judges it ([#52](https://github.com/OneLiteFeatherNET/Sturnus/issues/52)) ([f8776c5](https://github.com/OneLiteFeatherNET/Sturnus/commit/f8776c5da9a9ab880904722986efee1990d88918))

## [0.6.0](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.5.1...v0.6.0) (2026-08-21)


### Features

* **bot:** say so, during the meeting, when a speaker's audio has no level ([#43](https://github.com/OneLiteFeatherNET/Sturnus/issues/43)) ([0ceb496](https://github.com/OneLiteFeatherNET/Sturnus/commit/0ceb4969f4174237e04d0589bc180511eccd68f9))
* **discord:** add /queue and let an admin re-run a finished session ([#49](https://github.com/OneLiteFeatherNET/Sturnus/issues/49)) ([e2e8c93](https://github.com/OneLiteFeatherNET/Sturnus/commit/e2e8c9334e9becb389d766b2394023e42f277c86))
* **observability:** trace, measure and log Sturnus without shipping content ([#50](https://github.com/OneLiteFeatherNET/Sturnus/issues/50)) ([1ff8a9d](https://github.com/OneLiteFeatherNET/Sturnus/commit/1ff8a9df415ff203ccb3c9ae1470bea1eeb70aea))


### Bug Fixes

* **worker:** transcribe the speech, not the padded track ([#48](https://github.com/OneLiteFeatherNET/Sturnus/issues/48)) ([d3e773a](https://github.com/OneLiteFeatherNET/Sturnus/commit/d3e773a79b16cf0c3f2bad695d822cd0461a1553))

## [0.5.1](https://github.com/OneLiteFeatherNET/Sturnus/compare/v0.5.0...v0.5.1) (2026-08-21)


### Bug Fixes

* **worker:** close the veto that switched off Whisper's own silence check ([#46](https://github.com/OneLiteFeatherNET/Sturnus/issues/46)) ([455b200](https://github.com/OneLiteFeatherNET/Sturnus/commit/455b2008baf452713a1278448580bdffdfb35cd2))

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
