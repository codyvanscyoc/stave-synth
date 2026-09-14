# Source-audit evidence (2026-09-14)

These documents and fixtures describe the `4c64f33` baseline. **The probes deliberately assert existing failures. Exit 0 means the defect was reproduced, not that the instrument passed.** Later fixes should replace/invert those expectations in the project's regression suite.

The Python fixtures extract exact definitions with AST rather than importing the application. Native dependencies are mocks; data writes use disposable temporary directories. The bridge fixture compiles the original `jack_bridge.c` with mock JACK headers/functions and never opens JACK or network sockets. No script here launches the synth, reaches the Pi, uses live state, or changes routing. Local Python with NumPy and a C compiler are needed; nothing is installed automatically.

These are historical probes for the audited source, not the current regression
suite. Run them only in a disposable checkout of the source-audit commit that
contains these fixtures and the unchanged baseline application. Later fixes are
expected to invalidate their assertions. For current development use the
explicit offline regression commands in `docs/pi4/REPAIR_PLAN.md`; do not use
broad test discovery, which would include the old production stress scripts.

From that historical checkout root:

```sh
python3 docs/pi4/review-evidence/core_probes.py
python3 docs/pi4/review-evidence/audio_repro.py
```

Compile the native fixture into a fresh temporary directory (retain that path to inspect the executable):

```sh
stave_audit_build=$(mktemp -d /private/tmp/stave-native-audit.XXXXXX)
clang -std=gnu11 -O1 -Idocs/pi4/review-evidence/include docs/pi4/review-evidence/native_probe.c -o "$stave_audit_build/native_probe"
"$stave_audit_build/native_probe"
```

GNU11 clang reports a label/declaration extension warning from the unchanged bridge. These Mac mock tests do not establish ARM concurrency, deployed native binary equivalence, audio quality, measured latency or real hardware recovery.

Observed verification: core fixture exit0 with eight reported defect checks; native fixture exit0 with four; audio fixture exit0 with seven. The audio fixture's organ mock uses the actual 16-slot configuration. Its additional freeze-decay printout is informative, not a separate asserted acoustic test. Root reran all three durable fixtures successfully. All 37 tracked Python files parsed, JavaScript passed `node --check`, and the installer/launcher/Faust build scripts passed `bash -n`. Every coverage-ledger SHA-256 matched its tracked baseline file, and `git diff --exit-code` confirmed no tracked baseline edits.

Scope and findings: [complete review](../COMPLETE_CODE_REVIEW.md), [coverage](../REVIEW_COVERAGE.md), [core](core-report.md), [audio](audio-report.md), [UI](ui-report.md), [boot/tests](boot-test-report.md).

Do not substitute the existing production-oriented stress scripts for these isolated probes. See the qualification/isolation warning in [ENGINEERING_VALIDATION.md](../ENGINEERING_VALIDATION.md).
