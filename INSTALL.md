# Installing VEDA

VEDA is an immuno-epitope analysis pipeline: a Streamlit application that
drives thirteen external prediction tools across four isolated Python
environments.

```bash
git clone <your-repo> VEDA
cd VEDA
./install.sh
```

That is the whole procedure on a fresh Ubuntu/Debian machine. The installer is
safe to re-run and resumes where it left off.

---

## What you need

| | |
|---|---|
| OS | Ubuntu 20.04+ / Debian 11+ (or a Debian derivative), **x86_64** |
| Disk | **~24 GB** free; 30 GB recommended |
| RAM | 8 GB minimum, 16 GB comfortable |
| Network | Outbound HTTPS to GitHub, conda-forge, PyPI, Docker Hub |
| Privileges | Your own user. `sudo` is asked for once, only for apt and Docker |
| GPU | Optional. An NVIDIA GPU speeds up DeepLoc; CPU works fine |

x86_64 is not negotiable: the NetMHCpan, NetMHCIIpan and GibbsCluster
binaries are distributed as Linux x86_64 only.

---

## What the installer does

Ten stages, each recorded under `~/.veda/state` so a re-run skips completed
work:

| Stage | Action |
|---|---|
| `preflight` | Checks OS, architecture, disk, memory, network, sudo. Read-only. |
| `probe` | Discovers what the machine already has, and adapts. Read-only. |
| `system` | apt installs `tcsh`, `perl`, `git-lfs`, `unzip`, build tools |
| `mamba` | Downloads a private micromamba into `~/.veda/bin` |
| `envs` | Builds the four pinned environments, then PyTorch and DeepLoc2 |
| `tools` | Downloads, stages and repairs the prediction tools |
| `docker` | Installs Docker Engine, pulls the Vaxign-ML and DeepTMHMM images |
| `configure` | Writes `veda_paths.json`, rewrites the tools' hardcoded paths |
| `launcher` | Creates the `veda` command and a desktop entry |
| `verify` | Runs every tool against a small real input and reports |

### The probe: what makes this work on someone else's machine

Run it on its own, before installing anything, on any Ubuntu box:

```bash
./veda-doctor --system          # no VEDA install needed
./install.sh --only probe       # the same thing, as a stage
```

It writes `~/.veda/system-report.txt` and changes what the later stages do.
It **installs nothing** and modifies nothing.

**Anaconda and the NVIDIA driver are never installed by VEDA.** Both are
system-wide, user-owned decisions — one needs a kernel module and a reboot,
the other is something you may already depend on. VEDA detects them, reports
them, and adapts.

| What it finds | What changes |
|---|---|
| Distro release | apt package names are resolved per release, not hardcoded. Ubuntu 24.04 renamed `libglib2.0-0` to `libglib2.0-0t64`, and on 26.04 the old name has no install candidate at all. The probe declares a *capability* and picks whichever candidate name this release actually provides. |
| An existing Anaconda/Miniconda/Miniforge | Left completely alone. VEDA always uses its own micromamba under `~/.veda/mamba`. Verified: setting `CONDARC` makes micromamba ignore your `~/.condarc`, so a `defaults` channel in it cannot affect VEDA's solves. |
| An **activated** conda environment | Its variables are scrubbed from every VEDA worker. See below. |
| GPU driver version | Picks the PyTorch wheel the driver can actually run: `>= 525` → CUDA 12.1, `450–524` → CUDA 11.8, older or absent → CPU. These are NVIDIA's documented family minimums. |
| NVIDIA hardware but no driver | Installs CPU PyTorch and prints the `ubuntu-drivers install` command. Install the driver, reboot, re-run `./install.sh --only envs`, and the CUDA build swaps in — nothing else is rebuilt. |
| GPU with little VRAM | Warns that ESM-2 in DeepLoc2 may fall back to CPU on long proteins. |
| No system `pip`, or a PEP 668 "externally managed" Python | Neither matters. VEDA never installs into the system Python; each environment ships its own pip. |
| `~/.local` packages | Reports how many, and suppresses them rather than touching them. |
| Docker absent, daemon down, no group, podman, or inside a container | Skips or repairs as appropriate instead of failing. |
| A `noexec` or symlink-less filesystem under `$VEDA_HOME` | Aborts early with an explanation. Tested by executing a file, not by parsing mount flags. |
| **A `$VEDA_HOME` that is too deep** | Aborts. The DTU binaries have a 64-byte buffer for their scratch directory path: measured against NetMHCpan 4.2, a `TMPDIR` of 63 characters produces predictions and 64 dies with `*** buffer overflow detected ***` and a core dump. The launcher points `TMPDIR` at `$VEDA_HOME/tmp`, so the depth of `$VEDA_HOME` decides whether NetMHCpan, NetMHCIIpan and GibbsCluster work at all. Keep it short — `~/.veda` is 15 characters. |
| CPU without AVX2, low RAM, non-UTF-8 locale, an HTTPS proxy | Warned about; the locale is forced to `C.UTF-8` for the tools that need it. |

### Environment isolation

The generated `veda` launcher unsets variables that an activated Anaconda or a
shell profile would otherwise leak into every worker subprocess —
`PYTHONPATH`, `CONDA_PREFIX`, `PERL5LIB`, `SSL_CERT_FILE`, `PIP_USER` and the
rest — and sets `PYTHONNOUSERSITE=1`. It also filters `PATH` and
`LD_LIBRARY_PATH` to remove:

* **empty entries**, which mean "the current directory" and would let a stray
  `python` or `.so` in whatever folder the app started from win;
* **other conda prefixes**;
* **a system CUDA** in `LD_LIBRARY_PATH`, which takes precedence over the CUDA
  libraries bundled in the PyTorch wheel and is a common cause of "torch
  installed fine but cannot see the GPU". Set `VEDA_KEEP_SYSTEM_CUDA=1` to
  keep it.

Only VEDA's own subprocesses are affected; your interactive shell is not.

This is why you should start the app with `veda`, **not** with
`streamlit run main.py` by hand — doing it by hand puts `~/.local` back in
front of the environment.

`veda doctor` includes an `env isolation` check that names anything still
shadowing the environment, so this cannot regress silently.

### The four environments

They are separate because their dependency pins genuinely conflict.

| Environment | Python | Serves |
|---|---|---|
| `veda-ui` | 3.10 | Streamlit app, dashboards, DeepLoc2, PyTorch |
| `veda-tools` | 3.9 | AlgPred2, ToxinPred2, IL2Pred, IL4Pred2 — **and BLAST** |
| `veda-tmhmm` | 3.9 | DeepTMHMM via `pybiolib` |
| `veda-ifn` | 3.10 | IFNEpitope2 |

They live in `~/.veda/mamba/envs`, not in your Anaconda install. This is
deliberate: a shared conda base gets upgraded by unrelated work and silently
breaks the scikit-learn pins these predictors unpickle their models against.

`blastp` belongs to `veda-tools` specifically because the workers locate BLAST
as `Path(config.TOOL_PYTHON).parent / "blastp"` rather than searching `$PATH`.

---

## Using a tool mirror (recommended for a lab)

Instead of cloning six repos and hand-staging five archives on every machine,
pack the tools once and have the installer pull them from one place you
control. This removes the two most fragile parts of the install: git-lfs
resolving correctly, and the manual DTU staging step.

### 1. Pack the bundle from a working machine

```bash
./veda-pack
```

Each tool becomes its own archive in `dist/mirror/`, and
`installer/manifest/mirror.manifest` records a SHA-256 and byte size for every
one. The whole set is **~500 MB compressed** (the sklearn RF models compress
about 5×, from 1.5 GB).

`veda-pack` cleans macOS litter and `__pycache__` before packing, and refuses
to pack a tool whose model file is still an unresolved git-lfs pointer.

### 2. Upload it

**GitHub Releases on a private repo** is the best host:

```bash
gh repo create your-org/veda-tools --private
./veda-pack --upload gh://your-org/veda-tools@tools-v1
```

Why Releases: 2 GiB per asset (the largest tool is ~160 MB), assets live
outside git history so clones stay small, CDN-backed and resumable, versioned
by tag, and a private repo keeps the DTU-licensed tools behind your own access
control.

**Do not use GitHub LFS for this.** The free quota is 10 GiB/month of
bandwidth — roughly 20 installs — and once exceeded, LFS is disabled on the
account until the next billing month. Release assets are not metered that way.

### 3. Point the installer at it

`--upload` writes `installer/mirror.conf` for you. Commit it together with
`installer/manifest/mirror.manifest`, and every later install is just:

```bash
./install.sh
```

Collaborators need `gh auth login` once, for a private repo.

### Supported mirror URLs

| Form | Notes |
|---|---|
| `gh://owner/repo@tag` | GitHub Release. **Recommended.** Works with private repos via `gh`. |
| `https://host/path/` | Any web server, S3, R2, B2, institutional space. Resumable. |
| `file:///path` | A mounted share or USB drive. The only form needing no network. |
| `rclone://remote:path` | Any rclone remote. Uses the provider's real API, so it avoids Drive's interstitial and quota. Needs `rclone config` once. |
| `gdrive://folder-id` | Google Drive via `gdown`. **Not recommended** — see below. |

Override per-run with `--mirror <url>`, or disable with `--no-mirror`.

### Why not plain Google Drive

It works, but it is the weakest option here:

- Every VEDA archive except two is over Drive's **~25 MB virus-scan threshold**,
  so each download needs the confirm-token workaround, which has broken
  repeatedly whenever Google changes its cookies or warning page.
- Drive's **quota lockout is triggered by request frequency**, returns HTTP
  403/429, and can last ~24 hours — exactly what happens when several people
  install on the same afternoon.

If you must use Drive, use the `rclone://` form: it authenticates against the
real Drive API and sidesteps both problems.

### Licence boundary

NetMHCpan, NetMHCIIpan, GibbsCluster, Seq2Logo and DeepLoc are licensed to you
by DTU Health Tech for your own academic use and **may not be redistributed**.
If your bundle includes them, the mirror must be access-controlled — a private
repo, or storage only your group can read. Not a public release, not a public
web server, not Zenodo. `veda-pack` warns you when the bundle contains them.

To build a bundle that is safe to host publicly:

```bash
./veda-pack --free-only      # the 6 redistributable tools only
./veda-pack --dtu-only       # the 5 licence-gated ones, for private hosting
```

Re-packing a subset **merges** into the existing manifest rather than replacing
it, so `./veda-pack --only algpred2` does not invalidate the other entries.

---

## The five tools you must fetch yourself

Eight of the thirteen tools download automatically. Five cannot:

| Tool | Why |
|---|---|
| NetMHCpan 4.2 | |
| NetMHCIIpan 4.3 | DTU Health Tech issues a **personal, single-use** download |
| GibbsCluster 2.0 | link after academic registration. The licence forbids |
| Seq2Logo 2.2 | redistribution, so no script may fetch them for you. |
| DeepLoc 2.1 | |

If they are already in `resources/tools/` the installer uses them and says
nothing. If a mirror is configured it fetches them from there. Otherwise it
prints the registration URL and waits.

To add one later:

1. Register and download the **Linux** archive from the URL the installer printed.
2. Drop the archive, unmodified, into `~/.veda/staging/`.
3. Re-run `./install.sh`.

Do this once, then `./veda-pack` and upload — and no one else has to repeat it.

Already extracted it somewhere? Move the folder to the name the manifest
expects, e.g. `resources/tools/netMHCpan-4.2/`.

**VEDA installs and runs without them.** The matching pipeline steps simply
report themselves unavailable. `./veda-doctor` lists exactly which.

---

## Running it

```bash
veda                  # start the web interface
veda doctor           # re-check every tool
veda paths            # show the resolved interpreter and tool paths
veda shell            # a shell inside the veda-ui environment
veda --server.port 8502
```

If `veda` is not found, either open a new terminal (the installer offers to add
`~/.local/bin` to your `PATH`) or run `~/.veda/bin/veda`.

Always start VEDA through the launcher. It puts `veda-ui/bin` at the front of
`PATH`, which several workers depend on — `deeploc2` is invoked by bare name.
Running `streamlit run main.py` from the wrong environment is the most common
way this pipeline breaks.

---

## Installer options

```
-y, --yes           take the default answer for every prompt (unattended)
-v, --verbose       stream all command output
-f, --force         redo stages already marked complete
    --from STAGE    start at STAGE and re-run everything after it
    --only STAGE    run just one stage
    --only-tool ID  with --only tools, handle a single tool
    --skip-docker   no Docker Engine, no image pulls
    --skip-system   do not touch apt
    --skip-verify   skip the post-install checks
    --cpu-only      CPU PyTorch even if an NVIDIA GPU is present
    --offline       no downloads; use only what is on disk
    --prewarm       also pre-download DeepLoc's ESM weights (several GB)
    --dry-run       print what would happen, change nothing
```

Useful recipes:

```bash
./install.sh -y                           # unattended
./install.sh --from envs --force          # rebuild the environments
./install.sh --only configure             # re-detect paths after moving the project
./install.sh --only tools --only-tool algpred2 --force
./install.sh --skip-docker --cpu-only     # minimal footprint
```

---

## What gets modified

| Path | |
|---|---|
| `~/.veda/` | micromamba, the environments, caches, logs, staging, state |
| `~/.local/bin/veda` | launcher symlink |
| `~/.local/share/applications/veda.desktop` | desktop entry |
| `resources/tools/` | the tools; six files are path-patched |
| `veda_paths.json` | generated, machine-local — **do not commit** |
| `~/.bashrc` | one guarded `PATH` line, and only if you agree |

Six tool files carry absolute paths of their own and are rewritten by the
`configure` stage. The original of each is kept next to it as `*.veda-orig`:

```
netMHCpan-4.2/netMHCpan          setenv NMHOME, setenv TMPDIR
netMHCIIpan-4.3/netMHCIIpan      setenv NMHOME
gibbscluster-2.0/gibbscluster    setenv GIBBS
algpred2/envfile                 BLASTP, BLASTDB, MERCI, MOTIFS
toxinpred2/envfile               BLASTP, BLASTDB, MERCI, MOTIFS
ifnepitope2-main/envfile         BLASTP, BLASTDB
```

`src/config.py` reads `veda_paths.json` for its interpreter paths. If the file
is absent it falls back to the legacy `~/anaconda3/envs/...` layout and then to
the running interpreter, so a hand-built setup keeps working.

---

## Troubleshooting

Start here — it names the failing component and the fix:

```bash
./veda-doctor
./veda-doctor --json      # machine-readable
```

| Symptom | Fix |
|---|---|
| `veda: command not found` | `~/.veda/bin/veda`, or open a new terminal |
| An environment is broken | `./install.sh --from envs --force` |
| Tool paths wrong after moving the project | `./install.sh --only configure` |
| One tool fails | `./install.sh --only tools --only-tool <id> --force` |
| Docker permission denied | Log out and back in, or `newgrp docker` |
| A model file is ~130 bytes | git-lfs pointer. `sudo apt-get install git-lfs && git lfs install`, then re-run with `--force` |
| conda solver fails | Usually transient; just re-run `./install.sh` |
| `ModuleNotFoundError: No module named 'pkg_resources'` | setuptools after 80.x stopped bundling it, and DeepLoc2 and ifnepitope2 both import it. The env specs pin `setuptools<81`; re-run `./install.sh --only envs` and the installer downgrades it in place. |
| GPU works in your shell but `torch.cuda.is_available()` is `False` | Almost always a system CUDA on `LD_LIBRARY_PATH` overriding the wheel's own libraries. The launcher strips it; check with `veda doctor --system`. |
| Installed a GPU driver after installing VEDA | `./install.sh --only envs`. The installer notices the CPU build no longer matches the machine and swaps in the CUDA one. |
| Anything behaving differently after a system update | `veda-doctor --system` first. It reports a changed driver, a newly activated Anaconda, and packages a distro upgrade renamed. |
| `*** buffer overflow detected ***` from a DTU tool | The scratch path is over 63 characters. Reinstall with a shorter `VEDA_HOME`. The probe now refuses to proceed in this case. |
| `Unable to open(r) file .../data/version` | The tool's data payload is missing — NetMHCpan ships a 190 MB `data/` tree and NetMHCIIpan a 125 MB one, separate from the wrapper script. Re-stage the tool from the original download. `./install.sh --only tools` now detects this instead of reporting the tool present. |
| DeepTMHMM's Docker step is greyed out | Expected. There is no public DeepTMHMM image — `biolib/dtu-deeptmhmm` is not a real Docker Hub repository and DTU does not publish one. DeepTMHMM runs in cloud mode through pybiolib, which the installer sets up and verifies. The local Docker path needs an image you supply yourself. |

Logs: `~/.veda/logs/install-latest.log` (and one per doctor run).

### Known inconsistency in the Vaxign-ML image name

Three places name a different image:

- `src/config.py` → now `e4ong1031/vaxign-ml:latest` (what the installer pulls)
- `src/backend/workers/vaxign_worker.py:78` → `e4ong1031/vaxign-ml:latest`
- `src/backend/proteins.py:562,574` → `teambbb/vaxign-ml`

The installer pulls `e4ong1031/vaxign-ml:latest`, which is the image the
project's own notes document. The `proteins.py` path still refers to
`teambbb/vaxign-ml`, which is not pulled; that code path will fail until the
name is reconciled. Left alone deliberately — changing worker behaviour is a
code decision, not an installer one.

---

## Uninstalling

```bash
./uninstall.sh          # environments, launcher, generated config
./uninstall.sh --all    # also ~/.veda (caches, logs, staging)
```

Your tools, `data/` and results are never deleted. The uninstaller offers to
restore the six patched tool files from their `.veda-orig` backups.
