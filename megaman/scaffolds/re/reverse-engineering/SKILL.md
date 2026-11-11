---
name: reverse-engineering
description: >-
  Workflow for reverse-engineering a shipped artifact you own — a binary, an
  iOS .ipa, an Android .apk, a desktop or Electron app, firmware, a game ROM —
  with the REA harness as the tool layer and a verification loop that proves
  the result instead of asserting it. Use this skill whenever the work is
  "understand/recover/port something we only have as a build": decompilation,
  symbol and type recovery, protocol or format recovery, behaviour comparison
  between versions, or rebuilding a feature from evidence. It defines what
  counts as evidence, how to drive REA, the legacy-iOS/armv6 details REA's own
  guidance doesn't carry, and the differential-testing loop that turns "this
  decompiles" into "this is provably the same program".
---

# Reverse engineering (REA harness + verified decompilation)

Two halves. **REA** is the tool layer: one MCP server and CLI over Ghidra,
Frida, JADX, static JavaScript/Electron analysis, Mach-O/PE/ELF inventory and
runtime observation, with Evidence provenance built into every answer. The
**verification loop** is this project's own discipline, distilled from a
prior legacy-iOS decompilation project (1,395 functions, 99.6% proven bit-identical): a
decompilation nobody checked is a guess with syntax highlighting.

## 0. Scope line — read once, then stop re-litigating it

Work only on artifacts the owner owns or is authorised to analyse, locally. This
skill is for understanding, porting, interoperability, security review and
preservation. It is **not** for stripping DRM from something to redistribute,
for shipping someone else's assets, or for defeating a protection in order to
pirate it. If a task crosses that line, say so in one sentence and offer the
part that doesn't.

## 1. Connect REA, then get out of the way

REA is registered for this project (local MCP scope — `rea` server). On a
fresh session:

1. If REA tools are present in the session, **use them directly**. Do not run
   doctor/setup before every investigation.
2. If they're missing: `rea doctor --client claude_code --skill --json`, read
   the blocker, fix only that, and resume. Registration lives in
   `~/.claude.json` under this project's entry.
3. `rea --help` is the same surface from the terminal — use it for batch work
   and in jobs (`/run`), where MCP isn't attached.

Providers on this laptop, as installed 2026-10-20:

| provider | state |
|---|---|
| Ghidra 12.1.4 | `~/tools/ghidra-12.1.4/ghidra_12.1.4_PUBLIC`, passed to REA as `GHIDRA_INSTALL_DIR` |
| Java | JDK 21+ passed as `JAVA_HOME` (REA refuses 17; `/opt/jdk-17` may belong to another project, leave it alone) |
| Ghidra 11.3.2 at `/opt/ghidra` | a patched decompiler another project may rely on — **do not upgrade or repoint it** |
| Hopper / IDA / Kuna | not installed; REA degrades to Ghidra, say so rather than inventing results |
| JADX (Android) | bring-your-own JAR, `REA_JADX_MCP_JAR` — set it before Android work |

## 2. Evidence discipline (non-negotiable)

Every claim lands in one of four buckets, and the bucket is stated:

- **Observed** — the tool produced it (bytes, a trace, a test result).
- **Derived** — computed from an observation by a stated rule.
- **Inferred** — a hypothesis that fits the evidence. Say "inferred".
- **Unknown** — say unknown. Do not round it up to inferred.

Keep REA's provenance: when a conclusion rests on an Evidence reference, cite
it (file + address/offset + the call that produced it). "Ghidra says" is not a
citation; `__text:0x0001a2c4, decompiled, with the switch fixup applied` is.

## 3. The workflow

```
triage → inventory → static model → dynamic ground truth → recompile → differential test → ledger
```

**Triage.** What is it? `file`, architecture, bitness, endianness, packer,
encryption (`LC_ENCRYPTION_INFO cryptid` on Mach-O; a non-zero cryptid means
the code is FairPlay-encrypted and static analysis stops until you have a
decrypted copy you're entitled to). Note protections and whether it shipped
stripped. REA's inventory/projection tools answer most of this in one call.

**Inventory.** Enumerate everything before going deep on anything: segments
and sections, symbol tables, debug records, resources, embedded archives,
linked libraries, entitlements/manifest. Write it to `analysis/` as files, not
chat. Chat is lost; the file is the project's memory.

**Static model.** Decompile, type, name. The decompiler's output is a *model*
of the program: calling conventions, switch tables and types in it are
inferences until something checks them.

**Dynamic ground truth.** Run the original — emulator, device, Frida,
debugger — and capture what the static side can only guess: which class
receives each dynamic dispatch, where indirect calls land, which "calls" never
return, real input/output pairs, and saved states you can re-enter. Feed those
facts back into the static model. This is the single highest-value move in the
whole workflow and the one most often skipped.

**Recompile + differential test.** See §5. This is what turns a decompilation
into a result.

**Ledger.** One row per function/unit: verdict, evidence, coverage. The ledger
is the deliverable; the C is just what it's about.

## 4. Legacy iOS / 32-bit ARM — details REA's guidance doesn't carry

From a prior legacy-iOS reverse-engineering project (armv6 Mach-O, iOS 3.x, Objective-C 2.0 + C++). These
cost days each when missed:

1. **ARM vs Thumb mode is in the symbol's `n_desc`** (`N_ARM_THUMB_DEF
   = 0x0008`), not in the address's low bit. Seed the mode per function
   *before* auto-analysis. Get it wrong and the decompiler reads Thumb
   halfwords as ARM words and emits convincing nonsense.
2. **STABS, when present, are the original build tree.** An unstripped 2009-era
   binary carries `N_SO`/`N_FUN`/`N_SLINE`/`N_LSYM` records: every function's
   name, address and source file. That reconstructs the original source layout
   before you decompile a line.
3. **Objective-C metadata is type information.** `__objc_*` holds classes,
   ivars with offsets, and method lists with runtime type encodings. Turn them
   into real structs and type every method's `self`. Ghidra won't build ivar
   structs for you.
4. **`objc_msgSend` call sites need a receiver class to be typed at all.** The
   selector is usually recoverable statically; the receiver often isn't. Take
   it from the runtime capture. Thousands of call sites hang on this.
5. **Long `bl` branches are not calls.** Thumb veneers and `bl` to a non-return
   helper will split or corrupt functions. Re-join functions split at long
   branches; mark non-returning helpers.
6. **Switch helpers (`___switch8/u8/16`) need a call-fixup**, not a jump-table
   annotation, or the decompiler loses the whole construct.
7. **Soft-float helpers are operators.** VFP/soft-float library calls must be
   turned back into float arithmetic, and float `+`/`*` are **not**
   associative — a decompiler that reassociates them prints C whose results
   differ from the binary. Both Ghidra floating-point patches in that project
   were found by differential testing, not by reading.
8. **Apple's armv6 ABI** aligns 64-bit values to 4 bytes, not 8. Stack-struct
   locals (`objc_super`, fast-enumeration state) must be modelled as structs.
9. **Integer division semantics are the platform's**, not C's: measure
   `x / 0` and signed edge cases on the real target and shim them.
10. **Version drift silently invalidates everything.** Dynamic captures from
    v2.1.0 shared **zero** function addresses with v2.3.0 static analysis.
    Pin one build, hash it, and re-do captures when it changes.

Worked reference (read before re-deriving any of this):
`analysis/claude_notes/README.md (in the original target project)` (how it was
done) and `TOOLS.md` (what each tool is for). The reusable parsers ship in
this project's `tools/` — see `tools/README.md`.

## 5. The verification loop — "decompiled" is not a result

A function counts as recovered when the machine agrees, not when it reads
well.

1. **Export compilable C per function**, from the decompiler's token tree
   rather than its text, so the exact call target, bit-reinterpretations
   (`__BITCAST`) and per-site prototypes survive.
2. **Recompile each function alone** against the original binary, with the
   original target flags. Log every mechanical transform you had to apply
   (stack overlays, 64-bit splits, soft-float shims) — each one is a claim.
3. **Differential-test from real captured states.** Enter the same state in
   the original and in the recompiled unit, run, compare registers, memory and
   side effects. Same state in, same state out, or it's a defect.
4. **Record a verdict per function**: proven / audited-by-hand / untestable
   (and why). Percentages come from the ledger file, never from memory.
5. **Check the checker.** A harness that can't fail is worthless:
   - *coverage* — is all real code actually in the decompilation? (Account for
     literal pools, switch tables and padding as data, not misses.)
   - *reproducibility* — rebuild the whole analysis project from scratch and
     diff. Script-ordering bugs only show up here.
   - *mutation* — inject deliberate defects; the harness must catch them.
   - *independent build* — a port or second implementation built from the
     recovered code is the strongest confirmation available.

## 6. Reporting

End any reply that finishes work with:
1. a **% complete** figure taken from the ledger/status file, with the
   denominator stated (functions? instructions? screens?), and
2. a **numbered next-steps list**, each with its expected payoff.

Never report a number you didn't regenerate. "About 80%" from memory is how
a project lies to itself for a month.

## 7. Memory and long jobs

Ghidra, emulators and full-project analyses are the heaviest things this
laptop runs:

- `claude-memcheck` **before** starting a headless analysis, and again while
  it runs. Cap Ghidra's heap explicitly (`analyzeHeadless -max-cpu 2` and
  `MAXMEM` in `analyzeHeadless`/`ghidraRun`); unbounded, it will take the
  session's whole budget.
- One heavy thing at a time: an emulator *or* a decompile pass, not both.
- If the job genuinely needs more than the cap:
  `claude-memcheck --request 12G --reason "Ghidra full analysis, 47 MB binary"`.
  the owner gets it on Discord and a grant applies live. Keep working under the
  current cap while you wait; don't launch the big thing hoping.
- Long analyses belong in a job (`/run`), not the interactive session, so a
  dropped connection doesn't lose them.
- Anything needing root (installing a provider, mounting an image, attaching a
  debugger) goes in `tools/setup.sh` and gets approved once with
  `/sudo action:approve project:<name> script:tools/setup.sh`.

## 8. Project layout

```
target/      the artifact under analysis (+ its sha256 in LOG.md — pin the build)
analysis/    everything the tools produce: inventory, symbols, types, traces, ledger
recovered/   recovered source/assets/data
tools/       project scripts; setup.sh for anything that needs root
```

Drop the artifact in `target/`, hash it, record the hash in `LOG.md`, and work
from there.
