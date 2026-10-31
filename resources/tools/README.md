# `resources/tools/` — the prediction tools

In a fresh clone this directory contains only documentation. The eleven
prediction programs VEDA drives are **not** in the repository; the installer
puts them here. This page explains why, and what you need to do.

```
git clone <this repo>
cd veda
./install.sh          # fetches what it can, tells you what it cannot
```

## Why the tools are not in the repository

Two reasons, either of which is sufficient:

**Licence.** Five of the tools come from DTU Health Tech under an academic
licence issued to a *named individual at an academic institution*. That licence
does not permit passing the program to third parties, so they cannot ship
inside a public repository — doing so would hand them to everyone who can
clone it. This is the same approach other open-source projects in the field
take with these tools.

**Permanence.** The tools total about 1.7 GB, and git history cannot be
rewritten without invalidating every clone and fork. Large third-party binaries
committed once are committed forever.

## The three tiers

| Tier | What | Size | How you get it |
|---|---|---|---|
| 1 | VEDA itself (`src/`, `installer/`) | ~2 MB | the clone you just made |
| 2 | Six open-source tools | ~1.2 GB | **the installer fetches them** from the authors' own repositories |
| 3 | Five DTU Health Tech tools | ~425 MB | **you download them**, free for academic use — see [DTU_TOOLS.md](DTU_TOOLS.md) |

VEDA runs without tier 3. The pipeline steps that need those tools report
themselves unavailable and are greyed out in the interface; everything else
works. Add them later and re-run `./install.sh --only tools` whenever you like.

## Tier 2 — fetched automatically

The installer fetches these for you. Nothing to do.

| Tool | Purpose | Fetched from | Licence |
|---|---|---|---|
| AlgPred 2.0 | allergenicity | clone of [raghavagps/algpred2](https://github.com/raghavagps/algpred2) | GPL-3.0 |
| ToxinPred 2.0 | toxicity | [standalone package](https://webs.iiitd.edu.in/raghava/toxinpred2/stand.html) — see below | GPL-3.0 |
| IL2Pred | IL-2 inducers | clone of [raghavagps/il2pred](https://github.com/raghavagps/il2pred) | see repository |
| IL4Pred 2.0 | IL-4 inducers | clone of [raghavagps/il4pred2](https://github.com/raghavagps/il4pred2) | GPL-3.0 |
| IFNEpitope 2.0 | IFN-γ inducers | clone of [raghavagps/ifnepitope2](https://github.com/raghavagps/ifnepitope2) | see repository |
| Vaxign-ML | protegenicity | clone of [VIOLINet/Vaxign-ML-docker](https://github.com/VIOLINet/Vaxign-ML-docker) | GPL-3.0 |

Two of these needed more than a plain `git clone`, found by installing into an
empty directory and watching what broke. Recorded here because the symptoms are
confusing if you meet them yourself:

- **ToxinPred 2.0 is not fully on GitHub.** Its `RF_model` — 204 MB, the actual
  classifier — is not in the repository at all. A clone gives you a tool that
  imports and prints help and then cannot predict. The installer downloads the
  author's standalone `.tar.gz` instead, which does contain it.
- **IFNEpitope 2.0's repository has git-lfs pointers but no `.gitattributes`.**
  Git therefore has no filter to expand them, so `git lfs pull` exits 0, prints
  nothing, and leaves 13 files — including the main script and `model.zip` — as
  130-byte stubs. The objects download fine; only the checkout step fails. The
  installer fetches them and resolves each pointer itself, verifying every
  object's SHA-256 against the hash recorded in the pointer before using it.

Vaxign-ML additionally needs a Docker image, which the installer pulls.

## Tier 3 — you supply these

| Tool | Purpose | Expected directory |
|---|---|---|
| NetMHCpan 4.2 | MHC-I binding | `netMHCpan-4.2/` |
| NetMHCIIpan 4.3 | MHC-II binding | `netMHCIIpan-4.3/` |
| GibbsCluster 2.0 | motif clustering | `gibbscluster-2.0/` |
| Seq2Logo 2.2 | sequence logos | `seq2logo-2.2/` |
| DeepLoc 2.1 | subcellular localisation | `deeploc-2.1.All/` |

Full step-by-step instructions, including which file to request for each and
how to check it landed correctly: **[DTU_TOOLS.md](DTU_TOOLS.md)**.

## What the installer does to the tools

Beyond downloading, the `tools` and `configure` stages repair things that stop
these programs working out of the box. Worth knowing, because it explains why
you should let the installer do this rather than unpacking by hand:

- rewrites the hardcoded installation paths inside the `tcsh` wrappers and the
  `envfile`s (every one of these tools ships with the author's own paths baked
  in), keeping a `.veda-orig` backup of each file it edits;
- marks the bundled binaries executable, which `tar` does not always preserve;
- strips macOS `._*` and `.DS_Store` files, which otherwise confuse the
  tools' own directory scans;
- builds the BLAST database AlgPred and ToxinPred need;
- unpacks the model archives that ship zipped;
- checks that each program's **data payload** arrived, not just its launcher
  script. NetMHCpan and NetMHCIIpan keep their matrices in a separate `data/`
  tree, and without it they print one error line, **exit 0**, and produce no
  predictions — which looks like success to anything that only checks the exit
  code.

## Checking what you have

```bash
veda doctor              # every tool, run against a real input
veda doctor --system     # the machine: GPU, Anaconda, package names, paths
```

`veda doctor` runs each tool on a small real input and reports what came back,
so a tool that is present but broken is reported as broken rather than
present. Tools you have not installed are reported as *unavailable*, not
failed.

## A note on scratch paths

The DTU programs hold their scratch directory path in a fixed 64-byte buffer.
VEDA points them at `$VEDA_HOME/tmp`, so if `VEDA_HOME` is deep enough that
the path exceeds 63 characters, they abort with
`*** buffer overflow detected ***`. The installer refuses to proceed in that
case and tells you to choose a shorter location. The default, `~/.veda`, is
15 characters.

## If you are packaging VEDA for a group

`veda-pack` builds per-tool archives and a checksum manifest for a local
mirror, so colleagues install without re-downloading everything:

```bash
./veda-pack --free-only     # tier 2 only: safe to host publicly
./veda-pack                 # everything, including tier 3
```

A bundle built without `--free-only` contains DTU-licensed programs. Host it
only where access is controlled and limited to people who hold their own DTU
licence — never a public release, a public web server, or a public data
repository such as Zenodo. `veda-pack` warns you when a bundle contains them.
