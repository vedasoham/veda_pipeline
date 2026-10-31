import subprocess
import os
import sys
from pathlib import Path
import datetime
import time
import config

def run(fasta_path, output_dir, threshold=0.49, host=1, job=1, window=8, log_file=None):
    def log(msg):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {msg}"
        print(entry)
        if log_file:
            with open(log_file, "a") as f:
                f.write(entry + "\n")

    start_time = time.time()
    log(f"⏳ Job Started: IFNEpitope")
    log(f"⚙️ Worker: Running IFNEpitope (Host {host}, Job {job}) in ifnepitope2...")
    
    fasta_path = Path(fasta_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_csv = output_dir / "ifnepitope_results.csv"
    
    log(f"📄 Input File: {fasta_path}")
    log(f"📂 Output File: {output_csv}")
    log(f"📝 Log File: {log_file}")
    
    # --- Update envfile dynamically ---
    envfile_path = config.IFNEPITOPE_DIR / "envfile"
    blastp_path = Path(config.TOOL_PYTHON).parent / "blastp"
    
    # Point BLASTDB to the Database folder
    env_content = f"BLASTP:{blastp_path}\n" \
                  f"BLASTDB:{config.IFNEPITOPE_DIR}/Database\n"
    try:
        with open(envfile_path, "w") as f:
            f.write(env_content)
    except Exception as e:
        log(f"⚠️  Warning: Could not write envfile: {e}")

    # --- Create Patch Script for Pandas 2.0 Compatibility ---
    patch_script = config.IFNEPITOPE_DIR / "ifnepitope_patch.py"
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

target_script = "ifnepitope2.py"

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
    
    # Set up globals
    globs = globals().copy()
    globs.update({'__file__': target_script, '__name__': '__main__'})
    
    exec(code, globs)

except SyntaxError as e:
    print(f"Wrapper Error: invalid syntax ({target_script}, line {e.lineno})")
    try:
        with open(target_script, "r") as f:
            lines = f.readlines()
            if lines and "<html" in lines[0].lower():
                print("CRITICAL: File appears to be HTML (download error).")
            elif 0 < e.lineno <= len(lines):
                print(f"Offending line: {lines[e.lineno-1].strip()}")
    except: pass
    sys.exit(1)
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
        config.IFNEPITOPE_PYTHON,
        str(patch_script),
        "-i", str(fasta_path),
        "-o", str(output_csv),
        "-t", str(threshold),
        "-s", str(host),   # 1: Human, 2: Mouse
        "-j", str(job),    # 1: Predict, 2: Design, 3: Scan
        "-w", str(window), # Window length (for scan)
        "-d", "2"          # Display: 2: All peptides
    ]

    try:
        log(f"🚀 Executing: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            cwd=str(config.IFNEPITOPE_DIR), 
            capture_output=True,
            text=True,
            check=False
        )

        if result.stderr:
            log(f"⚠️ IFNEpitope STDERR: {result.stderr[-500:]}")
            
        if output_csv.exists():
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"✅ IFNEpitope finished. Saved to {output_csv}. Elapsed: {elapsed:.2f}s")
            return True
        else:
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"❌ IFNEpitope Failed. No output file. Elapsed: {elapsed:.2f}s")
            log(f"STDOUT: {result.stdout}")
            return False

    except Exception as e:
        log(f"❌ Execution Error: {e}")
        return False