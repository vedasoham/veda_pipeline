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

def run(fasta_path, output_dir, threshold=0.3, model=2, log_file=None):
    start_time = time.time()
    log(f"⏳ Job Started: AlgPred 2.0", log_file)
    log(f"⚙️ Worker: Running AlgPred 2.0 (Model {model}) in algpred2...", log_file)
    
    fasta_path = Path(fasta_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_csv = output_dir / "algpred_results.csv"
    
    log(f"📄 Input File: {fasta_path}", log_file)
    log(f"📂 Output File: {output_csv}", log_file)
    log(f"📝 Log File: {log_file}", log_file)
    
    # --- Update envfile dynamically ---
    # The tool requires an 'envfile' in its directory with absolute paths.
    # We generate it here to ensure it matches the current installation.
    envfile_path = config.ALGPRED_DIR / "envfile"
    blastp_path = Path(config.TOOL_PYTHON).parent / "blastp"
    
    env_content = f"BLASTP:{blastp_path}\n" \
                  f"BLASTDB:{config.ALGPRED_DIR}/Database/data\n" \
                  f"MERCI:{config.ALGPRED_DIR}/progs/MERCI_motif_locator.pl\n" \
                  f"MOTIFS:{config.ALGPRED_DIR}/Database/pos_ige_motifs.txt\n"
    try:
        with open(envfile_path, "w") as f:
            f.write(env_content)
    except Exception as e:
        log(f"⚠️  Warning: Could not write envfile: {e}", log_file)

    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        config.TOOL_PYTHON,  # <--- Using the dedicated environment from config
        str(config.ALGPRED_SCRIPT),
        "-i", str(fasta_path),
        "-o", str(output_csv),
        "-t", str(threshold),
        "-m", str(model),
        "-d", "2"
    ]

    try:
        # We must run inside the directory so it finds 'rf_model'
        log(f"🚀 Executing: {' '.join(cmd)}", log_file)
        result = subprocess.run(
            cmd,
            cwd=str(config.ALGPRED_DIR), 
            capture_output=True,
            text=True,
            check=False
        )

        if result.stderr:
            # Print only last 500 chars to avoid clutter
            log(f"⚠️ AlgPred STDERR: {result.stderr[-500:]}", log_file)
            
        if output_csv.exists():
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"✅ AlgPred finished. Saved to {output_csv}. Elapsed: {elapsed:.2f}s", log_file)
            return True
        else:
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"❌ AlgPred Failed. No output file. Elapsed: {elapsed:.2f}s", log_file)
            # Print STDOUT to see why it failed (path errors often show here)
            log(f"STDOUT: {result.stdout}", log_file)
            return False

    except Exception as e:
        log(f"❌ Execution Error: {e}", log_file)
        return False