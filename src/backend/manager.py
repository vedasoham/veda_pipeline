import subprocess
import sys
import config
from pathlib import Path
import os
import signal
import datetime

class PipelineManager:
    
    @staticmethod
    def run_deeptmhmm(fasta_file):
        """
        Orchestrates the DeepTMHMM worker.
        """
        # Use centralized path logic to ensure consistency with background runner
        paths = PipelineManager.get_deeptmhmm_paths(fasta_file)
        output_dir = paths["output_dir"]
        worker_script = paths["worker_script"]
        
        # The Command:
        # [Specific Python Interpreter] [The Worker Script] [Arguments]
        cmd = [
            config.TMHMM_PYTHON,       # <--- Crucial: Uses the separate env
            "-u",                      # Force unbuffered output
            str(worker_script),
            "--fasta", str(fasta_file),
            "--outdir", str(output_dir)
        ]
        
        print(f"COMMAND CENTER: Launching DeepTMHMM Worker...")
        print(f"Command: {' '.join(cmd)}")
        
        # --- FIX: SANITIZE ENVIRONMENT ---
        # We copy the current environment but remove PYTHONPATH.
        # This ensures the worker uses its OWN site-packages, not Streamlit's.
        env = os.environ.copy()
        env.pop("PYTHONPATH", None) 
        
        try:
            # We run this and capturing logs
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True,
                env=env  # <--- PASS THE CLEAN ENV HERE
            )
            
            # Print logs for debugging (or return them to Streamlit)
            print("--- WORKER STDOUT ---")
            print(result.stdout)
            print("--- WORKER STDERR ---")
            print(result.stderr)
            
            if result.returncode == 0:
                return True, f"Success! Results in {output_dir}"
            else:
                return False, f"Worker Failed. Output:\n{result.stdout}\nError:\n{result.stderr}"
                
        except Exception as e:
            return False, f"System Error: {e}"

    @staticmethod
    def get_deeptmhmm_paths(fasta_file, mode="cloud"):
        """Returns paths for background execution management."""
        worker_name = "tmhmm_worker.py" if mode == "cloud" else "tmhmm_local_worker.py"
        fasta_path = Path(fasta_file).resolve()
        if fasta_path.parent.name == "02_intermediate":
            output_dir = fasta_path.parent.parent / "03_results" / "deeptmhmm"
        elif fasta_path.parent.name == "workspace" or fasta_path.parent.name == "data":
            output_dir = fasta_path.parent / "deeptmhmm"
        else:
            # Fallback for non-project files
            output_dir = config.RESULTS_DIR / f"tmhmm_{fasta_path.stem}"
        
        output_dir.mkdir(parents=True, exist_ok=True)
        return {
            "output_dir": output_dir,
            "pid_file": output_dir / "job.pid",
            "log_file": output_dir / "job.log",
            "worker_script": config.WORKERS_DIR / worker_name
        }

    @staticmethod
    def run_deeptmhmm_bg(fasta_file, mode="cloud"):
        """Starts DeepTMHMM in the background (Non-blocking)."""
        paths = PipelineManager.get_deeptmhmm_paths(fasta_file, mode)
        
        # Clean up previous run artifacts
        if paths["pid_file"].exists(): paths["pid_file"].unlink()
        if paths["log_file"].exists(): paths["log_file"].unlink()

        # Use specific env for cloud (BioLib), standard python for local (Docker)
        python_exe = config.TMHMM_PYTHON if mode == "cloud" else sys.executable

        cmd = [
            python_exe,
            "-u",
            str(paths["worker_script"]),
            "--fasta", str(fasta_file),
            "--outdir", str(paths["output_dir"])
        ]
        
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        try:
            # Open log file for writing
            with open(paths["log_file"], "w") as f_log:
                # Write start timestamp
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f_log.write(f"[{timestamp}] Starting DeepTMHMM background job...\n")
                f_log.flush()
                # Start process detached
                proc = subprocess.Popen(cmd, stdout=f_log, stderr=subprocess.STDOUT, env=env)
            
            # Save PID
            with open(paths["pid_file"], "w") as f:
                f.write(str(proc.pid))
            
            return True, "Job started in background."
        except Exception as e:
            return False, f"Failed to start background job: {e}"

    @staticmethod
    def check_deeptmhmm_status(fasta_file):
        """Checks if the background job is running."""
        paths = PipelineManager.get_deeptmhmm_paths(fasta_file)
        if not paths["pid_file"].exists():
            return "stopped"
        
        # --- FIX: Check log for explicit success message to handle zombie processes ---
        if paths["log_file"].exists():
            try:
                # Read only the last 4KB to avoid memory issues with large logs
                file_size = paths["log_file"].stat().st_size
                with open(paths["log_file"], "r") as f:
                    if file_size > 4096:
                        f.seek(file_size - 4096)
                    content = f.read()
                    if "Files saved successfully" in content:
                        # Job is done, remove PID file to update status
                        try:
                            paths["pid_file"].unlink()
                        except Exception:
                            pass
                        return "stopped"
            except Exception:
                pass

        try:
            with open(paths["pid_file"], "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0) # Check if process exists (does not kill)
            return "running"
        except (OSError, ValueError):
            try:
                paths["pid_file"].unlink()
            except Exception:
                pass
            return "stopped"

    @staticmethod
    def stop_deeptmhmm(fasta_file):
        """Kills the running background job."""
        paths = PipelineManager.get_deeptmhmm_paths(fasta_file)
        if paths["pid_file"].exists():
            try:
                with open(paths["pid_file"], "r") as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
            finally:
                if paths["pid_file"].exists(): paths["pid_file"].unlink()

    # -------------------------------------------------------------------------
    # NetMHCpan / NetMHCIIpan Management
    # -------------------------------------------------------------------------
    @staticmethod
    def get_netmhc_paths(fasta_file, tool_name):
        """Returns paths for NetMHC background execution."""
        # tool_name should be 'netmhcpan' or 'netmhciipan'
        fasta_path = Path(fasta_file).resolve()
        # Assuming standard structure: intermediate/netmhcpan
        
        # FIX: Check if we are already in the tool directory to avoid nesting
        if fasta_path.parent.name == tool_name:
            output_dir = fasta_path.parent
        else:
            output_dir = fasta_path.parent / tool_name
            
        output_dir.mkdir(parents=True, exist_ok=True)
        
        return {
            "output_dir": output_dir,
            "pid_file": output_dir / "job.pid",
            "log_file": output_dir / "job.log",
            "worker_script": config.WORKERS_DIR / "netmhc_worker.py"
        }

    @staticmethod
    def run_netmhc_bg(fasta_file, tool, alleles_list, max_jobs=4):
        """Starts NetMHC in the background."""
        tool_name = "netmhciipan" if "ii" in tool.lower() else "netmhcpan"
        paths = PipelineManager.get_netmhc_paths(fasta_file, tool_name)
        
        # Clean up previous run
        if paths["pid_file"].exists(): paths["pid_file"].unlink()
        if paths["log_file"].exists(): paths["log_file"].unlink()

        # Determine Tool Config
        if tool_name == "netmhciipan":
            tool_bin = config.NETMHCIIPAN_BIN
            tool_home = config.NETMHCIIPAN_DIR
        else:
            tool_bin = config.NETMHCPAN_BIN
            tool_home = config.NETMHCPAN_DIR

        alleles_str = ",".join(alleles_list)

        cmd = [
            sys.executable, "-u", str(paths["worker_script"]),
            "--fasta", str(fasta_file),
            "--outdir", str(paths["output_dir"]),
            "--alleles", alleles_str,
            "--tool_path", str(tool_bin),
            "--tool_home", str(tool_home),
            "--max_jobs", str(max_jobs)
        ]

        try:
            with open(paths["log_file"], "w") as f_log:
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f_log.write(f"[{timestamp}] Starting {tool_name} background job...\n")
                f_log.flush()
                proc = subprocess.Popen(cmd, stdout=f_log, stderr=subprocess.STDOUT, start_new_session=True)
            
            with open(paths["pid_file"], "w") as f:
                f.write(str(proc.pid))
            return True, "Job started."
        except Exception as e:
            return False, f"Failed to start: {e}"

    @staticmethod
    def check_netmhc_status(fasta_file, tool_name):
        paths = PipelineManager.get_netmhc_paths(fasta_file, tool_name)
        if not paths["pid_file"].exists():
            return "stopped"
            
        # 1. Check log for completion (Handles zombie processes)
        if paths["log_file"].exists():
            try:
                file_size = paths["log_file"].stat().st_size
                with open(paths["log_file"], "r") as f:
                    if file_size > 4096:
                        f.seek(file_size - 4096)
                    content = f.read()
                    if "Job Finished!" in content or "Job Failed" in content:
                        try:
                            paths["pid_file"].unlink()
                        except Exception:
                            pass
                        return "stopped"
            except Exception:
                pass

        # 2. Check process existence
        try:
            with open(paths["pid_file"], "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return "running"
        except (OSError, ValueError):
            try:
                paths["pid_file"].unlink()
            except Exception:
                pass
            return "stopped"

    @staticmethod
    def stop_netmhc(fasta_file, tool_name):
        paths = PipelineManager.get_netmhc_paths(fasta_file, tool_name)
        if paths["pid_file"].exists():
            PipelineManager._kill_pid_file(paths["pid_file"])

    @staticmethod
    def run_vaxign_docker(fasta_file):
        """
        Orchestrates the Vaxign Docker container.
        """
        fasta_path = Path(fasta_file).resolve()
        output_dir = config.RESULTS_DIR / f"vaxign_{fasta_path.stem}"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Docker Command Construction
        # Note: We must mount volumes so Docker can see our files
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{fasta_path.parent}:/data",  # Mount input folder
            "-v", f"{output_dir}:/out",          # Mount output folder
            config.VAXIGN_IMAGE,
            "--input", f"/data/{fasta_path.name}",
            "--output", "/out",
            "--species", "bacteria" # Parameterize this later
        ]
        
        print(f"COMMAND CENTER: Launching Vaxign Docker...")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            print("--- Vaxign Docker STDOUT ---")
            print(result.stdout)
            print("--- Vaxign Docker STDERR ---")
            print(result.stderr)
            
            if result.returncode == 0:
                return True, "Vaxign completed successfully."
            else:
                return False, f"Docker Failed: {result.stderr}"
        except FileNotFoundError:
            return False, "Docker executable not found."

    @staticmethod
    def _kill_pid_file(pid_file):
        try:
            with open(pid_file, "r") as f:
                pid = int(f.read().strip())
            # Kill the process group to ensure all child processes (workers) are terminated
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (AttributeError, ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        finally:
            if pid_file.exists(): pid_file.unlink()
