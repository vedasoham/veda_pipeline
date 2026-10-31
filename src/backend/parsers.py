import pandas as pd

def parse_netmhc_output(file_path):
    """
    Parses NetMHCpan standard output (txt) into a clean list of binders.
    """
    results = []
    try:
        with open(file_path, 'r') as f:
            lines = f.readlines()
        
        data_start = False
        is_class_ii = False
        headers = []
        for line in lines:
            line = line.strip()
            if "NetMHCIIpan" in line:
                is_class_ii = True
                
            if line.startswith("Pos") and ("MHC" in line or "HLA" in line):
                headers = line.split()
                # Secondary fallback if header is missing: Inverted is unique to IIpan 4.3
                if "Inverted" in headers:
                    is_class_ii = True
                data_start = True
                continue
            if line.startswith("----------------"):
                continue
            if not data_start or not line or not headers:
                continue
            
            parts = line.split()
            if not parts[0].isdigit(): continue
            
            try:
                def get_idx(col_names):
                    for col_name in col_names:
                        for i, h in enumerate(headers):
                            if col_name.lower() == h.lower():
                                return i
                    return -1

                idx_pep = get_idx(["Peptide", "Seq"])
                idx_mhc = get_idx(["MHC", "Allele", "HLA"])
                
                if is_class_ii:
                    idx_id = get_idx(["Identity", "ID", "Protein"])
                    idx_rank = get_idx(["%Rank_EL", "Rank", "%Rank"])
                    idx_aff = get_idx(["Affinity(nM)", "nM", "Affinity"])
                else:
                    idx_rank = get_idx(["EL_Rank", "%Rank_EL", "Rank", "%Rank"])
                    idx_aff = get_idx(["Aff(nM)", "Affinity", "Affinity(nM)", "nM"])
                    idx_id = get_idx(["Identity", "ID", "Protein"])

                if idx_pep == -1 or idx_mhc == -1 or idx_rank == -1:
                    continue
                
                peptide = parts[idx_pep]
                allele = parts[idx_mhc]
                rank = parts[idx_rank]
                affinity = parts[idx_aff] if idx_aff != -1 else 0.0
                
                # Use robust index fallback for protein ID to handle format shifts safely
                protein_id = parts[idx_id] if idx_id != -1 and idx_id < len(parts) else "Unknown"
                    
                res = {
                    "protein_id": protein_id,
                    "peptide": peptide,
                    "allele": allele,
                    "affinity": float(affinity) if affinity else 0.0,
                    "rank": float(rank) if rank else 100.0,
                    "level": ""
                }
                
                idx_ref = idx_aff if idx_aff != -1 else idx_rank
                
                # Extract BindLevel tags safely (checks to the end of the line)
                if len(parts) > idx_ref + 1:
                    level_parts = parts[idx_ref+1:]
                    if "SB" in level_parts: res["level"] = "SB"
                    elif "WB" in level_parts: res["level"] = "WB"
                
                # Fallback rank calculation if no BindLevel parsed
                if not res["level"]:
                    if is_class_ii:
                        if res["rank"] <= 1.0: res["level"] = "SB"
                        elif res["rank"] <= 5.0: res["level"] = "WB"
                    else:
                        if res["rank"] <= 0.5: res["level"] = "SB"
                        elif res["rank"] <= 2.0: res["level"] = "WB"
                
                results.append(res)
            except (ValueError, IndexError):
                continue

    except Exception as e:
        print(f"❌ Error parsing NetMHC results: {e}")
        return []
    
    return results

def parse_vaxign_results(result_path):
    """
    Parses Vaxign-ML output file.
    Returns a DataFrame with Protein ID and Vaxign Score.
    """
    try:
        # Vaxign output is often tab-separated or csv
        # We try tab first
        df = pd.read_csv(result_path, sep='\t')
        
        # If it looks weird (1 column), try CSV
        if df.shape[1] < 2:
            df = pd.read_csv(result_path, sep=',')
            
        # Normalize column names
        df.columns = df.columns.str.strip().str.lower()
        
        # Look for the score column. Usually 'vaxign-ml_score' or 'probability'
        score_col = None
        # Common Vaxign-ML column names: 'vaxign-ml_score', 'protegenicity', 'probability', 'score', 'prediction'
        candidates = ['vaxign-ml_score', 'protegenicity', 'probability', 'score', 'prediction']
        
        for cand in candidates:
            for col in df.columns:
                if cand in col:
                    score_col = col
                    break
            if score_col: break
        
        if score_col:
            # Sort by score descending (Highest score = Best Antigen)
            df = df.sort_values(by=score_col, ascending=False)
            return df
        else:
            print("⚠️ Could not find Score column in Vaxign output. Returning raw table.")
            return df

    except Exception as e:
        print(f"❌ Vaxign Parse Error: {e}")
        return pd.DataFrame()

def parse_algpred_results(file_path):
    """
    Parses AlgPred 2.0 CSV output.
    """
    try:
        # AlgPred outputs standard CSV
        df = pd.read_csv(file_path)
        
        # Clean columns if needed (remove whitespace)
        df.columns = df.columns.str.strip()
        
        # Determine Score Column (Hybrid Score or ML_Score)
        score_col = 'Hybrid Score' if 'Hybrid Score' in df.columns else 'ML_Score'
        
        # Rename for consistency
        if score_col in df.columns:
            df.rename(columns={score_col: 'Allergen_Score'}, inplace=True)
            
        return df
    except Exception as e:
        print(f"❌ Parse Error (AlgPred): {e}")
        return pd.DataFrame()

def parse_toxinpred_results(file_path):
    """
    Parses ToxinPred 2.0 CSV output.
    """
    try:
        df = pd.read_csv(file_path)
        
        # Clean columns if needed (remove whitespace)
        df.columns = df.columns.str.strip()
        
        return df
    except Exception as e:
        print(f"❌ Parse Error (ToxinPred): {e}")
        return pd.DataFrame()

def parse_il2pred_results(file_path):
    """
    Parses IL2Pred CSV output.
    """
    try:
        df = pd.read_csv(file_path)
        
        # Clean columns if needed (remove whitespace)
        df.columns = df.columns.str.strip()
        
        return df
    except Exception as e:
        print(f"❌ Parse Error (IL2Pred): {e}")
        return pd.DataFrame()

def parse_ifnepitope_results(file_path):
    """
    Parses IFNEpitope CSV output.
    """
    try:
        df = pd.read_csv(file_path)
        
        # Clean columns if needed (remove whitespace)
        df.columns = df.columns.str.strip()
        
        return df
    except Exception as e:
        print(f"❌ Parse Error (IFNEpitope): {e}")
        return pd.DataFrame()