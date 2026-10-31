import subprocess
import os
import sys
from pathlib import Path
import config
import datetime
import time

def run(fasta_path, output_dir, threshold=0.6, model=1, log_file=None):
    def log(msg):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {msg}"
        print(entry)
        if log_file:
            with open(log_file, "a") as f:
                f.write(entry + "\n")

    start_time = time.time()
    log(f"⏳ Job Started: ToxinPred 2.0")
    log(f"⚙️ Worker: Running ToxinPred 2.0 (Model {model}) in toxinpred2...")
    
    fasta_path = Path(fasta_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_csv = output_dir / "toxinpred_results.csv"
    
    log(f"📄 Input File: {fasta_path}")
    log(f"📂 Output File: {output_csv}")
    log(f"📝 Log File: {log_file}")
    
    # --- Update envfile dynamically ---
    # The tool requires an 'envfile' in its directory with absolute paths.
    envfile_path = config.TOXINPRED_DIR / "envfile"
    blastp_path = Path(config.TOOL_PYTHON).parent / "blastp"
    
    # Based on directory listing:
    # Database/data.* -> BLASTDB:.../Database/data
    # progs/MERCI_motif_locator.pl -> MERCI:...
    # Database/pos_motif.txt -> MOTIFS:...
    
    env_content = f"BLASTP:{blastp_path}\n" \
                  f"BLASTDB:{config.TOXINPRED_DIR}/Database/data\n" \
                  f"MERCI:{config.TOXINPRED_DIR}/progs/MERCI_motif_locator.pl\n" \
                  f"MOTIFS:{config.TOXINPRED_DIR}/Database/pos_motif.txt\n"
    try:
        with open(envfile_path, "w") as f:
            f.write(env_content)
    except Exception as e:
        log(f"⚠️  Warning: Could not write envfile: {e}")

    # --- Create Patch Script for Pandas 2.0 Compatibility ---
    # ToxinPred2 uses .append() which was removed in Pandas 2.0.
    # We create a wrapper that monkey-patches it back before running the tool.
    patch_script = config.TOXINPRED_DIR / "toxinpred_patch.py"
    patch_content = """
import sys
import pandas as pd
import runpy

# Monkey-patch DataFrame.append if missing (Pandas 2.0+)
if not hasattr(pd.DataFrame, 'append'):
    def append(self, other, ignore_index=False, verify_integrity=False, sort=False):
        from pandas import concat
        if isinstance(other, (list, tuple)):
            to_concat = [self] + list(other)
        else:
            to_concat = [self, other]
        return concat(to_concat, ignore_index=ignore_index, verify_integrity=verify_integrity, sort=sort)
    pd.DataFrame.append = append

# Run the actual tool
try:
    runpy.run_path("toxinpred2.py", run_name="__main__")
except Exception as e:
    print(f"Wrapper Error: {e}")
    sys.exit(1)
"""
    try:
        with open(patch_script, "w") as f:
            f.write(patch_content)
    except Exception as e:
        log(f"⚠️ Warning: Could not write patch script: {e}")

    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        config.TOOL_PYTHON,
        str(patch_script), # Run the patch wrapper instead of the original script
        "-i", str(fasta_path),
        "-o", str(output_csv),
        "-t", str(threshold),
        "-m", str(model),
        "-d", "2" # Display 2: All peptides (useful for pipelines to see negatives too)
    ]

    try:
        # We must run inside the directory so it finds 'RF_model'
        log(f"🚀 Executing: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            cwd=str(config.TOXINPRED_DIR), 
            capture_output=True,
            text=True,
            check=False
        )

        if result.stderr:
            # Print only last 500 chars to avoid clutter
            log(f"⚠️ ToxinPred STDERR: {result.stderr[-500:]}")
            
        if output_csv.exists():
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"✅ ToxinPred finished. Saved to {output_csv}. Elapsed: {elapsed:.2f}s")
            return True
        else:
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"❌ ToxinPred Failed. No output file. Elapsed: {elapsed:.2f}s")
            log(f"STDOUT: {result.stdout}")
            return False

    except Exception as e:
        log(f"❌ Execution Error: {e}")
        return False