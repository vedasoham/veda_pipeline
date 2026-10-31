# Getting the five DTU Health Tech tools

These five are free for academic use but cannot be distributed with VEDA, so
you request them yourself. It is a form, an email with a download link, and
then the installer does the rest. Budget ten minutes plus however long DTU
takes to reply.

VEDA works without them. The pipeline steps they power are greyed out until
they are installed, and you can add them at any time.

## The short version

1. Request each tool from the links in the table below. You need an
   institutional email address.
2. Drop the archives, exactly as downloaded, into `~/.veda/staging/`.
3. Run `./install.sh --only tools`.
4. Check with `veda doctor`.

```bash
mkdir -p ~/.veda/staging
cp ~/Downloads/netMHCpan-4.2.Linux.tar.gz    ~/.veda/staging/
cp ~/Downloads/netMHCIIpan-4.3.Linux.tar.gz  ~/.veda/staging/
cp ~/Downloads/gibbscluster-2.0*.tar.gz      ~/.veda/staging/
cp ~/Downloads/seq2logo-2.2*.tar.gz          ~/.veda/staging/
cp ~/Downloads/deeploc-2.1*.tar.gz           ~/.veda/staging/

./install.sh --only tools
veda doctor
```

Do not unpack them and do not rename them. The installer finds an archive by
looking for the tool's name in the filename, unpacks it into the right place,
rewrites the paths inside it, and fixes the file permissions. Unpacking by hand
means doing those last three steps yourself.

## The five tools

Pick the **Linux** build on each download page. VEDA requires `x86_64`; there
is no ARM build of these tools, so Apple Silicon and ARM servers cannot run
them.

| Tool | What VEDA uses it for | Request from | Archive you will receive |
|---|---|---|---|
| **NetMHCpan 4.2** | MHC class-I binding prediction | [NetMHCpan-4.2](https://services.healthtech.dtu.dk/services/NetMHCpan-4.2/) | `netMHCpan-4.2.Linux.tar.gz` |
| **NetMHCIIpan 4.3** | MHC class-II binding prediction | [NetMHCIIpan-4.3](https://services.healthtech.dtu.dk/services/NetMHCIIpan-4.3/) | `netMHCIIpan-4.3.Linux.tar.gz` |
| **GibbsCluster 2.0** | clustering peptides into binding motifs | [GibbsCluster-2.0](https://services.healthtech.dtu.dk/services/GibbsCluster-2.0/) | `gibbscluster-2.0*.Linux.tar.gz` |
| **Seq2Logo 2.2** | sequence logos for the clusters | [Seq2Logo-2.0](https://services.healthtech.dtu.dk/services/Seq2Logo-2.0/) | `seq2logo-2.2*.tar.gz` |
| **DeepLoc 2.1** | subcellular localisation | [DeepLoc-2.1](https://services.healthtech.dtu.dk/services/DeepLoc-2.1/) | `deeploc-2.1.All.tar.gz` |

Each page has a **Downloads** tab with an academic request form. DTU emails a
link. If you are not at a publicly funded academic institution, the pages
direct you to `health-software@dtu.dk` for a commercial licence instead.

Versions: VEDA is tested against the versions above. A newer point release
(say NetMHCpan 4.2b vs 4.2c) is fine. A different major version installs into
a differently named directory and VEDA will not find it — tell us if you need
one and the manifest can be updated.

## Checking it worked

```bash
veda doctor
```

Each tool is run against a small real input, so this distinguishes *installed*
from *actually working*:

```
✔ NetMHCpan 4.2          predicted 3 peptide-allele rows
✔ NetMHCIIpan 4.3        predicted 2 MHC-II binding rows
✔ GibbsCluster 2.0       wrapper + binary present
✔ Seq2Logo 2.2           responds to -h
✔ DeepLoc 2.1            console script works
```

Anything you have not installed is reported as *unavailable*, not failed.

## If something goes wrong

**`data files missing`, or no predictions despite no error.** NetMHCpan and
NetMHCIIpan keep their scoring matrices in a `data/` directory inside the
archive — 190 MB and 125 MB respectively. If that directory is absent the
program prints one line, `Unable to open(r) file .../data/version`, and then
**exits 0 having predicted nothing**. An incomplete or interrupted download is
the usual cause. Check it arrived:

```bash
ls resources/tools/netMHCpan-4.2/data/version
ls resources/tools/netMHCIIpan-4.3/data/version
```

Re-download and re-run `./install.sh --only tools --force` if either is
missing. `veda doctor` detects this case by name, and the installer no longer
counts a tool as installed when only its launcher script is present.

**`*** buffer overflow detected ***`.** These programs store their scratch
directory path in a fixed 64-byte buffer. VEDA points them at
`$VEDA_HOME/tmp`, so a deep `VEDA_HOME` overflows it: 63 characters works, 64
aborts. Reinstall with something short — the default `~/.veda` is 15
characters. The installer checks this before downloading anything.

**The installer did not find your archive.** It matches on the tool name
appearing somewhere in the filename, case-insensitively, for `.tar.gz`,
`.tgz`, `.tar.Z`, `.tar` and `.zip`. If you renamed the file, either restore a
name containing the tool name or unpack it into
`resources/tools/<expected-directory>/` yourself and re-run
`./install.sh --only tools`. Expected directory names:

```
netMHCpan-4.2/   netMHCIIpan-4.3/   gibbscluster-2.0/
seq2logo-2.2/    deeploc-2.1.All/
```

**Permission denied running a tool.** `tar` does not always preserve the
executable bit. `./install.sh --only tools` re-applies it.

## Please do not redistribute these

Your DTU licence is issued to you, at your institution, and does not let you
pass the programs to anyone else. Concretely, that rules out committing them to
a repository, attaching them to a public release, putting them on a web server
or in a public data repository such as Zenodo, or including them in a container
image you publish.

VEDA's `.gitignore` excludes this whole directory for that reason, in two
places, so a stray `git add` cannot commit them by accident.

If you are setting colleagues up, `../../veda-pack` builds a local mirror so
nobody re-downloads. Host a DTU-containing bundle only where access is
controlled and limited to people holding their own licence; `veda-pack
--free-only` builds one that is safe to share openly. The tool warns you when
a bundle contains licensed material.

## Citing them

These are other people's methods and each has its own paper. The DTU service
page for each tool lists the citation — please cite them alongside VEDA in any
publication that uses their output.
