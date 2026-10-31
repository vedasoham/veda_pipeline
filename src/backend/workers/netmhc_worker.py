import subprocess
import os
from pathlib import Path
import sys
import datetime
from concurrent.futures import ThreadPoolExecutor
import shutil
import time
import math
import re

def run(fasta_path, output_dir, alleles="HLA-A02:01", length=9, tool_path=None, tool_home=None, max_jobs=1, log_file=None):
    def log(msg, timestamp_override=None):
        timestamp = timestamp_override or datetime.datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {msg}"
        print(entry)
        if log_file:
            with open(log_file, "a") as f:
                f.write(entry + "\n")

    """
    Runs NetMHCpan-4.2 (Local Version).
    Args:
        alleles (str): Comma-separated alleles (e.g., "HLA-A02:01,HLA-B07:02")
        length (int): Peptide length to scan (usually 9)
        tool_path (Path): Path to the netMHCpan executable.
        tool_home (Path): Path to the netMHCpan installation directory (NMHOME).
    """
    start_time = time.time()
    fasta_name = Path(fasta_path).name
    
    # Extract batch info for display
    batch_display = fasta_name
    match = re.search(r"batch_(\d+)", fasta_name)
    if match:
        batch_display = f"Batch {match.group(1)}"

    # Format alleles for display
    allele_list = [a.strip() for a in alleles.split(',') if a.strip()]
    if len(allele_list) > 3:
        alleles_display = f"{', '.join(allele_list[:3])} (+{len(allele_list)-3} more)"
    else:
        alleles_display = ", ".join(allele_list)

    log(f"🚀 {batch_display}: Started analysis")
    log(f"   🔹 Input: {fasta_name} | Alleles: {alleles_display}")
    
    # Define Output Path
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    if not tool_path or not Path(tool_path).exists():
        log(f"❌ Error: NetMHCpan binary not found at {tool_path}")
        return False

    # Check input file
    if not Path(fasta_path).exists() or Path(fasta_path).stat().st_size == 0:
        log(f"❌ Error: Input FASTA file is missing or empty: {fasta_path}")
        return False

    # Prepare Output File
    out_file = output_dir / f"{Path(fasta_path).stem}.txt"
    
    # Prepare Environment (Set NMHOME so the tool finds its data)
    env = os.environ.copy()
    if "TMPDIR" not in env:
        env["TMPDIR"] = "/tmp"
    # Force single-threaded execution for the tool itself to avoid oversubscription
    env["OMP_NUM_THREADS"] = "1"
    env["MKL_NUM_THREADS"] = "1"
    if tool_home:
        tool_home_path = Path(tool_home)
        if "netmhciipan" in str(tool_path).lower():
            env["NMHOME"] = str(tool_home_path)
            env["NetMHCIIpan"] = str(tool_home_path)
            env["NETMHCIIpan"] = str(tool_home_path)
            if (tool_home_path / "Linux_x86_64").exists():
                env["NetMHCIIpanPLAT"] = str(tool_home_path / "Linux_x86_64")
        else:
            env["NMHOME"] = str(tool_home_path)
            env["NETMHCpan"] = str(tool_home_path)

    if not allele_list:
        log("❌ No alleles provided.")
        return False

    # Dynamic Chunking: Split alleles to utilize all available threads
    if max_jobs > 1:
        chunk_size = math.ceil(len(allele_list) / max_jobs)
        chunk_size = max(1, min(chunk_size, 20)) # Cap at 20, min 1
    else:
        chunk_size = 20
    
    chunks = [allele_list[i:i + chunk_size] for i in range(0, len(allele_list), chunk_size)]
    
    def run_chunk(chunk_alleles):
        chunk_str = ",".join(chunk_alleles)
        
        # Build Command
        cmd = [str(tool_path)]

        # NetMHCIIpan uses -f for both formats and -inptype 1 for peptides.
        # NetMHCpan explicitly uses -p for peptide formats.
        if "netmhciipan" in str(tool_path).lower():
            cmd.extend(["-inptype", "1", "-f", str(fasta_path)])
        else:
            cmd.extend(["-p", str(fasta_path)])
            
        cmd.extend(["-a", chunk_str])

        # In peptide mode (-p), we evaluate exactly the sequences provided in the file.
        # No length filtering arguments are added here since the python backend already chopped them.
            
        try:
            res = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                env=env
            )
            return res.returncode, res.stdout, res.stderr, cmd
        except Exception as e:
            if "Errno 2" in str(e):
                return -1, "", f"Executable not found or missing interpreter (e.g., tcsh). Tried: {cmd[0]}", cmd
            return -1, "", str(e), cmd

    try:
        final_stdout = []
        errors = 0
        
        results = []
        try:
            with ThreadPoolExecutor(max_workers=max_jobs) as executor:
                futures = [executor.submit(run_chunk, chunk) for chunk in chunks]
                for future in futures:
                    results.append(future.result())
        except RuntimeError as e:
            if "interpreter shutdown" in str(e):
                log("⚠️ ThreadPoolExecutor failed (interpreter shutdown). Switching to serial execution.")
                results = [run_chunk(chunk) for chunk in chunks]
            else:
                raise e

        for i, (rc, out, err, cmd_used) in enumerate(results):
                if rc != 0:
                    errors += 1
                    log(f"❌ Chunk {i+1} Failed (Exit Code {rc})")
                    log(f"🔍 Command: {' '.join(cmd_used)}")
                    
                    if out.strip(): log(f"⚠️ STDOUT: {out.strip()}")
                    if err.strip(): log(f"⚠️ STDERR: {err.strip()}")

                    if "buffer overflow" in err:
                        log("🛑 DIAGNOSTIC: Buffer overflow detected. This is usually caused by long file paths.")
                        log("👉 ACTION: Ensure TMPDIR is set to a short path (e.g., /tmp).")

                    if "buffer overflow" in err:
                        log("🛑 DIAGNOSTIC: Buffer overflow detected. This is usually caused by long file paths.")
                        log("👉 ACTION: Ensure TMPDIR is set to a short path (e.g., /tmp).")
                    
                    # --- Diagnostics ---
                    # 1. Check Binary
                    if not Path(tool_path).exists():
                        log(f"🛑 DIAGNOSTIC: Binary not found at {tool_path}")
                    elif not os.access(tool_path, os.X_OK):
                        log(f"🛑 DIAGNOSTIC: Binary is not executable. Run 'chmod +x {tool_path}'")
                    
                    # 2. Check Interpreter (common issue with NetMHC wrappers using tcsh/csh)
                    try:
                        with open(tool_path, 'rb') as f:
                            header = f.read(100).decode('utf-8', errors='ignore').splitlines()[0]
                        if header.startswith("#!"):
                            interp = header[2:].strip().split()[0]
                            if not shutil.which(interp):
                                log(f"🛑 DIAGNOSTIC: Interpreter '{interp}' not found. Please install it (e.g., 'sudo apt install tcsh' or 'csh').")
                    except: pass

                    # 3. Check Environment
                    if env.get("NMHOME") and not Path(env["NMHOME"]).exists():
                        log(f"🛑 DIAGNOSTIC: NMHOME directory missing: {env['NMHOME']}")
                    
                    if env.get("TMPDIR"):
                        tp = Path(env["TMPDIR"])
                        if not tp.exists(): log(f"🛑 DIAGNOSTIC: TMPDIR missing: {tp}")
                        elif not os.access(tp, os.W_OK): log(f"🛑 DIAGNOSTIC: TMPDIR not writable: {tp}")

                else:
                    final_stdout.append(out)
        
        if errors == 0:
            # Combine and Write stdout to file
            full_output = "\n".join(final_stdout)
            
            if not full_output.strip():
                log("❌ NetMHCpan Job Failed: Output is empty.")
                # Log stderr from chunks to help diagnose silent failures
                for i, (rc, out, err, cmd_used) in enumerate(results):
                    if err and err.strip():
                        log(f"   ⚠️ Chunk {i+1} STDERR: {err.strip()}")
                return False
                
            if "Pos" not in full_output or "MHC" not in full_output:
                log("❌ NetMHCpan Job Failed: Output contains no valid predictions.")
                log("   ⚠️ DIAGNOSIS: The tool skipped all peptides.")
                log("   ⚠️ ACTION: Ensure your sequences contain only standard amino acids and meet minimum length requirements.")
                log(f"   ⚠️ Raw Output Snippet:\n{full_output.strip()[:1000]}")
                return False
                
            with open(out_file, "w") as f:
                f.write(full_output)
            
            elapsed = time.time() - start_time
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
            log(f"   ✅ Finished in {time_str}")
            return True
        else:
            log(f"❌ NetMHCpan Failed: {errors} chunks failed.")
            return False

    except Exception as e:
        log(f"❌ Execution Error: {e}")
        return False

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--fasta", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--alleles", required=True)
    parser.add_argument("--tool_path", required=True)
    parser.add_argument("--tool_home", required=True)
    parser.add_argument("--max_jobs", type=int, default=1)
    
    args = parser.parse_args()
    
    # Run with default length=9 (standard for NetMHCpan)
    success = run(args.fasta, args.outdir, args.alleles, 9, args.tool_path, args.tool_home, args.max_jobs)
    sys.exit(0 if success else 1)