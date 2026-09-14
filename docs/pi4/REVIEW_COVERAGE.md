# Complete source-review coverage

Reviewed 2026-09-14 at commit `4c64f3305a611462c01faa3ad4233f40042f3dcc` on `pi4-stage-pro`.

74/74 tracked baseline files accounted for: 73 text files read in full (30,720 newline-delimited lines), plus the logo visually inspected. This is human-style multi-agent static review coverage, not measured test/branch coverage or a guarantee that all defects were found. Four reviewers divided the files below and cross-checked subsystem boundaries.

The new audit reports and reproduction fixtures are not part of that baseline count. Third-party Python/system packages, generated Faust C, deployed ARM binaries, soundfont contents, OS/firmware and private runtime files are not claimed to have received exhaustive source review. Their build/deployment and API boundaries were examined; binary provenance and target behavior remain qualification work. Previous live observations are labeled separately in the report. No stress test or new runtime instance was run on the Pi in this complete pass.

Reviewer key: Core = main/MIDI/JACK/recorder/persistence; Audio = synth/piano/organ/Faust; UI = browser/WebSocket/presets; Boot/tests = installation/services/all tests/tools/docs/licenses. AGENTS and Pi4 requirements were also cross-read by multiple reviewers.

| Baseline file | Lines / asset | Primary reviewer | Coverage | SHA-256 |
| --- | ---: | --- | --- | --- |
| `.gitignore` | 22 | Boot/tests | Read in full | `40004be2cfa92e00a750d9c99f2327e9cc433c8cde0aa9234b16fa323befe28a` |
| `AGENTS.md` | 13 | Boot/tests | Read in full | `6c27c5bcfdf63d41f44ddc78bdfd644626534916aeeafd6899a2b0166a4f3a62` |
| `CLAUDE.md` | 53 | Boot/tests | Read in full | `8a6dcf8a2f310c90ceabcaa1b1fc2ec60e3daef2fa69a1c8f0dd641b5c181bcb` |
| `FAUST_PORT_PLAN.md` | 640 | Boot/tests | Read in full | `eeb4b7433f7c8cd320d53a7c8cfda14dbe192e838b224b750476e9825f8d404f` |
| `LICENSE` | 21 | Boot/tests | Read in full | `f0f073bff95fac540031237239548a4453195c95ad1ea3b98f76192fb606fb43` |
| `README.md` | 273 | Boot/tests | Read in full | `c851afc0c8c7d6f7b899574931648c756ab4650e9fff19935ada3be7f26323b3` |
| `docs/pi4/BASELINE.md` | 46 | Boot/tests | Read in full | `55cfe9cacc247b2a9e7a15089a5b968d3e1a82b76a6e28e182f8eb5ed6858cfb` |
| `docs/pi4/ENGINEERING_VALIDATION.md` | 79 | Boot/tests | Read in full | `fe22cefd0a7350f6f5db4dcb04b4e699c3a4f048059825bb039b1777859c989d` |
| `docs/pi4/PRODUCT_VISION.md` | 110 | Boot/tests | Read in full | `aa23ec673070e2ad1045c34ae6e190540f6b8196f8e9c84415a62defc81b62b2` |
| `faust/build.sh` | 133 | Audio | Read in full | `e2e26ffc99e60f1a690d789b0c2333baf49abe9fabc366d8e6635095440b52f2` |
| `faust/bus_comp.dsp` | 83 | Audio | Read in full | `953c621f461319ab8a3c2f193bb86b786b0d27abae478b15ef4c7d65f64a7730` |
| `faust/drone.dsp` | 142 | Audio | Read in full | `22049c59049b54cdb3ba20adfd79d4c39a3aa0d5d28a3a202c31e1055dc14510` |
| `faust/faust_cprelude.h` | 15 | Audio | Read in full | `6293ebf5f64d3257adc43cc1defe18b417376185d51def2edec5ca8c28e8f0ce` |
| `faust/master_fx.dsp` | 79 | Audio | Read in full | `6b3896096694260eb6b99dc0d6ad24b684f56a26d1e6be1b4de20d10ac69d69a` |
| `faust/merged_shim.c` | 53 | Audio | Read in full | `66ceb1a06f12cb26b94ffadeaf46d1180e92dc24a5a8db803687b4197d791eb2` |
| `faust/organ.dsp` | 160 | Audio | Read in full | `d86bfa7dcd8371fa88ff06d985eb12f2b4f9ef182b16ab66c6e627af7ba729a8` |
| `faust/osc_bank.dsp` | 299 | Audio | Read in full | `e659f679bd373fb0f9a40135e068793f04516cba525024d03d81efed30ee89a0` |
| `faust/pad_bus.dsp` | 492 | Audio | Read in full | `f910d71810e1f7bf1b5e0b93d1347e9bf1c63f4471e6cf2eeb024851185a86b5` |
| `faust/piano_chain.dsp` | 202 | Audio | Read in full | `230bac09215257481840fb50323f15f7b7ac9837130662acc7f37f8142e9ec3e` |
| `faust/piano_room.dsp` | 46 | Audio | Read in full | `8974a59916f59a95ac330d27d62ea8ad3072516077a71da39e79c09c5ef818d1` |
| `faust/ping_pong.dsp` | 170 | Audio | Read in full | `e5a33486ba4810e41c293e101286fb8655c95eb06a97decc474aa4b405a71e99` |
| `faust/plate.dsp` | 66 | Audio | Read in full | `f8b25fd15d36360efb58d76be16b20b2bcf42f6283b3ec7d9526ee8aff2cccb4` |
| `faust/reverb.dsp` | 186 | Audio | Read in full | `2451753d6c821e6e3782812177ba164eaada4ff6d63fbcb57bf77bdb8feefa77` |
| `faust/sympathetic.dsp` | 52 | Audio | Read in full | `b681d0af463e249bea92038f3b5f7a9e37cb2e3f09c980658d55f2428165b97b` |
| `install.sh` | 605 | Boot/tests | Read in full | `0e041ba3abdd92614b26f0c1e618b2b9ba7c599c27c7800adece4cd9f5878f3b` |
| `requirements-gui.txt` | 4 | Boot/tests | Read in full | `4bc4603d54914e51aa8adee60f9df3f29efab6f9f24b48dde42159898ff7a5b0` |
| `requirements.txt` | 6 | Boot/tests | Read in full | `d0956619f0a007aa1ebf5038317243a8dd6020f853bc8f1c6f10ba41e5230576` |
| `setup.py` | 32 | Boot/tests | Read in full | `cda1c00414dedd7c6ca385b304d2dacd84b4ff7b8e0ee7d067231d61fea41a88` |
| `soundfonts/SALAMANDER-LICENSE.txt` | 75 | Boot/tests | Read in full | `ed3a0ccd16573a8e72966bdfb14bf4b3e18fab94bb9e36da1aa6409522d359f8` |
| `stave-synth.sh` | 40 | Boot/tests | Read in full | `972deb6134a099e2fb5ae1362a3e50dd1848ce9ea116177dc00800a32620b24d` |
| `stave_synth/__init__.py` | 2 | Core | Read in full | `aa057dc9e3a0741dc65049a3a26086ed31c944dc341bd6a95c7035bd8111e008` |
| `stave_synth/config.py` | 543 | Core | Read in full | `20dd23b30c4146003f038c798ff531dc8c707dedcf325bce6f99223cee7b9daa` |
| `stave_synth/faust_bus_comp.py` | 176 | Audio | Read in full | `514819a0bfc567bcc71c17246546f4d8ad1d7b6ad18d8480e1d040a580f4e698` |
| `stave_synth/faust_drone.py` | 173 | Audio | Read in full | `9018c5732d572b32c282f8e25c4b1e4af9015df52d1528d9effb7fe08872b2eb` |
| `stave_synth/faust_master_fx.py` | 171 | Audio | Read in full | `54cf726bbdc8e92f7c099840367f3205e2931dcb832d322fb735940f8436fc2e` |
| `stave_synth/faust_merged.py` | 116 | Audio | Read in full | `742d2e122d01babbd223a40b73c1203cd7b8953346d3937a48866bce2d59ebb1` |
| `stave_synth/faust_organ.py` | 524 | Audio | Read in full | `02c94e0d7bad9be333813653ee0ee3ec2a776beef898330dd27e7ad5136f8a52` |
| `stave_synth/faust_osc_bank.py` | 338 | Audio | Read in full | `9cc3abc132884d0824007350dd4c6ae825de5feec2cef8e43c809b63364efdf2` |
| `stave_synth/faust_pad_bus.py` | 353 | Audio | Read in full | `6d4c800d9aa0fd447e6824f4cfde89c278895da3e6b67af32a99266680c7231f` |
| `stave_synth/faust_piano_chain.py` | 219 | Audio | Read in full | `46acebcb990635d46eb39c3744f69d4982bcacb03a07216d096b6ffbdb3ef814` |
| `stave_synth/faust_piano_room.py` | 176 | Audio | Read in full | `be5db65a3e37aa99a870073ac037948d121ade7d23a5be45448920f450263e32` |
| `stave_synth/faust_ping_pong.py` | 202 | Audio | Read in full | `f5998d22f1cd4c664db40b215e0a42674e5e610fea642d8e8a479ac16c52fb80` |
| `stave_synth/faust_plate.py` | 173 | Audio | Read in full | `b81e528f46c174c994cb922778d99cc09cdfe7f09227ed63ba958bf1d5eaca16` |
| `stave_synth/faust_reverb.py` | 593 | Audio | Read in full | `294ff776ee9bbc076c13e11cd444fb4feb3788a5d22eab6429a3da461b6db716` |
| `stave_synth/faust_sympathetic.py` | 187 | Audio | Read in full | `d11705fbf07c0e10766dae4c701dbfc04419e4d6f6220011eed2f6def949c7c9` |
| `stave_synth/fluidsynth_player.py` | 1082 | Audio | Read in full | `bb8ae306f3137018a2478084e8a25eeb66b45e2a7d491b7dae9dcda07a7eec20` |
| `stave_synth/jack_bridge.c` | 438 | Core | Read in full | `345fdc72ef9e3721bcc62d5d722bfba0f9592863850a9d3e63b58d45542a4b3d` |
| `stave_synth/jack_engine.py` | 1552 | Core | Read in full | `e30b1ea1dd0fc45754b8216bc448d6b8a868ac23b6b5203e9a0b639bf4400538` |
| `stave_synth/main.py` | 2336 | Core | Read in full | `89d6b0355ed0355c0af37638aac18d5f81182683f9d6314e689a8c0e47e9dbf8` |
| `stave_synth/midi_handler.py` | 31 | Core | Read in full | `f5d4fffd6cf14f31691f32ee7a080926bc4e833b7b2184f3f84cde0bbf95d5b4` |
| `stave_synth/organ_engine.py` | 546 | Audio | Read in full | `84dac998934aeb7b578a09d31e5c7ccb77671b3b504dec7fd1d5b27053b64fb2` |
| `stave_synth/preset_manager.py` | 102 | UI | Read in full | `341d896feb6c41dd9ed78c6363528081c182f0543aecbc32b0eb6d9ed31c5495` |
| `stave_synth/recorder.py` | 286 | Core | Read in full | `edc44b778957d387a6414d9e8f9605ecd30ca2f6e46f210c1fba11ec3ccbf4a0` |
| `stave_synth/synth_engine.py` | 4261 | Audio | Read in full | `5d70b0b97b15a3578c908eb58b08b8ed7f5f73ee2eec506c9379d288e9e192ec` |
| `stave_synth/websocket_server.py` | 183 | UI | Read in full | `1cc25b73a580c0a64ea7a15857bacaeab3b254e8dcabbc18fd98ba9b454fb120` |
| `systemd/stave-synth.service` | 48 | Boot/tests | Read in full | `6cc5c462940eff46c21a6f9fee774910c6681e919a3cd36e7f4dd789f5521c6c` |
| `systemd/stave-synth.service.d/faust.conf` | 12 | Boot/tests | Read in full | `aeb95e6fe71e06fc15755a9661fcdc229d8e893ccbe9b79493d69e9ea639d606` |
| `tests/_midi_util.py` | 44 | Boot/tests | Read in full | `571383176c64763febe7c115f2ef1a7c6c0fbf27b5345eb02fdf5b9206ff5270` |
| `tests/_save_worker.py` | 31 | Boot/tests | Read in full | `8b28db57e274103f417aad7e431ae8bf70fe64adea4ae07fa6da01e5a867ad42` |
| `tests/test_1_atomic_save.py` | 97 | Boot/tests | Read in full | `7b0c81dbf4917a5630ee98d18475df711a42bb8d8361a48970738bb4f0ddb34e` |
| `tests/test_2_param_flood.py` | 171 | Boot/tests | Read in full | `e85596be1288a71bd370f04a29ba779cfb241617c42fc979122e101ec00bf743` |
| `tests/test_3_connection_chaos.py` | 202 | Boot/tests | Read in full | `7752b71f9e4442fdb0d6eb371ad3e388d55a6d1ee6f658162091b43b43ffca19` |
| `tests/test_4_boundary_sweep.py` | 342 | Boot/tests | Read in full | `ae8cf8865b85c6593efcfaa9d8d277dfa4231ec4f9a2f8166940e6feccd87fa9` |
| `tools/bench_ping_pong.py` | 80 | Boot/tests | Read in full | `e3e1d8d5ab2876803cb79e4404bd2021bc4b46f542c3765a49a863225b92b259` |
| `tools/bench_reverb.py` | 51 | Boot/tests | Read in full | `e1b728b762427fba5002f6fe8f14bacdb29dcd6fda9a7564e2cfb69c445244f9` |
| `tools/compare_pad_bus.py` | 349 | Boot/tests | Read in full | `9cd6241057bcc9afb9219037a54858784ef500616bbf098d34a7423b458b9ba2` |
| `tools/compare_piano_chain.py` | 301 | Boot/tests | Read in full | `e67b349238829beb8045a2be27f163b31c23f8c8c936cc590a243d1ddbb908f9` |
| `tools/polish_shimmer.py` | 78 | Boot/tests | Read in full | `a19136144f5a29c3d2a774e131978c0e8790e67dc8553aee6255056da27ecea4` |
| `tools/render_compare.py` | 164 | Boot/tests | Read in full | `a662fe8ce223c51168a601bde99ba553833903c6d6816e5afd50c60781b49766` |
| `ui/index.html` | 1714 | UI | Read in full | `fb1bfccc7bb637956bc7031fc98a30c64c5abc0b111bec4f981634a3042515a0` |
| `ui/logo.png` | 510×510 image | UI | Visually inspected | `2f6bf7fcf9188a5f2bb1baf5b771f0b3c0f4e0c706debed64c1348e684a9561f` |
| `ui/manifest.json` | 13 | UI | Read in full | `c9c95cb94bf98f54deb69f4cb7d92a791f70c2a6d5e3a3056157b5a32487927e` |
| `ui/script.js` | 4693 | UI | Read in full | `239699705910d32a3c2376334f989d7f5c104d4366f436304bb08dfc3a3f5900` |
| `ui/style.css` | 3370 | UI | Read in full | `3edea222e66c2e32b12712abbb547e274bb137adc0e0bcd2a04b258066eeeff2` |

Full findings: [Complete review](COMPLETE_CODE_REVIEW.md). Hashes identify the actual content reviewed, not a later edited version.
