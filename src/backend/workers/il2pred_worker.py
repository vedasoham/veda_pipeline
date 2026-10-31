import subprocess
import os
import sys
from pathlib import Path
import config
import datetime
import time

def log(msg, log_file=None):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry, flush=True)
    if log_file:
        with open(log_file, "a") as f:
            f.write(entry + "\n")

def run(fasta_path, output_dir, threshold=0.38, job=1, model=1, log_file=None):
    start_time = time.time()
    log(f"⏳ Job Started: IL2Pred", log_file)
    log(f"⚙️ Worker: Running IL2Pred (Job {job}, Model {model})...", log_file)
    
    fasta_path = Path(fasta_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_csv = output_dir / "il2pred_results.csv"
    
    log(f"📄 Input File: {fasta_path}", log_file)
    log(f"📂 Output File: {output_csv}", log_file)
    
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Create Patch Script for Pandas 2.0 Compatibility ---
    patch_script = config.IL2PRED_DIR / "il2pred_patch.py"
    patch_content = """
import sys
import pandas as pd
import os

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

target_script = "il2pred.py"

if not os.path.exists(target_script):
    print(f"Wrapper Error: {target_script} not found.")
    sys.exit(1)

try:
    # Read file with utf-8-sig to handle potential BOM
    with open(target_script, "r", encoding="utf-8-sig") as f:
        source = f.read()
    
    sys.argv[0] = target_script
    
    # Compile and execute manually to avoid runpy quirks
    code = compile(source, target_script, "exec")
    globs = globals().copy()
    globs.update({'__file__': target_script, '__name__': '__main__'})
    exec(code, globs)

except Exception as e:
    print(f"Wrapper Error: {e}")
    sys.exit(1)
"""
    try:
        with open(patch_script, "w") as f:
            f.write(patch_content)
    except Exception as e:
        log(f"⚠️ Warning: Could not write patch script: {e}", log_file)

    cmd = [
        config.TOOL_PYTHON,
        str(patch_script),
        "-i", str(fasta_path),
        "-o", str(output_csv),
        "-t", str(threshold),
        "-j", str(job),
        "-m", str(model),
        "-d", "2"
    ]

    try:
        log(f"🚀 Executing: {' '.join(cmd)}", log_file)
        result = subprocess.run(
            cmd,
            cwd=str(config.IL2PRED_DIR), 
            capture_output=True,
            text=True,
            check=False
        )

        if result.stderr:
            log(f"⚠️ IL2Pred STDERR: {result.stderr[-500:]}", log_file)
            
        if output_csv.exists():
            elapsed = time.time() - start_time
            log(f"✅ IL2Pred finished. Saved to {output_csv}. Elapsed: {elapsed:.2f}s", log_file)
            return True
        else:
            elapsed = time.time() - start_time
            log(f"❌ IL2Pred Failed. No output file. Elapsed: {elapsed:.2f}s", log_file)
            log(f"STDOUT: {result.stdout}", log_file)
            return False

    except Exception as e:
        log(f"❌ Execution Error: {e}", log_file)
        return False