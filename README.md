# BootWave: connecting initial BSP launches to SLM readiness gates

BootWave is a Windows/Python host tool that turns one explicit service configuration into a QNX 8.0 BSP fragment, an SLM configuration and a small readiness helper. An embedded developer can keep early driver launches and the applications that depend on them in one source, inspect the required target files, and replay the generated dependencies before acquiring a target environment.

Use Python 3.12, standard library only, from this directory:

```powershell
py -3.12 -B bootwave.py example.json --output example_output
py -3.12 -B verify.py
```

The generator never executes input service commands. It emits:
  bootwave.build       initial BSP launch fragment and positional SLM invocation
  bootwave.slm.xml     native SLM components, explicit dependencies and success gates
  bootwave-ready.sh    external POSIX shell helper for path readiness
  plan.json           required target files and virtual-time output replay

example.json is synthetic. Its /sbin and /usr/bin paths, demo applications, device readiness paths and times must be replaced with a reviewed board configuration. Declare the first useful application explicitly. ready_after_s is a model input, not a runtime delay instruction. launch_return_s models initial BSP command return time and the manual reference. timeout_s becomes the real readiness timeout. SLM starts its managed foreground processes through its own native machinery.

## Supported subset
- QNX 8.0 and explicit platform/per-service parallel-start permission.
- Absolute executable plus separate plain argument tokens. No arbitrary shell composition, raw commands, environment fields or modifiers; unsupported spellings are rejected rather than partially parsed.
- Independent initial BSP launches; gated dependency work belongs in the SLM phase.
- Screen: BSP self_daemon only, no ampersand. SLM programs must remain foreground; daemon behavior is a target integration responsibility.
- Paths use a separate helper. Explicit SLM exit-0 commands support one-shot setup such as if_up -p en0. Interface shorthand and an interface represented by /dev are rejected.

## Runtime contract
Start slogger2 in the existing reviewed BSP before this fragment. Install the files listed in plan.json and all required shared libraries. runtime declares target paths; it does not verify those paths exist. In the example, install bootwave-ready.sh at /etc/bootwave-ready.sh and XML at /etc/bootwave.slm.xml. The shell is invoked explicitly, so the helper need only be readable by it. No if statement is embedded into the mkifs script language.

The external waitfor utility returns nonzero on timeout. The helper preserves failure and rechecks path existence before returning zero. XML gates use wait="exits" with expected 0; a dependent requires successful completion of its gate. SLM repair/stop and retry behavior belongs to native SLM. Initial BSP processes are not adopted or monitored; their meaningful, fresh readiness paths must be unique to the intended service. A path is not an application-health or stale-resource detector. This version does not modify an arbitrary existing buildfile or provide general environment, privilege or security-policy conversion.

## Verification
verify.py checks generated file bytes against current source, reads emitted BSP and XML, verifies dependency/exit contracts, and replays virtual readiness/failure events. manual_reference.slm.xml is a separately written same-workload SLM reference with the same shell success gate. example.json supplies the well-arranged manual blocking order. All three produce synthetic total 1.52 s / first useful 1.14 s; no speed advantage over these references is claimed. The A/B/C timing-reversal case reaches C at 1.2 s / total 4 s, while the old estimate-ordered blocking order totals 5 s. Missing paths and failed commands block descendants in the replay. These checks do not execute SLM or validate mkifs grammar.

The next target milestone is to review the BSP boundary and file inventory, build the same artifacts with mkifs, run them under QNX 8.0 SLM, inject readiness and command failures, and measure the first useful application and total readiness against the manual and handwritten SLM references.

## Primary references

- <https://qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.utilities/topic/s/slm.html>
- <https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.building/topic/buildfiles/nto_script.html>
- <https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.utilities/topic/w/waitfor.html>
- <https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.fastboot/topic/cscreen_optimizing-startup_configure_buildscript.html>
